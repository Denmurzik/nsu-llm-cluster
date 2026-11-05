"""Per-model gateway traffic of one minute, written by the LiteLLM hook (main/litellm/overload_hook.py).

One Redis hash per minute, `portal:gwm:<minute since epoch>`, expiring after two hours. Its fields are
`<model>:req` (requests that ended: successes, 429 and 5xx), `<model>:429`, `<model>:5xx` and `<model>:t<bound>`, a
histogram of the time to the first token of streamed answers (`tinf` above the last bound). The key format is shared
with the hook; main/tests/test_gateway_keys.py compares the two.
"""

import logging
from dataclasses import dataclass, field

from app.deps import REDIS_ERRORS

log = logging.getLogger(__name__)

PREFIX = "portal:gwm"
TTFT_BOUNDS_MS = (100, 250, 500, 1000, 2000, 3000, 5000, 10000, 20000, 30000, 60000)


def bucket_key(minute: int) -> str:
    return f"{PREFIX}:{minute}"


@dataclass
class ModelMinute:
    requests: int = 0
    e429: int = 0
    e5xx: int = 0
    # answers per bucket: index i counts those up to TTFT_BOUNDS_MS[i], the last one those above the last bound
    ttft: list[int] = field(default_factory=lambda: [0] * (len(TTFT_BOUNDS_MS) + 1))

    def percentile(self, p: float) -> float | None:
        """The time to the first token (ms) below which a share `p` of the streamed answers fell, interpolated
        inside its bucket; None without samples. An answer above the last bound counts as that bound."""
        total = sum(self.ttft)
        if total == 0:
            return None
        target = p * total
        seen = 0
        lower = 0
        for i, count in enumerate(self.ttft):
            if i == len(TTFT_BOUNDS_MS):  # above the last bound: no width to interpolate in
                return float(TTFT_BOUNDS_MS[-1])
            upper = TTFT_BOUNDS_MS[i]
            if count and seen + count >= target:
                return round(lower + (upper - lower) * (target - seen) / count, 1)
            seen += count
            lower = upper
        return float(TTFT_BOUNDS_MS[-1])


def _count(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def read_minute(redis, minute: int) -> dict[str, ModelMinute] | None:
    """{model: ModelMinute} of one finished minute; None when Redis cannot be read."""
    try:
        raw = await redis.hgetall(bucket_key(minute))
    except REDIS_ERRORS as exc:
        log.warning("gateway model counters unavailable: %s", exc)
        return None
    out: dict[str, ModelMinute] = {}
    for name, value in raw.items():
        model, _, kind = name.rpartition(":")
        if not model:
            continue
        stats = out.setdefault(model, ModelMinute())
        n = _count(value)
        if kind == "req":
            stats.requests += n
        elif kind == "429":
            stats.e429 += n
        elif kind == "5xx":
            stats.e5xx += n
        elif kind == "tinf":
            stats.ttft[-1] += n
        elif kind[:1] == "t" and kind[1:].isdigit() and int(kind[1:]) in TTFT_BOUNDS_MS:
            stats.ttft[TTFT_BOUNDS_MS.index(int(kind[1:]))] += n
    return out
