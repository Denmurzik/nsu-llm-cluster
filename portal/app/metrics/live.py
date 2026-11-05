"""The latest sample of every node and model, kept in Redis for the live views and for the per-minute rows.

Nodes write their key from the API workers (any of them), the controllers process writes the model keys and reads
both to make the rows of `metric_samples`. A sample expires by itself: a node that stopped reporting disappears
from the live view after NODE_LIVE_TTL_S.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.deps import REDIS_ERRORS

log = logging.getLogger(__name__)

NODE_KEY = "portal:metrics:node:"
MODEL_KEY = "portal:metrics:model:"
RATE_KEY = "portal:metrics:rate:"
NODE_LIVE_TTL_S = 120  # about six missed pushes of the 20 s interval
MODEL_LIVE_TTL_S = 150  # a little over two controller minutes
MIN_PUSH_GAP_MS = 5000  # a node pushing faster than this is refused with 429 (the node's own interval is 20 s)


async def allow_push(redis, name: str) -> bool:
    """True when this node may push now: one sample per MIN_PUSH_GAP_MS (a lost reply is retried a little later)."""
    return bool(await redis.set(RATE_KEY + name, "1", px=MIN_PUSH_GAP_MS, nx=True))


async def put(redis, prefix: str, name: str, sample: dict[str, Any], ttl_s: int) -> None:
    await redis.set(prefix + name, json.dumps(sample), ex=ttl_s)


async def read_all(redis, prefix: str) -> dict[str, dict[str, Any]]:
    """{name: sample} of every live key; a key that does not parse is ignored. Raises the Redis errors."""
    keys = [k async for k in redis.scan_iter(match=prefix + "*", count=200)]
    if not keys:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, raw in zip(keys, await redis.mget(keys), strict=True):
        if raw is None:
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            out[key.removeprefix(prefix)] = value
    return out


async def read_safe(redis, prefix: str) -> dict[str, dict[str, Any]] | None:
    """As read_all, None when Redis cannot be read."""
    try:
        return await read_all(redis, prefix)
    except REDIS_ERRORS as exc:
        log.warning("live metrics unavailable: %s", exc)
        return None


def age_s(sample: dict[str, Any], now: datetime | None = None) -> int | None:
    try:
        at = datetime.fromisoformat(sample["at"])
    except (KeyError, TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0, int(((now or datetime.now(UTC)) - at).total_seconds()))
