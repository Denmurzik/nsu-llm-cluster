import asyncio
import logging
import time

from fastapi import FastAPI

from app.controllers.backends import BackendController
from app.controllers.local_models import LocalModelsController
from app.controllers.metrics import MetricsController
from app.controllers.user_limits import UserLimitsController
from app.controllers.workloads import WorkloadController

log = logging.getLogger(__name__)

_LOG_EVERY_S = 60.0


class _RateLimitedLog:
    """Logs a given message at most once per minute."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def error(self, name: str, exc: BaseException) -> None:
        key = f"{name}: {type(exc).__name__}: {exc}"
        now = time.monotonic()
        if now - self._last.get(key, -_LOG_EVERY_S) >= _LOG_EVERY_S:
            self._last[key] = now
            log.error("controller %s failed: %s", name, exc, exc_info=exc)


async def _loop(name: str, controller, interval_s: float) -> None:
    limiter = _RateLimitedLog()
    while True:
        try:
            await controller.reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            limiter.error(name, exc)
        await asyncio.sleep(interval_s)


def start(app: FastAPI) -> None:
    interval = app.state.settings.controller_interval_s
    controllers = [
        ("workloads", WorkloadController(app.state)),
        ("backends", BackendController(app.state)),
        ("local-models", LocalModelsController(app.state)),
        ("metrics", MetricsController(app.state)),
        ("user-limits", UserLimitsController(app.state)),
    ]
    app.state.controller_tasks = [
        asyncio.create_task(_loop(name, ctl, interval), name=f"controller-{name}") for name, ctl in controllers
    ]


async def stop(app: FastAPI) -> None:
    tasks = getattr(app.state, "controller_tasks", [])
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    app.state.controller_tasks = []
