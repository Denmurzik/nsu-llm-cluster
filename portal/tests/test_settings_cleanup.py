"""`model_cache_idle_s` of the panel (task 2-34) reaches the cleanup of idle nodes without a restart: the workload controller
reads it through `state.runtime`, not from the .env."""

from types import SimpleNamespace

import fakeredis
import pytest

from app import settings_store
from app.controllers.workloads import WorkloadController
from app.settings_store import RuntimeSettings

# the fixtures and helpers of the controller's own cleanup tests: an idle node, a fake clock, the cleanup Jobs
from tests.test_cleanup_controller import (  # noqa: F401
    IDLE_S,
    add_nodes,
    at,
    clean_slate,
    clock,
    k8s,
    make_hf,
    purges,
    settings,
)

pytestmark = pytest.mark.docker


@pytest.fixture
def state(app, k8s, settings):  # noqa: F811
    s = app.state
    redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    return SimpleNamespace(
        settings=settings,
        sessionmaker=s.sessionmaker,
        k8s=k8s,
        hf=make_hf(),
        redis=redis,
        runtime=RuntimeSettings(s, ttl=0),
    )


@pytest.fixture
def controller(state):
    return WorkloadController(state)


async def test_idle_time_from_the_panel_applies_at_once(app, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    async with app.state.sessionmaker() as db:
        await settings_store.set(db, "model_cache_idle_s", 100, None)
        await db.commit()
    await at(controller, clock, 1000)
    await at(controller, clock, 1099)
    assert purges(k8s) == []  # the .env says 600 s (IDLE_S), the panel 100 s
    await at(controller, clock, 1100)
    assert purges(k8s) == [("pc01", [])]
    assert IDLE_S == 600
