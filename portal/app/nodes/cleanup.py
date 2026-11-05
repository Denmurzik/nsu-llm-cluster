"""Cleanup of model weights on class nodes (task 2-26): what a node needs, how the order reaches it, what came back.

A class node keeps its weights on the volume `llm-models` (hostPath /var/lib/llm-models for the pods): the model files
of every deployment it ever served and the tensor cache of the rpc server. Nothing but the start of a new llama pod
removed the files of other models, a node without a deployment kept tens of GB for good. `models-cleanup.sh` of the
node image removes what a `Keep` does not list; this module decides what that is and hands the order over:

- a k3s node gets a one-shot Job pinned to it (builders.purge_job). Chosen over a pass in an existing pod because an
  idle node has no pod at all, over an agent of ours on the node because there is no listener, token or firewall opening
  to add, and the script that runs is the one of the node image the portal is configured with, whatever image the PC was
  installed with. The Job reads its answer from the log (the `RESULT` line).
- a standalone node gets the order in the reply to its next heartbeat (like the Wake-on-LAN relay orders, app.nodes.relay)
  and reports the result with a later one.

The workload controller issues the automatic cleanups (`WorkloadController._cleanup`), the «Очистить модели» button of
the «Ноды» page issues the manual one (app.api.admin) and waits for the result.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field

from kubernetes_asyncio.client.exceptions import ApiException

from app.deployments import service
from app.deps import REDIS_ERRORS
from app.k8s import K8sUnavailable
from app.localmodels.store import is_local
from app.workloads import builders

log = logging.getLogger(__name__)

ORDER_PREFIX = "models:purge:"  # Redis list per standalone node: orders its next heartbeat takes
RESULT_PREFIX = "models:purge-result:"  # Redis string per standalone node: the last result it reported
ORDER_TTL_S = 300  # an order nobody took by then is stale (the node is gone)
RESULT_TTL_S = 3600
PURGE_TIMER_PREFIX = "purge:"  # ControllerTimer key of a cleanup waiting for its time or running
CACHE_TIMER_PREFIX = "cache:"  # ControllerTimer key of what a node's volume was last cleaned for
RESULT_PREFIX_LOG = "RESULT "  # the line models-cleanup.sh ends with
POLL_S = 1.0
WAIT_K8S_S = 90.0  # the button waits this long for a Job
WAIT_STANDALONE_S = 50.0  # a heartbeat takes the order, a later one brings the result back
GIVE_UP_S = 180.0  # a standalone order without an answer by then failed (a live node answers within two heartbeats)

_K8S_ERRORS = (K8sUnavailable, ApiException)


@dataclass(frozen=True)
class Keep:
    """What a node's cleanup leaves: files as `repo:file` (models-cleanup.sh `--keep`), the rpc tensor cache, vLLM's cache."""

    files: tuple[str, ...] = ()
    rpc: bool = False
    vllm: bool = False

    def args(self) -> list[str]:
        result: list[str] = []
        for f in self.files:
            result += ["--keep", f]
        if self.rpc:
            result.append("--keep-rpc")
        if self.vllm:
            result.append("--keep-vllm")
        return result

    def order(self, order_id: str) -> dict:
        return {"id": order_id, "keep": list(self.files), "keep_rpc": self.rpc, "keep_vllm": self.vllm}


def deployment_files(dep) -> set[str]:
    """The files of `dep` the pods keep on a node: the model, its draft model and its projector (`repo:file`)."""
    files: set[str] = set()
    if dep.engine == "llamacpp" and dep.file:
        files.add(f"{dep.repo}:{dep.file}")
    draft_repo, draft_file = getattr(dep, "draft_repo", None), getattr(dep, "draft_file", None)
    if draft_repo and draft_file:
        files.add(f"{draft_repo}:{draft_file}")
    mmproj = getattr(dep, "mmproj_file", None)
    if mmproj:
        if is_local(mmproj):  # local/<model>/<file>
            owner, _, rest = mmproj.partition("/")
            name, _, file = rest.partition("/")
            if name and file:
                files.add(f"{owner}/{name}:{file}")
        elif dep.engine == "llamacpp":
            files.add(f"{dep.repo}:{mmproj}")
    return files


def keep_for(deps, rpc: bool | None = None) -> Keep:
    """The union of what the deployments `deps` need. `rpc` overrides the tensor cache: by default it stays when one of
    them is a split deployment."""
    files: set[str] = set()
    for d in deps:
        files |= deployment_files(d)
    return Keep(
        tuple(sorted(files)),
        any(d.mode == "split" for d in deps) if rpc is None else rpc,
        any(d.engine == "vllm" for d in deps),
    )


@dataclass(frozen=True)
class Signature:
    """What a node's volume is cleaned for: the files of its assigned deployment and the model its rpc tensor cache
    belongs to (the split model, repo:file without the commit). `IDLE` is a node without a deployment."""

    files: tuple[str, ...] = ()
    rpc: str = ""
    vllm: bool = False

    def dump(self) -> str:
        return json.dumps({"files": list(self.files), "rpc": self.rpc, "vllm": self.vllm}, sort_keys=True)

    @staticmethod
    def load(text: str) -> "Signature | None":
        try:
            raw = json.loads(text)
            return Signature(tuple(raw["files"]), str(raw["rpc"]), bool(raw["vllm"]))
        except (ValueError, KeyError, TypeError):
            return None


IDLE = Signature()


def signature(dep) -> Signature:
    """The signature of the deployment a node is assigned to (None: the node has none)."""
    if dep is None:
        return IDLE
    keep = keep_for([dep])
    return Signature(keep.files, f"{dep.repo}:{dep.file}" if dep.mode == "split" else "", keep.vllm)


def node_deployments(row, deployments, accel, source, now, drain_s: float = 0.0):
    """(assigned, needed): the deployment a node is assigned to when it is enabled, and every deployment the node needs
    files for now, which adds the one it is lent to outside the working window. No deployment is: no assignment, the
    deployment deleted or disabled. A node on maintenance keeps its assignment, and so its files."""
    assigned = None
    if row is not None and row.deployment_id is not None:
        assigned = next((d for d in deployments if d.id == row.deployment_id and d.enabled), None)
    needed = [assigned] if assigned is not None else []
    effective = service.effective_deployment(row, deployments, accel, source, now, drain_s)
    if effective is not None and effective is not assigned:
        needed.append(effective)
    return assigned, needed


# ---- results --------------------------------------------------------------------------------------------------------


@dataclass
class Outcome:
    state: str  # "running" | "done" | "failed"
    freed_bytes: int = 0
    models_bytes: int | None = None  # what is left on the volume
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str = ""


def parse_result(text: str) -> Outcome | None:
    """The `RESULT {json}` line models-cleanup.sh ends with (the last one in `text`); None without one."""
    for line in reversed(text.splitlines()):
        if line.startswith(RESULT_PREFIX_LOG):
            try:
                raw = json.loads(line[len(RESULT_PREFIX_LOG):])
                return _outcome(raw)
            except (ValueError, TypeError):
                return None
    return None


def _outcome(raw: dict) -> Outcome:
    def count(key: str) -> int | None:
        value = raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def names(key: str) -> list[str]:
        value = raw.get(key)
        return [str(v)[:512] for v in value[:50]] if isinstance(value, list) else []

    error = str(raw.get("error") or "")[:500]
    return Outcome(
        "failed" if error else "done", count("freed_bytes") or 0, count("models_bytes"), names("removed"),
        names("skipped"), error,
    )


# ---- sending an order and waiting for its answer -------------------------------------------------------------------------


async def send(state, node: str, source: str, accel: str | None, keep: Keep) -> tuple[str | None, str | None]:
    """Hands the order to the node: (reference, None), or (None, the error). The reference is the Job (k3s) or the
    order id (standalone) `poll` takes."""
    try:
        if source == "k8s":
            return await state.k8s.create_job(builders.purge_job(node, accel, keep.args(), state.settings)), None
        order_id = uuid.uuid4().hex
        key = ORDER_PREFIX + node
        await state.redis.rpush(key, json.dumps(keep.order(order_id)))
        await state.redis.expire(key, ORDER_TTL_S)
        await state.redis.delete(RESULT_PREFIX + node)
        return order_id, None
    except (*_K8S_ERRORS, *REDIS_ERRORS) as exc:
        return None, f"{type(exc).__name__}: {exc}"


async def poll(state, node: str, source: str, ref: str, issued_at: float | None = None, now: float | None = None) -> Outcome:
    """Where an order stands. A Job ends by its `status.conditions` (Complete or Failed): a Job killed at its deadline
    (an image that cannot be pulled, a node that cannot start the pod) is Failed even with `status.failed` 0. A Job that
    is gone was never seen succeeding (a success ends the order the moment it is seen, so nobody asks again): it counts
    as failed and the cleanup is tried again, it is harmless to repeat. A standalone order without an answer GIVE_UP_S
    after `issued_at` (`now`) failed."""
    if source == "k8s":
        try:
            job = await state.k8s.read_job(ref)
            if job is None:
                return Outcome("failed", error="задание исчезло, результат не получен")
            if not job.condition and not job.succeeded and not job.failed:  # no pod yet, or running
                return Outcome("running")
            log_text = await state.k8s.job_log(ref, 50)
        except _K8S_ERRORS as exc:
            log.warning("models cleanup of %s: job %s unreadable: %s", node, ref, exc)
            return Outcome("running")
        parsed = parse_result(log_text)
        ended_ok = job.condition != "Failed" and bool(job.succeeded or job.condition == "Complete")
        if ended_ok and parsed is not None:
            return parsed
        if ended_ok:
            return Outcome("done")
        tail = " ".join(log_text.strip().splitlines()[-3:])[:300]
        return Outcome("failed", error=tail or f"задание на ноде не выполнено ({job.reason or 'без причины'})")
    try:
        raw = await state.redis.get(RESULT_PREFIX + node)
    except REDIS_ERRORS as exc:
        log.warning("models cleanup of %s: result unavailable: %s", node, exc)
        return Outcome("running")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("id") == ref:
                return _outcome(data)
        except ValueError:
            pass
    if issued_at is not None and now is not None and now - issued_at > GIVE_UP_S:
        return Outcome("failed", error="нода не забрала команду")
    return Outcome("running")


async def wait(state, node: str, source: str, ref: str, timeout_s: float | None = None) -> Outcome:
    """`poll` until the order ends or the time is up (then the last answer: running)."""
    limit = (WAIT_K8S_S if source == "k8s" else WAIT_STANDALONE_S) if timeout_s is None else timeout_s
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while True:
        outcome = await poll(state, node, source, ref)
        if outcome.state != "running" or loop.time() >= deadline:
            return outcome
        await asyncio.sleep(POLL_S)


# ---- the node side of a standalone order ------------------------------------------------------------------------------


async def pop_orders(redis, name: str) -> list[dict]:
    """The cleanup orders a standalone node should run now (heartbeat reply); empty when Redis does not answer."""
    key = ORDER_PREFIX + name
    try:
        async with redis.pipeline(transaction=True) as pipe:
            raw, _ = await pipe.lrange(key, 0, -1).delete(key).execute()
    except REDIS_ERRORS as exc:
        log.warning("node %s: cleanup orders unavailable: %s", name, exc)
        return []
    orders = []
    for item in raw:
        try:
            order = json.loads(item)
            orders.append(
                {
                    "id": str(order["id"]),
                    "keep": [str(f) for f in order.get("keep", [])],
                    "keep_rpc": bool(order.get("keep_rpc")),
                    "keep_vllm": bool(order.get("keep_vllm")),
                }
            )
        except (ValueError, KeyError, TypeError):
            continue
    return orders


async def store_result(redis, name: str, raw: dict) -> Outcome | None:
    """The result a standalone node reported with its heartbeat, kept for whoever waits for it; None for junk."""
    order_id = raw.get("id")
    if not isinstance(order_id, str) or not 0 < len(order_id) <= 64:
        return None
    outcome = _outcome(raw)
    body = {
        "id": order_id, "freed_bytes": outcome.freed_bytes, "models_bytes": outcome.models_bytes,
        "removed": outcome.removed, "skipped": outcome.skipped, "error": outcome.error,
    }
    try:
        await redis.set(RESULT_PREFIX + name, json.dumps(body), ex=RESULT_TTL_S)
    except REDIS_ERRORS as exc:
        log.warning("node %s: cleanup result not stored: %s", name, exc)
    return outcome
