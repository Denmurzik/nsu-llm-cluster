"""Admin monitoring API (task 2-33): the latest sample of every node and model, and downsampled history for charts."""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store
from app.auth.sessions import require_admin
from app.db import get_session
from app.metrics import history, live

router = APIRouter(prefix="/api/admin/metrics", dependencies=[Depends(require_admin)])

DEFAULT_RANGE = timedelta(hours=1)
MIN_STEP_S = history.MINUTE_S
MAX_STEP_S = 86400


def _live_entries(samples: dict[str, dict[str, Any]] | None, now: datetime) -> list[dict[str, Any]]:
    entries = []
    for name in sorted(samples or {}):
        sample = dict(samples[name])
        at = sample.pop("at", None)
        entries.append({"name": name, "at": at, "age_s": live.age_s({"at": at}, now), **sample})
    return entries


@router.get("/live")
async def live_metrics(request: Request) -> dict[str, Any]:
    """The latest sample per node (pushed every ~20 s) and per deployment (made every minute). `available` is false
    when Redis cannot be read: the lists are then empty and the page says so."""
    redis = request.app.state.redis
    now = datetime.now(UTC)
    nodes = await live.read_safe(redis, live.NODE_KEY)
    models = await live.read_safe(redis, live.MODEL_KEY)
    return {
        "at": now.isoformat(),
        "available": nodes is not None and models is not None,
        "nodes": _live_entries(nodes, now),
        "models": _live_entries(models, now),
    }


@router.get("/names")
async def names(db: AsyncSession = Depends(get_session)) -> dict[str, list[str]]:
    """The nodes and deployments that have history within the retention period: the choices of the monitoring page."""
    days = int(await settings_store.get(db, "metrics_retention_days"))
    return await history.names(db, datetime.now(UTC) - timedelta(days=days))


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@router.get("/history")
async def history_metrics(
    kind: Literal["node", "model"],
    name: str = Query(min_length=1, max_length=255),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    step: int | None = Query(default=None, ge=MIN_STEP_S, le=MAX_STEP_S, description="секунд на точку"),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """The per-minute rows of one node or deployment between `from` and `to` (default: the last hour), merged into
    `step`-second points (default: whole minutes so that there are at most history.MAX_POINTS points). A point holds the
    average of its minutes, the worst one for errors, queue and temperature."""
    end = _utc(to) if to else datetime.now(UTC)
    start = _utc(from_) if from_ else end - DEFAULT_RANGE
    if start >= end:
        raise HTTPException(422, "Начало периода должно быть раньше его конца")
    if end - start > history.MAX_RANGE:
        raise HTTPException(422, "Период не больше года")
    used = step or history.auto_step(start, end)
    if (end - start).total_seconds() / used > history.MAX_POINTS * 4:  # a tiny step over a long range: nobody draws that
        raise HTTPException(422, "Слишком мелкий шаг для такого периода")
    _, points = await history.series(db, kind, name, start, end, used)
    return {
        "kind": kind,
        "name": name,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "step": used,
        "points": points,
    }
