"""History of `metric_samples`: the per-minute rows, their retention and the downsampled series for the charts."""

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MetricSample

MINUTE_S = 60
MAX_POINTS = 300  # a chart shows at most this many points: the step is raised to fit the range
MAX_RANGE = timedelta(days=366)
# a bucket keeps the worst minute of these (a spike must not vanish into an average), the rest is averaged
PEAK_FIELDS = frozenset({"e429", "e5xx", "queue", "gpu_temp_c"})


def minute_start(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(int(epoch_s // MINUTE_S) * MINUTE_S, UTC)


async def add_rows(db: AsyncSession, at: datetime, kind: str, rows: dict[str, dict[str, Any]]) -> None:
    """One row per (kind, name, minute): a restart of the controllers inside a minute replaces that minute's rows."""
    if not rows:
        return
    await db.execute(
        delete(MetricSample).where(MetricSample.at == at, MetricSample.kind == kind, MetricSample.name.in_(list(rows)))
    )
    for name, data in rows.items():
        db.add(MetricSample(at=at, kind=kind, name=name, data=data))


async def prune(db: AsyncSession, days: int, now: datetime | None = None) -> int:
    """Deletes the samples older than `days` days; returns how many."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    result = await db.execute(delete(MetricSample).where(MetricSample.at < cutoff))
    return result.rowcount or 0


def auto_step(start: datetime, end: datetime) -> int:
    """Seconds per point: whole minutes, so that the range fits MAX_POINTS."""
    span = max((end - start).total_seconds(), 1.0)
    return max(MINUTE_S, math.ceil(span / MAX_POINTS / MINUTE_S) * MINUTE_S)


def _merge(values: list[Any], peak: bool) -> Any:
    values = [v for v in values if v is not None]
    if not values:
        return None
    if all(isinstance(v, bool) for v in values):
        return any(values)
    numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not numbers:
        return values[-1]
    return max(numbers) if peak else round(sum(numbers) / len(numbers), 2)


def downsample(rows: list[tuple[datetime, dict[str, Any]]], step_s: int) -> list[dict[str, Any]]:
    """Groups the rows (sorted by time) into buckets of `step_s` seconds aligned to the epoch, so that the points
    of two requests for the same step fall on the same times; one point per bucket that has rows:
    {"t": bucket start, field: merged value}."""
    buckets: dict[int, list[dict[str, Any]]] = {}
    for at, data in rows:
        buckets.setdefault(int(at.timestamp() // step_s), []).append(data)
    points = []
    for index in sorted(buckets):
        group = buckets[index]
        names = sorted({k for d in group for k in d})
        point: dict[str, Any] = {"t": datetime.fromtimestamp(index * step_s, UTC).isoformat()}
        for name in names:
            point[name] = _merge([d.get(name) for d in group], name in PEAK_FIELDS)
        points.append(point)
    return points


async def series(
    db: AsyncSession, kind: str, name: str, start: datetime, end: datetime, step_s: int | None = None
) -> tuple[int, list[dict[str, Any]]]:
    """(step, points) of one node or deployment between `start` and `end`."""
    step = step_s or auto_step(start, end)
    result = await db.execute(
        select(MetricSample.at, MetricSample.data)
        .where(MetricSample.kind == kind, MetricSample.name == name, MetricSample.at >= start, MetricSample.at <= end)
        .order_by(MetricSample.at)
    )
    rows = [(at if at.tzinfo else at.replace(tzinfo=UTC), data) for at, data in result.all()]
    return step, downsample(rows, step)


async def names(db: AsyncSession, since: datetime) -> dict[str, list[str]]:
    """The nodes and deployments that have samples since `since`."""
    result = await db.execute(select(MetricSample.kind, MetricSample.name).where(MetricSample.at >= since).distinct())
    out: dict[str, list[str]] = {"node": [], "model": []}
    for kind, name in result.all():
        out.setdefault(kind, []).append(name)
    for lst in out.values():
        lst.sort()
    return out
