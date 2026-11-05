"""Workload controller: node assignments and model deployments -> node labels and Kubernetes objects."""

import ipaddress
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace

from kubernetes_asyncio.client.exceptions import ApiException
from sqlalchemy import delete, select

from app import settings_store
from app.catalog import placement
from app.catalog.sources import model_sources
from app.deployments import schedule, service
from app.deployments.service import NodeCandidate
from app.k8s import A_GROUP_HASH, A_SPEC_HASH, L_DEPLOYMENT, L_NODE_DEPLOYMENT, K8sUnavailable, NodeInfo
from app.models import ControllerTimer, NodeAssignment, NodeModels, NodeWake, StandaloneNode
from app.nodes import cleanup, identity, registry, relay, wake
from app.workloads import builders
from app.workloads.builders import RpcWorker
from app.nodes import versions

log = logging.getLogger(__name__)

_ACCELS = ("cpu", "nvidia")
_K8S_ERRORS = (K8sUnavailable, ApiException)
Key = tuple[str, str]  # (kind, name)
_now = time.time  # wall clock: the pauses are kept in the database (ControllerTimer) and outlive a restart
_NOT_READY, _LOST = "notready:", "lost:"
# container starts come from each node's clock, the time a loss was seen from main's: a start this close to another
# clock's moment counts as a restart, the safe side (a restart instead of a group stuck on a dead connection)
CLOCK_SKEW_S = 120


@dataclass
class _Lost:
    """A split group that misses a member which is still assigned to it."""

    members: tuple[str, ...]
    since: float  # time the loss was first seen (`_now`)
    leader_lost: bool = False  # the leader's node was among the missing


@dataclass
class _Plan:
    """What one pass changes besides the labels."""

    desired: dict[Key, dict] = field(default_factory=dict)  # every workload object that should exist
    recycle: list[str] = field(default_factory=list)  # pods to delete so that their controller starts fresh ones
    hold: set[Key] = field(default_factory=set)  # split leaders left exactly as they are
    force: list[str] = field(default_factory=list)  # leader pods on a down node, deleted without a grace period
    # split leaders that must get no requests (`app_state.held_split_leaders`, read by the backend controller): held
    # ones may hang in a recv on a dead rpc connection, restarted ones are about to lose theirs
    unserved: set[str] = field(default_factory=set)


def _annotations(manifest: dict) -> dict:
    return manifest["spec"]["template"]["metadata"]["annotations"]


def _stale(owned, manifest: dict) -> bool:
    """The cluster object is missing or was built from a different spec/group."""
    if owned is None:
        return True
    annotations = _annotations(manifest)
    return owned.spec_hash != annotations[A_SPEC_HASH] or owned.group_hash != annotations.get(A_GROUP_HASH)


def _leader_index(deployment: str, pod) -> int | None:
    """The group index of a pod of the leader Deployment `llm-<deployment>-g<i>`, read from the object the pod
    belongs to: its own name is cut short for long deployment names (generateName)."""
    match = re.fullmatch(rf"llm-{re.escape(deployment)}-g(\d+)", pod.workload or "")
    return int(match.group(1)) if match else None


def _crash_looping(pod) -> bool:
    return not pod.ready or (pod.message or "").startswith("CrashLoopBackOff")


def _restarted(started_at: float | None, moment: float, skew: float = CLOCK_SKEW_S) -> bool:
    """The container started after `moment`, or within `skew` before it (another clock)."""
    return started_at is not None and started_at > moment - skew


def _ran_through(started_at: float | None, moment: float) -> bool:
    """The container surely ran through `moment` (another clock): not started within CLOCK_SKEW_S after it."""
    return started_at is None or started_at <= moment + CLOCK_SKEW_S


def _missing_accels(members: list[str], missing: list[str], node_accel: dict[str, str]) -> Counter | None:
    """accel -> how many of the `missing` members have it; a member whose node is gone takes the accel the other
    members share. None when that is unknown (a mixed group, or no member left in the cluster)."""
    shared = {node_accel[m] for m in members if m in node_accel}
    need: Counter = Counter()
    for m in missing:
        accel = node_accel.get(m) or (next(iter(shared)) if len(shared) == 1 else None)
        if accel is None:
            return None
        need[accel] += 1
    return need


@dataclass
class _Waking:
    """A down node being woken (Wake-on-LAN): seen down since `since` (`_now`), `tries` magic packets sent."""

    since: float
    tries: int = 0


@dataclass
class _Purge:
    """A cleanup of a node's models volume (task 2-26) that is waiting for its time or has been handed to the node: kept
    in the database (`purge:<node>`) so that a restart of the controllers neither forgets the idle time nor issues the
    cleanup twice."""

    since: float  # the cleanup became due (`_now`); MODEL_CACHE_IDLE_S counts from here
    sig: cleanup.Signature  # what the volume is cleaned for
    ref: str = ""  # the Job (k3s) or the order (standalone) handed to the node, empty before
    issued_at: float = 0.0
    not_before: float = 0.0  # a cleanup that failed is not repeated before
    source: str = ""

    def dump(self) -> str:
        return json.dumps(
            {"sig": self.sig.dump(), "ref": self.ref, "at": self.issued_at, "nb": self.not_before, "src": self.source}
        )

    @staticmethod
    def load(since: float, text: str) -> "_Purge | None":
        try:
            raw = json.loads(text)
            sig = cleanup.Signature.load(raw["sig"])
            if sig is None:
                return None
            return _Purge(since, sig, str(raw.get("ref", "")), float(raw.get("at", 0)), float(raw.get("nb", 0)), str(raw.get("src", "")))
        except (ValueError, KeyError, TypeError):
            return None


CLEANUP_RETRY_S = 300.0  # a failed cleanup is tried again after this long (at least)


def _url_host(url: str) -> str | None:
    host = url.removeprefix("http://").rpartition(":")[0]
    return host or None


class WorkloadController:
    def __init__(self, app_state):
        self.app_state = app_state
        self._meta_backoff: dict[int, tuple[float, float]] = {}
        self._not_ready_since: dict[str, float] = {}  # node name -> time it was first seen NotReady
        self._lost: dict[tuple[str, int], _Lost] = {}  # (deployment, group index) -> the loss being waited out
        self._unplaced: set[tuple] = set()  # groups whose missing placement was logged (logged once, not every pass)
        self._repos: str | None = None  # the repo list last written to the ConfigMap llm-model-repos
        self._waking: dict[str, _Waking] = {}  # node name -> its Wake-on-LAN attempts
        self._cached: dict[str, tuple[float, cleanup.Signature]] = {}  # node -> what its models volume was last cleaned for
        self._purging: dict[str, _Purge] = {}  # node -> the cleanup of its models volume that is due or running

    async def reconcile_once(self) -> None:
        state = self.app_state
        # The pods run the node image pinned by the digest main's registry reports (task 2-30). Built before the registry has
        # answered (the main container is starting), they would carry the bare tag and be re-templated a minute later: every
        # model would load twice. A main without a registry gets its pods after versions.PIN_WAIT_S.
        if not await versions.refresh_pins(state):
            log.info("workloads: the registry of main has not answered yet, the pods wait")
            return
        try:
            nodes = [n for n in await state.k8s.list_nodes() if not n.is_main]
        except _K8S_ERRORS as exc:
            log.warning("workloads: cluster unavailable, nothing changed: %r", exc)
            return
        async with state.sessionmaker() as db:
            await service.purge_standalone(db, nodes, state.settings.standalone_purge_s)
            await self._load_timers(db)
            await service.refresh_missing_meta(db, model_sources(state), self._meta_backoff)
            # only nodes the admin approved serve (app.nodes.identity): an unapproved one gets no assignment, no label, no pods
            required = await identity.approval_required(state)
            live_standalone = await service.live_nodes(db, state.settings.standalone_ttl_s)
            approved = await identity.approved_names(
                db, [n.name for n in nodes] + [sn.name for sn in live_standalone], required
            )
            candidates = [
                NodeCandidate(n.name.lower(), n.accel, "k8s", n.vram_mib, n.mem_mib)
                for n in nodes
                if n.ready and n.accel and n.name.lower() in approved
            ]
            for sn in live_standalone:
                if sn.name.lower() in approved:
                    candidates.append(NodeCandidate(sn.name.lower(), sn.accel, "standalone", sn.vram_mib, sn.ram_mib))
            await service.auto_assign(db, candidates)
            every = await service.list_all(db)
            rows = (await db.scalars(select(NodeAssignment).where(NodeAssignment.deployment_id.is_not(None)))).all()
            standalone = (await db.scalars(select(StandaloneNode))).all()
            live_rows = await service.live_nodes(db, state.settings.standalone_ttl_s)
            live = {sn.name for sn in live_rows}
            nics = {w.node_name: w for w in (await db.scalars(select(NodeWake))).all()}
            models = {m.node_name: m.models_bytes for m in (await db.scalars(select(NodeModels))).all()}
        timers_before = self._timer_state()
        now = schedule.now_utc()
        drain = state.settings.schedule_drain_s
        # past the end of its window a deployment keeps its pods for SCHEDULE_DRAIN_S (out of LiteLLM already)
        deployments = [d for d in every if d.enabled and schedule.running(d.schedule, now, drain)]
        by_name = {r.node_name: r for r in rows}
        roster: dict[int, set[str]] = {}  # deployment id -> nodes assigned to it, on maintenance or away included
        for r in rows:
            if r.node_name in approved:
                roster.setdefault(r.deployment_id, set()).add(r.node_name)
        node_dep = {}
        for n in nodes:
            if n.name.lower() not in approved:
                continue
            dep = service.effective_deployment(by_name.get(n.name.lower()), every, n.accel, "k8s", now, drain)
            if dep is not None:
                node_dep[n.name.lower()] = dep
        labels = self._labels(nodes, node_dep)
        down = [(n.name.lower(), n.accel, "k8s", n.external_ip or n.internal_ip) for n in nodes if not n.ready]
        # standalone: a stale or stopped record, or no record at all any more (purged after STANDALONE_PURGE_S): the
        # reported NIC and the assignment are enough, so a PC switched off over the weekend is woken on Monday
        records = {sn.name.lower(): sn for sn in standalone}
        in_cluster = {n.name.lower() for n in nodes} | {"llm-main"}
        for name in sorted((set(records) | set(nics)) - in_cluster - {x.lower() for x in live}):
            sn = records.get(name)
            down.append((name, sn.accel if sn else None, "standalone", _url_host(sn.url) if sn else None))
        relays = [r for r in relay.candidates(nodes, live_rows, nics) if r.name in approved]
        await self._wake(down, nics, by_name, every, now, relays)
        await self._cleanup(nodes, live_rows, set(records), by_name, every, now, models)
        try:
            owned = {(o.kind, o.name): o for o in await state.k8s.list_owned()}
            leader_pods = {
                d.name: [p for p in await state.k8s.list_pods(d.name) if p.role == builders.ROLE_LEADER]
                for d in every
                if d.mode == "split"
            }
            plan = await self._desired(deployments, nodes, node_dep, list(owned.values()), roster, leader_pods)
            state.held_split_leaders = plan.unserved
            await self._execute(nodes, labels, plan, owned)
            await self._publish_repos(every)
        except _K8S_ERRORS as exc:
            log.warning("workloads: cluster unavailable, reconcile aborted: %r", exc)
        await self._save_timers(timers_before)

    # ---- pauses kept in the database ----------------------------------------------------

    async def _load_timers(self, db) -> None:
        self._not_ready_since, self._lost, self._waking, self._cached, self._purging = {}, {}, {}, {}, {}
        for t in (await db.scalars(select(ControllerTimer))).all():
            if t.key.startswith(cleanup.PURGE_TIMER_PREFIX):
                purge = _Purge.load(t.since, t.members)
                if purge is not None:
                    self._purging[t.key.removeprefix(cleanup.PURGE_TIMER_PREFIX)] = purge
            elif t.key.startswith(cleanup.CACHE_TIMER_PREFIX):
                sig = cleanup.Signature.load(t.members)
                if sig is not None:
                    self._cached[t.key.removeprefix(cleanup.CACHE_TIMER_PREFIX)] = (t.since, sig)
            elif t.key.startswith(wake.TIMER_PREFIX):
                tries = int(t.members) if t.members.isdigit() else 0
                self._waking[t.key.removeprefix(wake.TIMER_PREFIX)] = _Waking(t.since, tries)
            elif t.key.startswith(_NOT_READY):
                self._not_ready_since[t.key.removeprefix(_NOT_READY)] = t.since
            elif t.key.startswith(_LOST):
                dep, _, index = t.key.removeprefix(_LOST).rpartition(":")
                members = tuple(t.members.split(",")) if t.members else ()
                self._lost[(dep, int(index))] = _Lost(members, t.since, t.leader_lost)

    def _timer_state(self) -> dict[str, tuple[float, str, bool]]:
        state = {f"{_NOT_READY}{node}": (since, "", False) for node, since in self._not_ready_since.items()}
        for (dep, index), lost in self._lost.items():
            state[f"{_LOST}{dep}:{index}"] = (lost.since, ",".join(lost.members), lost.leader_lost)
        for node, w in self._waking.items():
            state[f"{wake.TIMER_PREFIX}{node}"] = (w.since, str(w.tries), False)
        for node, (at, sig) in self._cached.items():
            state[f"{cleanup.CACHE_TIMER_PREFIX}{node}"] = (at, sig.dump(), False)
        for node, purge in self._purging.items():
            state[f"{cleanup.PURGE_TIMER_PREFIX}{node}"] = (purge.since, purge.dump(), False)
        return state

    async def _save_timers(self, before: dict[str, tuple[float, str, bool]]) -> None:
        after = self._timer_state()
        if after == before:
            return
        async with self.app_state.sessionmaker() as db:
            await db.execute(delete(ControllerTimer))
            db.add_all(
                ControllerTimer(key=key, since=since, members=members, leader_lost=leader_lost)
                for key, (since, members, leader_lost) in after.items()
            )
            await db.commit()

    # ---- Wake-on-LAN ---------------------------------------------------------------------

    async def _wake(
        self, down: list[tuple[str, str | None, str, str | None]], nics: dict[str, NodeWake], by_name, every, now,
        relays: list[wake.Relay],
    ) -> None:
        """Magic packets for the down nodes (name, accel, source, last address) that are needed now: the node runs a
        deployment at this moment unless it is down (`service.effective_deployment`: not on maintenance, the
        deployment enabled and inside its working window, or the node lent to another one) and reported its NIC.
        The first attempt WOL_DELAY_S after the node was first seen so, then with growing pauses
        (`wake.attempt_due_at`), WOL_MAX_TRIES in all; the count stays until the node is back, so the pages show it
        gave up. A node that came back, or is not needed any more, starts over the next time.

        Main sends every attempt; with WOL_VIA_NODE (`wake.via_node`) the first live node by name of the PC's subnet
        (`relays`) also sends it on its own network (app.nodes.relay)."""
        s = self.app_state.settings
        live = await settings_store.live_many(self.app_state, ("wol_enabled", "wol_delay_s", "wol_max_tries"))
        if not live["wol_enabled"]:
            self._waking.clear()
            return
        t = _now()
        needed: set[str] = set()
        for name, accel, source, last_ip in down:
            nic = nics.get(name)
            if nic is None or service.effective_deployment(by_name.get(name), every, accel, source, now) is None:
                continue
            needed.add(name)
            w = self._waking.setdefault(name, _Waking(t))
            if w.tries >= live["wol_max_tries"] or t < wake.attempt_due_at(
                w.since, w.tries, live["wol_delay_s"], s.wol_retry_s
            ):
                continue
            w.tries += 1
            addrs = wake.targets(nic.ip, nic.prefix, s.wol_overrides, (last_ip or nic.ip) if s.wol_unicast else None)
            errors = wake.send(nic.mac, addrs)
            log.info(
                "wake-on-lan: node %s down since %.0f s, attempt %d of %d: magic packet for %s to %s (UDP %s)",
                name, t - w.since, w.tries, live["wol_max_tries"], nic.mac, ", ".join(addrs) or "nowhere",
                ", ".join(map(str, wake.PORTS)),
            )
            for e in errors:
                log.warning("wake-on-lan: node %s: send to %s failed", name, e)
            if wake.via_node(s.wol_via_node, s.wol_via_node_after, w.tries):
                await self._wake_via_node(name, nic, relays)
        for name in set(self._waking) - needed:
            del self._waking[name]

    async def _wake_via_node(self, name: str, nic: NodeWake, relays: list[wake.Relay]) -> None:
        broadcast = wake.directed_broadcast(nic.ip, nic.prefix)
        r = wake.pick_relay(name, nic.ip, nic.prefix, relays)
        if r is None or broadcast is None:
            subnet = f"{nic.ip}/{nic.prefix}"
            if broadcast is not None:
                subnet = str(ipaddress.IPv4Network(subnet, strict=False))
            log.info("wake-on-lan: node %s: no live node in %s to send the packet from", name, subnet)
            return
        how = "a Job on it" if r.source == "k8s" else "its next heartbeat"
        error = await relay.send_via(self.app_state, r, name, nic.mac, broadcast)
        if error:
            log.warning("wake-on-lan: node %s: the packet via node %s (%s) failed: %s", name, r.name, how, error)
        else:
            log.info("wake-on-lan: node %s: magic packet via node %s (%s) to %s", name, r.name, how, broadcast)

    # ---- cleanup of model weights ----------------------------------------------------------

    async def _cleanup(
        self, nodes: list[NodeInfo], live_rows, records: set[str], by_name, every, now, models: dict[str, int]
    ) -> None:
        """Removes the model files and the tensor cache a node does not need (task 2-26, app.nodes.cleanup).

        What a node needs is decided by the deployment it is assigned to (`cleanup.node_deployments`: the working window
        does not matter, a deployment that is deleted or disabled does not count) plus the one it is lent to right now.
        Per node, `cleanup.Signature` tells what its volume is cleaned for, and it is remembered after every cleanup:
        - a node without a deployment is cleaned of everything MODEL_CACHE_IDLE_S after it was seen so;
        - a node whose deployment changed the files it needs is cleaned at once, down to what the new one uses; the tensor
          cache stays when the node is still in a split group of the same model, a different split model (or none) takes
          it away;
        - the first time a node with a deployment is seen nothing is known to clean: the signature is only remembered.
        A node that is not live waits (its time keeps running). A cleanup is a Job on a k3s node and an order in the
        heartbeat reply of a standalone node; a failed one is tried again after CLEANUP_RETRY_S."""
        s = self.app_state.settings
        t = _now()
        in_cluster = {n.name.lower() for n in nodes}
        candidates = [(n.name.lower(), n.accel, "k8s", n.ready) for n in nodes]
        candidates += [
            (sn.name.lower(), sn.accel, "standalone", True) for sn in live_rows if sn.name.lower() not in in_cluster
        ]
        present = in_cluster | records
        for name in set(self._purging) - present:
            del self._purging[name]
        for name in set(self._cached) - present:
            del self._cached[name]
        for name, accel, source, ready in sorted(candidates, key=lambda c: c[0]):
            assigned, needed = cleanup.node_deployments(by_name.get(name), every, accel, source, now, s.schedule_drain_s)
            await self._purge_step(name, accel, source, ready, assigned, needed, models.get(name), t)

    async def _purge_step(self, name, accel, source, ready, assigned, needed, models_bytes, t) -> None:
        idle_s = await settings_store.live(self.app_state, "model_cache_idle_s")  # the panel's value, no restart
        target = cleanup.signature(assigned)
        purge = self._purging.get(name)
        if purge is not None and purge.ref:  # handed to the node: see how it ended
            outcome = await cleanup.poll(self.app_state, name, purge.source or source, purge.ref, purge.issued_at, t)
            if outcome.state == "running":
                return
            if outcome.state == "failed":
                log.warning("models cleanup of node %s failed: %s", name, outcome.error)
                self._purging[name] = replace(purge, ref="", not_before=t + max(idle_s, CLEANUP_RETRY_S))
                return
            log.info(
                "models cleanup of node %s done: %d bytes freed%s",
                name, outcome.freed_bytes, f", skipped {', '.join(outcome.skipped)}" if outcome.skipped else "",
            )
            self._cached[name] = (t, purge.sig)
            del self._purging[name]
            if outcome.models_bytes is not None:
                async with self.app_state.sessionmaker() as db:
                    await registry.set_models(db, name, outcome.models_bytes)
                    await db.commit()
        cached = self._cached.get(name)
        stored = cached[1] if cached else None
        if stored == target:
            self._purging.pop(name, None)
            return
        if stored is None and target != cleanup.IDLE:  # first sight of a node with a deployment: nothing known to clean
            self._cached[name] = (t, target)
            self._purging.pop(name, None)
            return
        purge = self._purging.get(name)
        if purge is None or purge.sig != target:
            purge = self._purging[name] = _Purge(t, target)
        delay = idle_s if target == cleanup.IDLE else 0.0
        if not ready or t < purge.since + delay or t < purge.not_before:
            return
        if target == cleanup.IDLE and models_bytes == 0:  # the node reported an empty volume: nothing to remove
            self._cached[name] = (t, target)
            del self._purging[name]
            return
        # the tensor cache stays for a node still in a split group of the model it was filled for, and for a lent split one
        rpc = any(d.mode == "split" for d in needed if d is not assigned) or (
            bool(target.rpc) and stored is not None and stored.rpc == target.rpc
        )
        ref, error = await cleanup.send(self.app_state, name, source, accel, cleanup.keep_for(needed, rpc=rpc))
        if ref is None:
            log.warning("models cleanup of node %s not sent: %s", name, error)
            self._purging[name] = replace(purge, not_before=t + CLEANUP_RETRY_S)
            return
        log.info(
            "models cleanup of node %s sent (%s): %s",
            name, "no deployment" if target == cleanup.IDLE else "the model of its deployment changed", ref,
        )
        self._purging[name] = replace(purge, ref=ref, issued_at=t, source=source)

    # ---- the pass ----------------------------------------------------------------------

    def _labels(self, nodes: list[NodeInfo], node_dep: dict) -> dict[str, str | None]:
        """The deployment label each node should carry.

        A NotReady node is never given a new label and keeps its current one only for the regroup grace of its
        deployment (`service.regroup_grace`): a labelled node that stays down (a switched-off PC) would stall every
        rolling update of the DaemonSet. The assignment in the database stays, so the node is labelled again once it
        is Ready.
        """
        settings = self.app_state.settings
        now = _now()
        for name in set(self._not_ready_since) - {n.name for n in nodes}:
            del self._not_ready_since[name]
        labels: dict[str, str | None] = {}
        for node in nodes:
            dep = node_dep.get(node.name.lower())
            wanted = dep.name if dep is not None else None
            if node.ready:
                self._not_ready_since.pop(node.name, None)
            else:
                since = self._not_ready_since.setdefault(node.name, now)
                if dep is None or wanted != node.deployment or now - since >= service.regroup_grace(dep, settings):
                    wanted = None
            labels[node.name] = wanted
        return labels

    async def _desired(
        self, deployments, nodes: list[NodeInfo], node_dep: dict, owned: list, roster: dict[int, set[str]], leader_pods
    ) -> _Plan:
        """Every workload object that should exist, the pods to recycle or force-delete and the split leaders to leave
        exactly as they are.

        Only reads the cluster. Only rpc servers of the rpc DaemonSet's current template are workers: a leader never
        gets a `--tensor-split` against servers of two templates while the DaemonSet rolls. A split group that lost a
        member still assigned to it is rebuilt at once when a spare
        worker of the deployment with the accel of its members can replace the member (spare: alive, in no group and
        not forming one this pass), else it waits for the regroup grace (`_held`). After that, or at once when the
        member was taken off the deployment, the group is dissolved: the rpc servers of its surviving members may
        still be blocked on the dead leader's connection (ggml-rpc-server serves one client and has no keepalive), so
        their pods are recreated, and those nodes join a new group on a later pass. A group formed anew
        always gets a fresh index, a new leader Deployment: rewriting the old one would wait behind its pod (strategy
        Recreate), which hangs in Terminating on a node that is down. Such pods are force-deleted. With `spare_nodes`
        the number of groups is capped by the deployment's roster (its assigned nodes, those on maintenance or away
        included, plus nodes lent to it), so spare nodes replace lost ones at once.

        A group whose lost members are all back (`_recover`) stays as it is unless a process lost its peer meanwhile.
        """
        s = self.app_state.settings
        plan = _Plan()
        seen: set[tuple[str, int]] = set()
        memory = {n.name.lower(): service.node_memory(n) for n in nodes}
        node_accel = {n.name: n.accel for n in nodes if n.accel}
        for dep in deployments:
            assigned = [n for n in nodes if node_dep.get(n.name.lower()) is dep]
            accels = [a for a in _ACCELS if any(n.accel == a for n in assigned)]
            if dep.mode == "split":
                rpc_hash: dict[str, str] = {}
                for accel in accels:
                    limit = placement.rpc_memory_limit_mib(
                        dep.meta, list(memory.values()), accel, dep.use_node_ram, dep.ram_budget_pct
                    )
                    manifest = builders.rpc_daemonset(dep, accel, s, limit)
                    rpc_hash[accel] = _annotations(manifest)[A_SPEC_HASH]
                    self._add(plan.desired, manifest)
                assigned_names = {n.name for n in assigned}
                endpoints = [
                    ep
                    for ep in await self.app_state.k8s.list_endpoints(builders.ROLE_RPC, deployment=dep.name)
                    if ep.node_name in assigned_names and ep.accel in _ACCELS and ep.spec_hash == rpc_hash.get(ep.accel)
                ]
                workers = [
                    RpcWorker(ep.node_name, ep.ip, ep.port, ep.accel, getattr(memory.get(ep.node_name), "ram_mib", None))
                    for ep in endpoints
                ]
                alive = {w.node_name for w in workers}
                existing = builders.leader_groups(owned, dep.name)
                seen.update((dep.name, i) for i in existing)
                in_groups = {n for names in existing.values() for n in names}
                broken = {i for i, names in existing.items() if not set(names) <= alive}
                whole = {i: names for i, names in existing.items() if i not in broken}
                group_limit = service.split_group_limit(dep, len(roster.get(dep.id, set()) | assigned_names))
                pods = [(p, _leader_index(dep.name, p)) for p in leader_pods.get(dep.name, [])]
                # an index is never reused while its leader object or a pod of it is still there
                reserved = set(existing) | {i for _, i in pods if i is not None}
                # spare workers: those left over even if every broken group waits, not those that form a group anyway
                limit = None if group_limit is None else max(group_limit - len(broken), 0)
                mixed = dep.accel == "any"  # GPU and CPU nodes in one group
                forming = builders.plan_groups(workers, dep.group_size, whole, in_groups, limit, reserved, mixed)
                grouped = in_groups | {w.node_name for g in forming.values() for w in g}
                free = Counter(w.accel for w in workers if w.node_name not in grouped)
                held, recovered = self._held(dep, existing, alive, assigned_names, free, node_accel)
                plan.hold.update(("Deployment", builders.object_name(dep, f"g{i}")) for i in held)
                plan.unserved.update(builders.object_name(dep, f"g{i}") for i in held)
                waiting = {n for names in held.values() for n in names}
                live = {i: names for i, names in existing.items() if i not in held}
                limit = None if group_limit is None else max(group_limit - len(held), 0)
                planned = builders.plan_groups(workers, dep.group_size, live, waiting, limit, reserved, mixed)
                intact = {i for i, g in planned.items() if [w.node_name for w in g] == live.get(i)}
                kept = {n for i in intact for n in live[i]}
                survivors = {n for names in live.values() for n in names if n in alive and n not in kept}
                restart: set[str] = set()
                relaunch: set[str] = set()
                for i, lost in recovered.items():
                    if i in intact:
                        members, leaders = self._recover(lost, live[i], [p for p, j in pods if j == i], endpoints)
                        restart |= members
                        relaunch |= leaders
                        if members or leaders:
                            plan.unserved.add(builders.object_name(dep, f"g{i}"))
                plan.recycle += sorted(ep.pod_name for ep in endpoints if ep.node_name in survivors | restart and ep.pod_name)
                plan.recycle += sorted(relaunch)
                exclude = survivors | waiting
                for i, group in builders.plan_groups(workers, dep.group_size, live, exclude, limit, reserved, mixed).items():
                    decision, tensor_split = self._placement(dep, i, group, memory)
                    if decision != placement.HOLD:
                        self._add(plan.desired, builders.split_leader(dep, i, group, s, tensor_split))
            elif dep.engine == "vllm":
                if "nvidia" in accels:
                    self._add(plan.desired, builders.vllm_daemonset(dep, s))
            else:
                for accel in accels:
                    self._add(plan.desired, builders.replicas_daemonset(dep, accel, s))
        for key in set(self._lost) - seen:
            del self._lost[key]
        ready = {n.name for n in nodes if n.ready}
        for name, pods in leader_pods.items():
            for p in pods:
                key = ("Deployment", p.workload)
                if (
                    _leader_index(name, p) is not None
                    and p.node_name not in ready
                    and key not in plan.desired
                    and key not in plan.hold
                ):
                    plan.force.append(p.name)
        return plan

    def _placement(
        self, dep, index: int, group: list[RpcWorker], memory: dict[str, placement.NodeMemory]
    ) -> tuple[str, list[int] | None]:
        """`placement.decide` for one group and the `--tensor-split` it gets: SPLIT with the plan, AUTO without
        (llama.cpp splits by free memory), HOLD (a group with RAM devices that does not fit: no leader, the status page
        says why). Anything but SPLIT is logged once per group."""
        nodes = [memory.get(w.node_name) or placement.NodeMemory(w.node_name, w.accel, None, None) for w in group]
        plan = placement.plan_for(dep, nodes, self.app_state.settings.gpu_compute_reserve_mib)
        decision = placement.decide(plan)
        key = (dep.name, index, tuple(w.node_name for w in group))
        if decision == placement.SPLIT:
            self._unplaced.discard(key)
            return decision, plan.tensor_split
        if key not in self._unplaced:
            self._unplaced.add(key)
            if decision == placement.HOLD:
                log.warning("deployment %s group %d: leader not started, the model does not fit", dep.name, index)
            else:
                log.warning("deployment %s group %d: no layer placement, llama.cpp splits by itself", dep.name, index)
        return decision, None

    def _held(
        self,
        dep,
        existing: dict[int, list[str]],
        alive: set[str],
        assigned: set[str],
        free: Counter,
        node_accel: dict[str, str],
    ) -> tuple[dict[int, list[str]], dict[int, _Lost]]:
        """The groups of `dep` waiting out the regroup grace for a lost member, and the groups whose lost members are
        all back (with the loss they recovered from).

        A group is held while it misses a member that is still assigned to the deployment (a rebooting PC, a
        restarting rpc pod), no `free` worker (accel -> count of spare workers: alive, in no group and in no group
        formed this pass) of the accel of each missing member can take its place (a mixed group of accel `any` needs a
        GPU node for a GPU member) and the grace has not run out. A member taken off on purpose (maintenance, another
        assignment) or a changed group size is not waited for.
        """
        grace = service.regroup_grace(dep, self.app_state.settings)
        now = _now()
        held: dict[int, list[str]] = {}
        recovered: dict[int, _Lost] = {}
        for index, members in sorted(existing.items()):
            key = (dep.name, index)
            lost = self._lost.get(key)
            if lost is not None and lost.members != tuple(members):  # a new group took the index
                del self._lost[key]
                lost = None
            missing = [m for m in members if m not in alive]
            if not missing:
                if lost is not None:
                    del self._lost[key]
                    recovered[index] = lost
                continue
            if len(members) != dep.group_size or not set(members) <= assigned:
                self._lost.pop(key, None)
                continue
            need = _missing_accels(members, missing, node_accel)
            if need is not None and all(free[a] >= n for a, n in need.items()):  # spare workers replace them now
                free.subtract(need)
                self._lost.pop(key, None)
                continue
            if lost is None:
                lost = self._lost[key] = _Lost(tuple(members), now)
            lost.leader_lost = lost.leader_lost or members[0] in missing
            if now - lost.since < grace:
                held[index] = members
        return held, recovered

    @staticmethod
    def _recover(lost: _Lost, members: list[str], leader_pods: list, endpoints: list) -> tuple[set[str], set[str]]:
        """The members whose rpc pods are recreated and the leader pods that are, for a group whose lost members are
        all back. A short NotReady blip (a lost lease, a slow probe) restarts nothing: the processes ran through it.

        Containers are told apart by their start (`started_at`, the node's clock) against the time the loss was seen:
        - the leader's node was lost and its leader did not run through the outage (started after it, or not
          running): the other members' rpc servers that did run through it may still wait on the old leader's
          connection (ggml-rpc-server serves one client and has no keepalive), they are recreated;
        - an rpc server started after the running leader connected (after the loss, and after the leader's own
          start): the leader holds a dead connection and its first request would end in GGML_ABORT, it is recreated;
        - otherwise a leader that crash-looped while a member was away starts at once, not after its back-off.
        A start within CLOCK_SKEW_S of a moment of another clock is taken the way that restarts rather than leaves a
        group stuck; the leader and the rpc server of its own node share one clock.
        """
        rpc_start = {ep.node_name: ep.started_at for ep in endpoints}
        leader_start = max((p.started_at for p in leader_pods if p.started_at is not None), default=None)
        restart: set[str] = set()
        if lost.leader_lost and (leader_start is None or _restarted(leader_start, lost.since)):
            restart = {n for n in members[1:] if _ran_through(rpc_start.get(n), lost.since)}
        if leader_start is not None and any(
            _restarted(rpc_start.get(n), lost.since)
            and _restarted(rpc_start.get(n), leader_start, 0 if n == members[0] else CLOCK_SKEW_S)
            for n in members
        ):
            return restart, {p.name for p in leader_pods}
        if not lost.leader_lost:
            return restart, {p.name for p in leader_pods if _crash_looping(p)}
        return restart, set()

    async def _publish_repos(self, every) -> None:
        """The repos of every deployment, disabled ones included (they may be switched on again), for the cache
        cleanup of serve-llama.sh (KEEP_MODEL_REPOS); written when the list changed or the last write failed."""
        repos = ",".join(sorted({r for d in every for r in builders.deployment_repos(d)}))
        if repos == self._repos:
            return
        try:
            await self.app_state.k8s.put_config_map(builders.MODEL_REPOS_CONFIGMAP, {builders.MODEL_REPOS_KEY: repos})
        except ApiException as exc:
            log.warning("workloads: write of ConfigMap %s refused (%s): %s", builders.MODEL_REPOS_CONFIGMAP, exc.status, exc.reason)
            return
        self._repos = repos

    @staticmethod
    def _add(desired: dict[Key, dict], manifest: dict) -> None:
        desired[(manifest["kind"], manifest["metadata"]["name"])] = manifest

    async def _execute(self, nodes: list[NodeInfo], labels: dict[str, str | None], plan: _Plan, owned) -> None:
        """Class taints, labels, then deletions (but not of held leaders), then leader pods stuck on a down node, then
        pods to recycle, then creations/updates (a freed host port lets the next pod start)."""
        k8s = self.app_state.k8s
        for node in nodes:  # never llm-main: reconcile_once filters it out
            if not node.class_taint:
                await self._guarded(f"class taint of node {node.name}", k8s.add_class_taint(node.name))
        for node in nodes:
            if node.deployment != labels[node.name]:
                await self._guarded(f"label of node {node.name}", k8s.patch_node_labels(node.name, {L_NODE_DEPLOYMENT: labels[node.name], L_DEPLOYMENT: None}))
        for kind, name in [key for key in owned if key not in plan.desired and key not in plan.hold]:
            await self._guarded(f"delete of {kind} {name}", k8s.delete(kind, name))
        for pod in plan.force:
            await self._guarded(f"force delete of leader pod {pod}", k8s.delete_pod(pod, grace_period_seconds=0))
        for pod in plan.recycle:
            await self._guarded(f"recycle of pod {pod}", k8s.delete_pod(pod))
        for key, manifest in plan.desired.items():
            if _stale(owned.get(key), manifest):
                await self._guarded(f"apply of {key[0]} {key[1]}", k8s.apply(manifest))

    @staticmethod
    async def _guarded(what: str, call) -> None:
        """An apiserver refusal for one object must not block the others; an unreachable cluster aborts the pass."""
        try:
            await call
        except ApiException as exc:
            log.warning("workloads: %s refused (%s): %s", what, exc.status, exc.reason)
