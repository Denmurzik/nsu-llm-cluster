"""Ready backend counts for the pages every user polls (/api/public/status, /api/models).

Asking LiteLLM `/model/info` on each request costs a few megabytes of JSON per call and 1000 browsers poll
every 15 s. The counts come from the backend controller's last round in Redis (it is the source that decides
what is registered in LiteLLM) while it is fresh, otherwise from LiteLLM itself; either way the answer is kept
for a few seconds in this process, one lookup at a time.
"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime

from app.controllers.backends import BACKEND_STATUS_KEY
from app.deployments import service
from app.deps import REDIS_ERRORS

log = logging.getLogger(__name__)


class BackendCounts:
    """Reads `state.litellm` and `state.redis` on every lookup (they are replaced in tests and on reconnect)."""

    def __init__(self, state, ttl_s: float, status_max_age_s: float) -> None:
        self._state = state
        self.ttl_s = ttl_s
        self.status_max_age_s = status_max_age_s
        self._lock = asyncio.Lock()
        self._value: dict[str, int] | None = None
        self._at = float("-inf")

    def _fresh(self) -> bool:
        return self.ttl_s > 0 and time.monotonic() - self._at < self.ttl_s

    async def get(self) -> dict[str, int] | None:
        """Model name -> ready backends; None when neither Redis nor the gateway can say."""
        if self._fresh():
            return self._value
        async with self._lock:
            if self._fresh():  # another request refreshed it while this one waited
                return self._value
            value = await self._from_redis()
            if value is None:
                value = await service.ready_backends(self._state.litellm)
            self._value, self._at = value, time.monotonic()
            return value

    async def _from_redis(self) -> dict[str, int] | None:
        try:
            raw = await self._state.redis.get(BACKEND_STATUS_KEY)
        except REDIS_ERRORS:
            return None
        if not raw:
            return None
        try:
            status = json.loads(raw)
            age = (datetime.now(UTC) - datetime.fromisoformat(status["at"])).total_seconds()
            if age > self.status_max_age_s:
                return None  # the controllers stopped: ask the gateway
            counts: dict[str, int] = {}
            for backend in status["backends"]:
                name = backend["model_name"]
                counts[name] = counts.get(name, 0) + 1
        except (ValueError, KeyError, TypeError):
            log.warning("backend status in Redis is malformed", exc_info=True)
            return None
        return counts
