"""User-level limits controller (task SEC-1, rule 7): keeps the rpm/tpm of every LiteLLM user that has a key equal to the
limits the panel gives it (app.user_limits.sweep). Runs at most once per SWEEP_EVERY_S, whatever the controller interval."""

import logging
import time

from app import user_limits

log = logging.getLogger(__name__)

SWEEP_EVERY_S = 60.0


class UserLimitsController:
    def __init__(self, app_state, clock=time.monotonic):
        self.app_state = app_state
        self._clock = clock
        self._last = float("-inf")

    async def reconcile_once(self) -> None:
        now = self._clock()
        if now - self._last < SWEEP_EVERY_S:
            return
        self._last = now
        if not await self.app_state.litellm.health():
            return
        updated = await user_limits.sweep(self.app_state)
        if updated:
            log.info("user limits: %d users got their limits in LiteLLM", updated)
