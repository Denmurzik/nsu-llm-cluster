import asyncio

from fastapi import FastAPI

from app.config import Settings
from app.controllers import runner
from app.controllers.backends import BackendController
from app.controllers.workloads import WorkloadController


async def test_runner_keeps_looping_after_controller_errors(monkeypatch, caplog):
    calls = {"workloads": 0, "backends": 0}

    async def failing(self):
        calls["workloads"] += 1
        raise RuntimeError("boom")

    async def counting(self):
        calls["backends"] += 1

    monkeypatch.setattr(WorkloadController, "reconcile_once", failing)
    monkeypatch.setattr(BackendController, "reconcile_once", counting)

    app = FastAPI()
    app.state.settings = Settings(controller_interval_s=0.01)
    runner.start(app)
    await asyncio.sleep(0.2)
    await runner.stop(app)

    assert calls["workloads"] >= 3 and calls["backends"] >= 3
    # identical failures are logged once per minute, not on every iteration
    assert len([r for r in caplog.records if "boom" in r.getMessage()]) == 1
    assert app.state.controller_tasks == []
