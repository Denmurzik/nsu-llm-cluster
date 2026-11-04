"""Own model files (app.localmodels): checks finished uploads, finds files dropped into the folder on main."""

import time

from app.localmodels import service


class LocalModelsController:
    def __init__(self, app_state):
        self.app_state = app_state
        self._next_scan = 0.0  # monotonic; the first pass scans at once

    async def reconcile_once(self) -> None:
        state = self.app_state
        store = state.local_models
        async with state.sessionmaker() as db:
            # sha256 of a file of hundreds of GB takes minutes: this controller has its own loop, the others go on
            await service.verify_pending(db, store)
            now = time.monotonic()
            if now >= self._next_scan:
                await service.expire_uploads(db, store)
                await service.scan(db, store, state.settings)
                self._next_scan = now + state.settings.local_models_scan_s
