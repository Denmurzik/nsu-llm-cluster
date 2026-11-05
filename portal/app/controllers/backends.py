"""Backend controller: keeps LiteLLM's model list equal to the set of backends that answer `/health`.

Candidates are Kubernetes inference pods and split leaders (Ready, with the port/model annotations; not the leaders
the workload controller names in `app_state.held_split_leaders`)
plus live standalone nodes that already run the config the portal wants from them. Every candidate
is probed over HTTP; pod readiness alone is not trusted, so a node that dies leaves LiteLLM within
one controller interval plus the probe timeout. Entries are only deleted on positive evidence: when
LiteLLM or the cluster cannot be queried nothing that depends on that data is touched.

Every entry carries `max_parallel_requests` (see `max_parallel_requests()`), so a backend's queue stays
bounded and an overloaded model answers 429 instead of timing out. It also carries the deployment's gateway-side
settings in `model_info`: `access_groups` (`experimental` or `public`, who may call it) and `system_prompt`
(added by main/litellm/system_prompt_hook.py to chats without one). Neither is in the entry's id or in the pods'
spec hash, so a change of either is applied to the live entries in place, without a restart. The status of the last round is kept
in `app_state.backend_status` and published to Redis for the API workers (another process).

With several LiteLLM workers `/model/info` is answered by one of them from its own router, which learns
about writes made through another worker only after the Redis change notice (debounce 1 s, jitter up to
5 s, at most one resync per 10 s). So the controller remembers what it wrote: for `recent_window_s` an
entry it added or updated is not added or updated again, and one it deleted is not deleted again, whatever
a stale listing says. A dead backend is still removed at once, even right after it was added.
"""

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import httpx
from kubernetes_asyncio.client import ApiException

from app.deployments import schedule, service
from app.deps import REDIS_ERRORS
from app.k8s import MAIN_NODE, K8sUnavailable
from app import key_access
from app.config import key_fingerprint
from app.litellm_client import LiteLLMError, LiteLLMUnavailable, access_groups
from app.nodes import identity

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 3.0
ROLES = ("inference", "split-leader")
BACKEND_STATUS_KEY = "portal:backend_status"
BACKEND_STATUS_TTL_S = 120  # a stopped controllers process leaves no stale status behind
_LOG_EVERY_S = 60.0
_WRITE_ERRORS = (LiteLLMError, LiteLLMUnavailable)


def managed_model_id(api_base: str, model_name: str, max_input_tokens: int | None) -> str:
    return "pm-" + hashlib.sha1(f"{api_base}|{model_name}|{max_input_tokens}".encode()).hexdigest()[:12]


def max_parallel_requests(parallel: int | None, settings) -> int:
    """LiteLLM's per-worker in-flight bound for one backend with `parallel` slots.

    LiteLLM v1.106.6 counts `max_parallel_requests` in each worker process separately (an in-process
    counter, router_utils/client_initalization_utils.py `MaxParallelRequestsLimit`), so every worker gets
    1/LITELLM_WORKERS of the parallel x QUEUE_FACTOR total. The bound is never below `parallel`: the limit
    answers 429 at once (RateLimitErrorRetries 0), and a smaller share would refuse a request while the
    backend still has a free slot, so one worker alone can fill every slot. A backend whose deployment is
    unknown counts as DEFAULT_PARALLEL slots.
    """
    slots = parallel if parallel is not None else settings.default_parallel
    return max(slots, math.ceil(slots * settings.queue_factor / settings.litellm_workers))


@dataclass(frozen=True)
class Candidate:
    model_name: str
    api_base: str
    health_url: str
    source: str  # "k8s" | "standalone"
    node: str
    max_input_tokens: int | None
    parallel: int | None = None  # slots of the backend; None when its deployment is unknown
    system_prompt: str | None = None  # of the deployment (gateway side)
    experimental: bool = False  # of the deployment: only admins' keys reach it
    # the key LiteLLM sends to this backend: its node's own (app.nodes.identity), the shared key of before only for a
    # legacy standalone node that has none yet; never in the id, never shown
    api_key: str = field(default="", repr=False, compare=False)

    @property
    def id(self) -> str:
        return managed_model_id(self.api_base, self.model_name, self.max_input_tokens)

    def view(self) -> dict:
        return {
            "id": self.id,
            "model_name": self.model_name,
            "api_base": self.api_base,
            "source": self.source,
            "node": self.node,
            "max_input_tokens": self.max_input_tokens,
        }


def _gateway_fields(dep) -> tuple[int | None, str | None, bool]:
    """(slots, system prompt, experimental) of a backend's deployment.

    Fails closed: a deployment the database no longer knows (deleted while its pods still drain) is `experimental`.
    The entry stays in LiteLLM, so the drain keeps working for admins' keys, but it never turns `public`, which would
    show the model of a deleted experimental deployment to every user for the length of the drain."""
    if dep is None:
        return None, None, True
    return dep.parallel, dep.system_prompt, bool(dep.experimental)


def _from_endpoint(e, deployments: dict) -> Candidate | None:
    if not e.model_name:
        return None
    root = f"http://{e.ip}:{e.port}"
    return Candidate(
        e.model_name,
        f"{root}/v1",
        f"{root}/health",
        "k8s",
        e.node_name,
        e.max_input_tokens,
        *_gateway_fields(deployments.get(e.deployment)),
    )


async def _probe(http: httpx.AsyncClient, candidate: Candidate) -> bool:
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            return (await http.get(candidate.health_url)).status_code == 200
    except (httpx.HTTPError, OSError, TimeoutError):
        return False


class BackendController:
    def __init__(self, app_state):
        self.app_state = app_state
        # model id -> ("write" | "delete", monotonic time) of this controller's own recent changes
        self._recent: dict[str, tuple[str, float]] = {}
        self._status_warned_at = -_LOG_EVERY_S

    def _remember(self, model_id: str, action: str) -> None:
        self._recent[model_id] = (action, time.monotonic())

    def _recently(self, model_id: str, action: str) -> bool:
        entry = self._recent.get(model_id)
        return (
            entry is not None
            and entry[0] == action
            and time.monotonic() - entry[1] < self.app_state.settings.backend_recent_window_s
        )

    def _forget_old(self) -> None:
        window = self.app_state.settings.backend_recent_window_s
        now = time.monotonic()
        self._recent = {k: v for k, v in self._recent.items() if now - v[1] < window}

    async def _k8s_candidates(self) -> list[Candidate] | None:
        """Ready inference endpoints; None when the cluster cannot be queried."""
        try:
            endpoints = [e for role in ROLES for e in await self.app_state.k8s.list_endpoints(role)]
        except (K8sUnavailable, ApiException) as exc:
            log.warning("backends: cluster unavailable, Kubernetes backends are left as they are: %s", exc)
            return None
        async with self.app_state.sessionmaker() as db:
            deployments = await service.list_all(db)
            keys = await identity.backend_keys(db)
            required = await identity.approval_required(self.app_state)
            approved = await identity.approved_names(db, {e.node_name for e in endpoints}, required)
        by_name = {d.name: d for d in deployments}
        # past the end of its working window a deployment's pods drain (SCHEDULE_DRAIN_S): no new requests for them
        now = schedule.now_utc()
        closed = {d.name for d in deployments if not schedule.active(d.schedule, now)}
        # a split group waiting for a lost member (or whose processes are being restarted) keeps a leader that answers
        # /health while a request would hang in a recv on the dead rpc connection: the workload controller names them
        unserved = getattr(self.app_state, "held_split_leaders", None) or set()
        endpoints = [
            e for e in endpoints
            if e.deployment not in closed and not (e.role == "split-leader" and e.workload in unserved)
        ]
        candidates = []
        for c in (_from_endpoint(e, by_name) for e in endpoints):
            if c is None:
                continue
            # a pod reads its node's own key from the node container (builders.BACKEND_KEY_FILE): a node without one, or not
            # approved (its pods should not be there at all), gets no entry. A split leader on main has main's own key:
            # main is no node, nobody approves it
            if c.node.lower() == MAIN_NODE:
                key = self.app_state.settings.main_backend_key.get_secret_value()
            else:
                key = keys.get(c.node.lower()) if c.node.lower() in approved else None
            if key:
                candidates.append(replace(c, api_key=key))
        return candidates

    async def _standalone_candidates(self) -> list[Candidate]:
        state = self.app_state
        candidates = []
        legacy_key = state.settings.backend_api_key.get_secret_value()
        async with state.sessionmaker() as db:
            by_name = {d.name: d for d in await service.list_all(db)}
            live = await service.live_nodes(db, state.settings.standalone_ttl_s)
            required = await identity.approval_required(state)
            approved = await identity.approved_names(db, [n.name for n in live], required)
            keys = await identity.backend_keys(db)
            for node in live:
                if node.name.lower() not in approved:
                    continue
                desired = await service.desired_for_node(db, node.name)
                if desired is None or node.applied_spec_hash != desired.spec_hash:
                    continue
                _, prompt, experimental = _gateway_fields(by_name.get(desired.deployment))
                root = node.url.rstrip("/")
                candidates.append(
                    Candidate(
                        desired.public_model_name,
                        f"{root}/v1",
                        f"{root}/health",
                        "standalone",
                        node.name,
                        desired.ctx_size // desired.parallel,
                        desired.parallel,
                        prompt,
                        experimental,
                        # the key the heartbeat handed it (app.nodes.routes.heartbeat)
                        keys.get(node.name.lower()) or legacy_key,
                    )
                )
        return candidates

    async def reconcile_once(self) -> None:
        litellm = self.app_state.litellm
        if not await litellm.health():
            return

        k8s_candidates = await self._k8s_candidates()
        candidates = (k8s_candidates or []) + await self._standalone_candidates()
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S, trust_env=False) as http:
            alive = await asyncio.gather(*(_probe(http, c) for c in candidates))
        wanted = {c.id: c for c, ok in zip(candidates, alive, strict=True) if ok}

        existing = {m.id: m for m in await litellm.list_managed_models()}
        keep_k8s = k8s_candidates is None
        settings = self.app_state.settings
        self._forget_old()
        # alive entries registered without their backend's current key (the node got its own, or a new one after a reset)
        # or with another in-flight bound (parallel, QUEUE_FACTOR or LITELLM_WORKERS changed) are updated in place: PATCH
        # keeps the id and the entry stays in the router meanwhile
        for model_id, m in existing.items():
            c = wanted.get(model_id)
            if c is None or self._recently(model_id, "write"):
                continue
            try:
                if c.api_key and m.key_fingerprint != key_fingerprint(c.api_key):
                    await litellm.update_model_key(model_id, c.api_key)
                    self._remember(model_id, "write")
                limit = max_parallel_requests(c.parallel, settings)
                if m.max_parallel_requests != limit:
                    await litellm.update_model_limit(model_id, limit)
                    self._remember(model_id, "write")
                    log.info("backends: %s on %s takes %s requests per LiteLLM worker", m.model_name, m.node, limit)
                if m.access_groups != tuple(access_groups(c.experimental)) or m.system_prompt != (c.system_prompt or ""):
                    await litellm.update_model_access(model_id, c.experimental, c.system_prompt)
                    self._remember(model_id, "write")
                    # the prompt text stays out of the log
                    log.info(
                        "backends: %s on %s is now %s%s",
                        m.model_name,
                        m.node,
                        "experimental" if c.experimental else "public",
                        ", with a system prompt" if c.system_prompt else "",
                    )
            except _WRITE_ERRORS as exc:
                log.warning("backends: cannot update %s on %s: %s", m.model_name, m.node, exc)
        # a new entry first, then the old one goes: a context change never leaves the model without a backend
        for model_id, c in list(wanted.items()):
            # an entry added a moment ago may be missing from a worker's listing that has not synced yet
            if model_id in existing or self._recently(model_id, "write"):
                continue
            try:
                await litellm.add_model(
                    model_id,
                    c.model_name,
                    c.api_base,
                    c.source,
                    c.node,
                    c.max_input_tokens,
                    max_parallel_requests=max_parallel_requests(c.parallel, settings),
                    experimental=c.experimental,
                    system_prompt=c.system_prompt,
                    api_key=c.api_key,
                )
            except _WRITE_ERRORS as exc:
                # v1.106.6 answers a duplicate id (written through another worker) with a generic 500, so a failed
                # add is not retried within the window either: the listing has caught up by then
                log.warning("backends: cannot add %s on %s: %s", c.model_name, c.node, exc)
                self._remember(model_id, "write")
                wanted.pop(model_id)
                continue
            self._remember(model_id, "write")
            log.info("backends: added %s on %s (%s, max %s tokens)", c.model_name, c.node, c.api_base, c.max_input_tokens)
        for model_id, m in existing.items():
            if model_id in wanted or (keep_k8s and m.source == "k8s") or self._recently(model_id, "delete"):
                continue
            try:
                await litellm.delete_model(model_id)
            except _WRITE_ERRORS as exc:
                log.warning("backends: cannot remove %s on %s: %s", m.model_name, m.node, exc)
                continue
            self._remember(model_id, "delete")
            log.info("backends: removed %s on %s (%s)", m.model_name, m.node, m.api_base)

        backends = [c.view() for c in wanted.values()]
        if keep_k8s:  # registered earlier and not probed this round
            backends += [
                {
                    "id": m.id,
                    "model_name": m.model_name,
                    "api_base": m.api_base,
                    "source": m.source,
                    "node": m.node,
                    "max_input_tokens": m.max_input_tokens,
                }
                for m in existing.values()
                if m.source == "k8s"
            ]
        status = {"at": datetime.now(UTC).isoformat(), "backends": backends}
        self.app_state.backend_status = status
        redis = getattr(self.app_state, "redis", None)
        if redis is not None:
            try:
                await redis.set(BACKEND_STATUS_KEY, json.dumps(status), ex=BACKEND_STATUS_TTL_S)
            except REDIS_ERRORS as exc:
                now = time.monotonic()
                if now - self._status_warned_at >= _LOG_EVERY_S:
                    self._status_warned_at = now
                    log.warning("backends: cannot publish the status to Redis: %s", exc)
        await self._sweep_key_access()

    async def _sweep_key_access(self) -> None:
        """Keys follow their owners' roles (portal/app/key_access.py); a failure here never disturbs the model list."""
        try:
            updated = await key_access.sweep(self.app_state)
        except Exception:  # noqa: BLE001 - the next round retries
            log.warning("backends: key access sweep failed", exc_info=True)
            return
        if updated:
            log.info("backends: keys of %d users updated to their role's models", updated)
