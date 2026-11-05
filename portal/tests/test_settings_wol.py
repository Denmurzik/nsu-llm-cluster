"""The Wake-on-LAN settings of the panel (task 2-34) reach the workload controller without a restart: the controller reads
`wol_enabled`, `wol_delay_s` and `wol_max_tries` from the database (through `state.runtime`), not from the .env."""

from types import SimpleNamespace

import fakeredis
import pytest

from app import settings_store
from app.controllers.workloads import WorkloadController
from app.settings_store import RuntimeSettings

# the fixtures and helpers of the controller's own WoL tests: one deployment on pc01, a fake clock, a recorded send
from tests.test_wake_controller import (  # noqa: F401
    NOON,
    clean_slate,
    clock,
    k8s,
    make_hf,
    pass_at,
    sent,
    setup,
    timers,
)

pytestmark = pytest.mark.docker


@pytest.fixture
def state(app, k8s, settings):  # noqa: F811
    s = app.state
    runtime = RuntimeSettings(s, ttl=0)
    redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    return SimpleNamespace(
        settings=settings, sessionmaker=s.sessionmaker, k8s=k8s, hf=make_hf(), redis=redis, runtime=runtime
    )


@pytest.fixture
def controller(state):
    return WorkloadController(state)


async def put(app, key, value):
    async with app.state.sessionmaker() as db:
        await settings_store.set(db, key, value, None)
        await db.commit()


async def test_wol_delay_from_the_panel_applies_at_once(app, db_session, k8s, controller, clock, sent):
    await setup(db_session, k8s)
    await put(app, "wol_delay_s", 10)
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1009)
    assert sent == []  # the .env says 60 s, the panel 10 s
    await pass_at(controller, clock, 1010)
    assert len(sent) == 1


async def test_wol_can_be_switched_off_and_on_from_the_panel(app, db_session, k8s, controller, clock, sent):
    await setup(db_session, k8s)
    await put(app, "wol_enabled", False)
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1100)
    assert sent == []
    await put(app, "wol_enabled", True)
    await pass_at(controller, clock, 1200)  # first seen "needed" now: the delay counts from here
    await pass_at(controller, clock, 1259)
    assert sent == []
    await pass_at(controller, clock, 1260)
    assert len(sent) == 1


async def test_wol_max_tries_from_the_panel_applies_at_once(app, db_session, k8s, controller, clock, sent):
    await setup(db_session, k8s)
    await put(app, "wol_max_tries", 2)
    await pass_at(controller, clock, 1000)
    for t in (1060, 1120, 1240, 99999):
        await pass_at(controller, clock, t)
    assert len(sent) == 2  # gives up after two, not the six of the .env
    assert (await timers(app))["wake:pc01"] == (1000, "2")
    await put(app, "wol_max_tries", 3)  # raised: it tries once more
    await pass_at(controller, clock, 100000)
    assert len(sent) == 3
