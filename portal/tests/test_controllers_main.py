"""The controllers run in their own process (`python -m app.controllers`); API workers run none."""

import asyncio
import subprocess
import sys
from pathlib import Path

import fakeredis
import pytest

from app.controllers import __main__ as controllers_main
from app.controllers.backends import BackendController
from app.controllers.local_models import LocalModelsController
from app.controllers.workloads import WorkloadController

pytestmark = pytest.mark.docker

PORTAL = Path(__file__).resolve().parent.parent


async def test_controllers_process_runs_every_controller_until_stopped(settings, monkeypatch):
    calls = {"workloads": 0, "backends": 0, "local-models": 0}
    states = []

    def counter(name):
        async def reconcile_once(self):
            calls[name] += 1
            states.append(self.app_state)

        return reconcile_once

    monkeypatch.setattr(WorkloadController, "reconcile_once", counter("workloads"))
    monkeypatch.setattr(BackendController, "reconcile_once", counter("backends"))
    monkeypatch.setattr(LocalModelsController, "reconcile_once", counter("local-models"))
    # controllers_enabled is the API workers' switch: the controllers process ignores it
    s = settings.model_copy(update={"controller_interval_s": 0.01, "controllers_enabled": False})
    redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    stop = asyncio.Event()
    task = asyncio.create_task(controllers_main.run(s, stop=stop, redis=redis))
    try:
        async with asyncio.timeout(60):
            while min(calls.values()) < 3:
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        async with asyncio.timeout(10):
            await task
    state = states[0]
    assert state.settings is s and state.redis is redis
    for name in ("sessionmaker", "litellm", "k8s", "hf", "local_models"):
        assert getattr(state, name) is not None, name
    assert state.controller_tasks == []


@pytest.mark.parametrize("enabled", [False, True])
async def test_api_app_runs_controllers_only_when_enabled(settings, monkeypatch, enabled):
    """CONTROLLERS_ENABLED=false (API workers): the app's lifespan never reconciles; true: it does."""
    from asgi_lifespan import LifespanManager

    from app.main import create_app

    calls = {"workloads": 0, "backends": 0, "local-models": 0}

    def counter(name):
        async def reconcile_once(self):
            calls[name] += 1

        return reconcile_once

    monkeypatch.setattr(WorkloadController, "reconcile_once", counter("workloads"))
    monkeypatch.setattr(BackendController, "reconcile_once", counter("backends"))
    monkeypatch.setattr(LocalModelsController, "reconcile_once", counter("local-models"))
    s = settings.model_copy(update={"controller_interval_s": 0.01, "controllers_enabled": enabled})
    application = create_app(s)
    application.state.redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    async with LifespanManager(application):
        await asyncio.sleep(0.3)
        tasks = list(getattr(application.state, "controller_tasks", []))
    if enabled:
        assert len(tasks) == 3 and min(calls.values()) >= 3
    else:
        assert tasks == [] and calls == {"workloads": 0, "backends": 0, "local-models": 0}


def test_module_is_runnable():
    proc = subprocess.run(
        [sys.executable, "-m", "app.controllers", "--help"], cwd=PORTAL, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "контроллер" in proc.stdout.lower()
