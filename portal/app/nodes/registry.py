"""Standalone node registry: registration, heartbeats and liveness of nodes that run without k3s."""

import hmac
import logging
from datetime import UTC, datetime

from kubernetes_asyncio.client import ApiException
from sqlalchemy import delete, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.deployments.service import live_nodes
from app.deps import REDIS_ERRORS
from app.k8s import K8sUnavailable
from app.metrics import live
from app.models import ControllerTimer, NodeAssignment, NodeModels, NodeWake, StandaloneNode
from app.nodes import cleanup, relay, versions, wake

log = logging.getLogger(__name__)

__all__ = [
    "delete_k8s_node",
    "forget_live",
    "heartbeat",
    "live_nodes",
    "remove",
    "set_models",
    "set_wake",
    "stop",
    "token_matches",
    "upsert",
]

KEY_UPDATE_SEEN = "node:upd-seen:"  # Redis: when the node last asked «should I update» (api/admin_versions.py KEY_SEEN)
NOT_READY_TIMER = "notready:"  # ControllerTimer key of a node that went NotReady (controllers/workloads.py)


def token_matches(presented: str, secret: str, user: str) -> bool:
    """The raw token or the k3s secure form `K10<ca hash>::<user>:<secret>` (user `node` for the agent token,
    `server` for the server token); constant time."""
    if not secret:
        return False
    token, full = presented.encode(), f"::{user}:{secret}".encode()
    raw = hmac.compare_digest(token, secret.encode())
    # compare the tail of the same length, so the check does not depend on where a mismatch is
    k3s = len(token) >= len(full) and hmac.compare_digest(token[-len(full) :], full)
    return raw or k3s


async def upsert(
    db: AsyncSession,
    name: str,
    url: str,
    accel: str,
    vram_mib: int | None,
    ram_mib: int | None,
    version: str,
) -> None:
    """Registers (or re-registers) a node and makes sure it has a NodeAssignment row.

    An existing assignment keeps its deployment and `manual` flag; only its source becomes standalone.
    A fresh registration has no running server yet, so `applied_spec_hash` is reset.
    """
    now = datetime.now(UTC)
    values = {
        "url": url,
        "accel": accel,
        "vram_mib": vram_mib,
        "ram_mib": ram_mib,
        "version": version,
        "applied_spec_hash": None,
        "registered_at": now,
        "last_seen_at": now,
        "stopped_at": None,
    }
    await db.execute(
        insert(StandaloneNode)
        .values(name=name, **values)
        .on_conflict_do_update(index_elements=[StandaloneNode.name], set_=values)
    )
    await db.execute(
        insert(NodeAssignment)
        .values(node_name=name, deployment_id=None, source="standalone", manual=False)
        .on_conflict_do_update(index_elements=[NodeAssignment.node_name], set_={"source": "standalone"})
    )
    await db.commit()


async def heartbeat(db: AsyncSession, name: str, applied_spec_hash: str | None) -> bool:
    """Marks the node alive and records the config its server runs; False for an unknown node."""
    result = await db.execute(
        update(StandaloneNode)
        .where(StandaloneNode.name == name)
        .values(last_seen_at=datetime.now(UTC), applied_spec_hash=applied_spec_hash, stopped_at=None)
        .returning(StandaloneNode.name)
    )
    found = result.first() is not None
    await db.commit()
    return found


async def set_wake(db: AsyncSession, name: str, mac: str, ip: str, prefix: int) -> None:
    """Stores the NIC a node reported for Wake-on-LAN (`mac` normalized); written only when it changed, the
    heartbeat of a standalone node repeats it every 15 s. The caller commits."""
    stmt = insert(NodeWake).values(node_name=name, mac=mac, ip=ip, prefix=prefix)
    new = stmt.excluded
    changed = or_(NodeWake.mac != new.mac, NodeWake.ip != new.ip, NodeWake.prefix != new.prefix)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[NodeWake.node_name],
            set_={"mac": mac, "ip": ip, "prefix": prefix, "updated_at": datetime.now(UTC)},
            where=changed,
        )
    )


async def set_models(db: AsyncSession, name: str, models_bytes: int) -> None:
    """Stores how many bytes the node's models volume holds; written only when it changed, the heartbeat of a standalone
    node repeats it every 15 s. The caller commits."""
    stmt = insert(NodeModels).values(node_name=name, models_bytes=models_bytes)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[NodeModels.node_name],
            set_={"models_bytes": models_bytes, "updated_at": datetime.now(UTC)},
            where=NodeModels.models_bytes != stmt.excluded.models_bytes,
        )
    )


async def stop(db: AsyncSession, name: str) -> bool:
    """A clean stop of the node (its container is shutting down, e.g. Windows «Завершение работы»): out of routing at
    once, its server runs no config any more; the record, the assignment and the NIC stay, so the PC is woken when
    its deployment needs it. False for an unknown node."""
    result = await db.execute(
        update(StandaloneNode)
        .where(StandaloneNode.name == name)
        .values(stopped_at=datetime.now(UTC), applied_spec_hash=None)
        .returning(StandaloneNode.name)
    )
    found = result.first() is not None
    await db.commit()
    return found


async def remove(db: AsyncSession, name: str, commit: bool = True) -> bool:
    """Deletes everything the database keeps for the node: the registry row, the reported NIC (an uninstalled PC is never
    woken), the size of its models volume, its assignment (the deployment it served) and the controllers' pauses for
    it. True when the registry row existed. `commit=False`: the caller commits (with an audit row)."""
    await db.execute(delete(NodeWake).where(NodeWake.node_name == name))
    await db.execute(delete(NodeModels).where(NodeModels.node_name == name))
    await db.execute(delete(NodeAssignment).where(NodeAssignment.node_name == name))
    timers = (wake.TIMER_PREFIX, cleanup.PURGE_TIMER_PREFIX, cleanup.CACHE_TIMER_PREFIX, NOT_READY_TIMER)
    await db.execute(delete(ControllerTimer).where(ControllerTimer.key.in_([p + name for p in timers])))
    result = await db.execute(delete(StandaloneNode).where(StandaloneNode.name == name).returning(StandaloneNode.name))
    found = result.first() is not None
    if commit:
        await db.commit()
    return found


async def delete_k8s_node(k8s, name: str) -> str:
    """Deletes the Kubernetes Node object (and with it the node password k3s keeps): `deleted`, `absent` (there was none)
    or `unavailable` (the cluster does not answer; logged, never raised: the rest of the removal goes on)."""
    try:
        if not await k8s.available():
            return "unavailable"
        return "deleted" if await k8s.delete_node(name) else "absent"
    except (K8sUnavailable, ApiException) as exc:
        log.warning("node %s: could not delete the Kubernetes node: %s", name, exc)
        return "unavailable"


async def forget_live(redis, name: str) -> bool:
    """Deletes what the portal keeps for the node in Redis: its image report and update request, the live metrics sample
    and its push rate limit, the cleanup orders and the last answer, the Wake-on-LAN commands waiting for it. The history
    rows of the metrics stay. False when Redis did not answer (logged; every key has a TTL)."""
    keys = [
        versions.KEY_REPORT + name,
        versions.KEY_REQUEST + name,
        KEY_UPDATE_SEEN + name,
        live.NODE_KEY + name,
        live.RATE_KEY + name,
        cleanup.ORDER_PREFIX + name,
        cleanup.RESULT_PREFIX + name,
        relay.KEY_PREFIX + name,
    ]
    try:
        await redis.delete(*keys)
    except REDIS_ERRORS as exc:
        log.warning("node %s: its Redis keys were not deleted: %s", name, exc)
        return False
    return True
