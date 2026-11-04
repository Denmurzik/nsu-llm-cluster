"""Gateway failure counters for the admin «Состояние» page.

The LiteLLM overload hook (main/litellm/overload_hook.py) counts every request that ends as 429 or 5xx in Redis:
one counter per kind and minute, `portal:gw:<429|5xx>:<minute since epoch>`, expiring after two hours. The page sums
the buckets of the last 5 and the last 60 minutes (the running minute included, so a window is up to a minute short
of the full span). The key format is shared with the hook; main/tests/test_gateway_keys.py compares the two.
"""

import logging

from app.deps import REDIS_ERRORS

log = logging.getLogger(__name__)

PREFIX = "portal:gw"
SHORT_MIN = 5
LONG_MIN = 60
KINDS = {"429": "rate_limited", "5xx": "server_errors"}


def bucket_key(kind: str, minute: int) -> str:
    return f"{PREFIX}:{kind}:{minute}"


def _sum(values: list) -> int:
    total = 0
    for v in values:
        try:
            total += int(v)
        except (TypeError, ValueError):
            continue
    return total


async def read_counts(redis, now: float) -> dict[str, dict[str, int]] | None:
    """{"last_5m": {"rate_limited", "server_errors"}, "last_1h": {...}}; None when Redis cannot be read."""
    current = int(now // 60)
    minutes = range(current, current - LONG_MIN, -1)  # newest first: the first SHORT_MIN are the short window
    result: dict[str, dict[str, int]] = {"last_5m": {}, "last_1h": {}}
    try:
        for kind, name in KINDS.items():
            values = await redis.mget([bucket_key(kind, m) for m in minutes])
            result["last_5m"][name] = _sum(values[:SHORT_MIN])
            result["last_1h"][name] = _sum(values)
    except REDIS_ERRORS as exc:
        log.warning("gateway counters unavailable: %s", exc)
        return None
    return result
