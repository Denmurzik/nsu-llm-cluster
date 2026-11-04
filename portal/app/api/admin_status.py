"""Admin «Состояние»: one read-only page-sized answer about nodes, deployments, the gateway and backups."""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app import backups, gateway_stats
from app.api.admin import collect_nodes
from app.auth.sessions import require_admin
from app.config import Settings
from app.db import get_session
from app.deployments import service
from app.deps import get_backend_counts, get_k8s, get_settings_dep

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

STATUS_OFF = "off"


def _age_s(at: datetime | None, now: datetime) -> int | None:
    return None if at is None else max(0, int((now - at).total_seconds()))


@router.get("/status")
async def status(
    request: Request,
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
    counts=Depends(get_backend_counts),
) -> dict[str, Any]:
    state = request.app.state
    now = datetime.now(UTC)
    k8s_available, rows, _, heartbeat_at = await collect_nodes(state, db, k8s, settings, with_heartbeats=True)
    nodes = [
        {
            "name": n["name"],
            "source": n["source"],
            "accel": n["accel"],
            "ready": n["ready"],
            "is_main": n["is_main"],
            "vram_mib": n["vram_mib"],
            "mem_mib": n["mem_mib"],
            "deployment": n["deployment"],
            "backends": n["backends"],
            "heartbeat_age_s": _age_s(heartbeat_at.get(n["name"]), now),
            "maintenance": bool(n.get("maintenance", False)),  # the nodes API field of the resilience task; absent: false
            "wakeable": n["wakeable"],
            "wake": n["wake"],
        }
        for n in rows
    ]

    all_deployments = await service.list_all(db)
    assigned = await service.assigned_nodes(db)
    await db.commit()  # the gateway is asked next: no pooled connection waits on the network
    ready_counts = await counts()
    deployments = []
    for dep in all_deployments:
        ready = None if ready_counts is None or not dep.enabled else ready_counts.get(dep.name, 0)
        n_assigned = len(assigned.get(dep.id, []))
        deployments.append(
            {
                "name": dep.name,
                "mode": dep.mode,
                "enabled": dep.enabled,
                "status": service.catalog_status(ready, n_assigned) if dep.enabled else STATUS_OFF,
                "ready_backends": ready,
                "assigned_nodes": n_assigned,
            }
        )

    window = await gateway_stats.read_counts(state.redis, time.time())
    gateway = {
        "available": window is not None,
        "last_5m": window["last_5m"] if window else None,
        "last_1h": window["last_1h"] if window else None,
    }
    return {
        "at": now.isoformat(),
        "k8s_available": k8s_available,
        "nodes": nodes,
        "deployments": deployments,
        "gateway": gateway,
        "main": {"gpu": settings.main_gpu or None},
        "backups": await asyncio.to_thread(backups.summarize, settings.backup_dir, now),
    }
