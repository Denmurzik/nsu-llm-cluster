"""Standalone node registry: registration, heartbeats and liveness of nodes that run without k3s."""

import hmac
from datetime import UTC, datetime

from sqlalchemy import delete, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.deployments.service import live_nodes
from app.models import NodeAssignment, NodeWake, StandaloneNode

__all__ = ["heartbeat", "live_nodes", "remove", "set_wake", "stop", "token_matches", "upsert"]


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


async def remove(db: AsyncSession, name: str) -> bool:
    """Deletes the registry row and the reported NIC (an uninstalled PC is never woken); the NodeAssignment stays so a
    re-created node keeps its model."""
    await db.execute(delete(NodeWake).where(NodeWake.node_name == name))
    result = await db.execute(delete(StandaloneNode).where(StandaloneNode.name == name).returning(StandaloneNode.name))
    found = result.first() is not None
    await db.commit()
    return found
