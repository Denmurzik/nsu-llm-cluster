"""Wake-on-LAN by the workload controller (task 2-18): which down nodes get a magic packet, when, and how often."""

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import fakeredis
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.catalog.gguf import GgufMeta
from app.catalog.hf import HfFile
from app.controllers import workloads as workloads_module
from app.controllers.workloads import WorkloadController
from app.deployments import schedule, service
from app.deployments.service import DeploymentIn
from app.models import ControllerTimer, ModelDeployment, NodeAssignment, NodeWake, PortalFlag, StandaloneNode
from app.nodes import wake
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s

pytestmark = pytest.mark.docker

GiB = 2**30
REPO, FILE = "org/model-GGUF", "model-Q4_K_M.gguf"
MAC = "00:1a:2b:3c:4d:5e"
# 2026-11-04 is a Wednesday (ISO day 3); 12:00 in Novosibirsk
NOON = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)


def make_hf() -> FakeHf:
    return FakeHf(files={REPO: [HfFile(FILE, 4 * GiB)]}, metas={(REPO, FILE): GgufMeta("llama", 32, 32, 8, 128, 4096, 32768)})


@pytest.fixture
def settings(settings):
    settings.hf_endpoint = "http://127.0.0.1:9"
    return settings


@pytest_asyncio.fixture(autouse=True)
async def clean_slate(app):
    async with app.state.sessionmaker() as db:
        for table in (NodeWake, ControllerTimer, NodeAssignment, StandaloneNode, ModelDeployment, PortalFlag):
            await db.execute(delete(table))
        await db.commit()


@pytest.fixture
def k8s():
    return FakeK8s()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(workloads_module, "_now", c)
    monkeypatch.setattr(schedule, "now_utc", lambda: NOON)
    return c


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple[str, list[str]]] = []

    def fake_send(mac, addrs):
        calls.append((mac, list(addrs)))
        return []

    monkeypatch.setattr(wake, "send", fake_send)
    return calls


@pytest.fixture
def state(app, k8s, settings):
    redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    return SimpleNamespace(settings=settings, sessionmaker=app.state.sessionmaker, k8s=k8s, hf=make_hf(), redis=redis)


@pytest.fixture
def controller(state):
    return WorkloadController(state)


async def setup(db_session, k8s, *, ready=False, schedule_=None, nic=True, **dep_kw):
    """Deployment m1 with node pc01 (k8s, cpu) assigned to it; pc01 reported its NIC."""
    k8s.add_node("llm-main", is_main=True)
    k8s.add_node("pc01", accel="cpu", internal_ip="10.1.2.30", ready=ready)
    dep = await service.create(
        db_session, make_hf(), DeploymentIn(name="m1", repo=REPO, file=FILE, nodes=["pc01"], schedule=schedule_, **dep_kw)
    )
    if nic:
        db_session.add(NodeWake(node_name="pc01", mac=MAC, ip="10.1.2.3", prefix=24))
        await db_session.commit()
    return dep


async def timers(app) -> dict[str, tuple[float, str]]:
    async with app.state.sessionmaker() as db:
        return {t.key: (t.since, t.members) for t in (await db.scalars(select(ControllerTimer))).all()}


async def pass_at(controller, clock, t):
    clock.now = t
    await controller.reconcile_once()


async def test_down_node_is_woken_after_the_delay_then_with_backoff(app, db_session, k8s, controller, clock, sent, caplog):
    await setup(db_session, k8s)
    caplog.set_level(logging.INFO, logger="app.controllers.workloads")
    await pass_at(controller, clock, 1000)  # first seen down
    await pass_at(controller, clock, 1059)
    assert sent == []
    await pass_at(controller, clock, 1060)
    # the subnet's directed broadcast, then the address the cluster knows now
    assert sent == [(MAC, ["10.1.2.255", "10.1.2.30"])]
    assert "pc01" in caplog.text and "attempt 1 of 6" in caplog.text
    await pass_at(controller, clock, 1100)
    assert len(sent) == 1
    for t, n in ((1120, 2), (1240, 3), (1480, 4), (1960, 5), (2920, 6), (99999, 6)):
        await pass_at(controller, clock, t)
        assert len(sent) == n, t
    assert (await timers(app))["wake:pc01"] == (1000, "6")  # given up, the pages say so until it comes back
    k8s.nodes["pc01"].ready = True
    await pass_at(controller, clock, 100000)
    assert "wake:pc01" not in await timers(app)


async def test_attempts_survive_a_controller_restart(app, db_session, k8s, state, clock, sent):
    await setup(db_session, k8s)
    await pass_at(WorkloadController(state), clock, 1000)
    await pass_at(WorkloadController(state), clock, 1060)
    assert len(sent) == 1
    fresh = WorkloadController(state)
    await pass_at(fresh, clock, 1061)
    assert len(sent) == 1  # attempt 2 is due at 1120, not again now
    await pass_at(fresh, clock, 1120)
    assert len(sent) == 2
    assert (await timers(app))["wake:pc01"] == (1000, "2")


async def test_wake_timers_live_beside_the_other_pauses(app, db_session, k8s, controller, clock, sent):
    await setup(db_session, k8s)
    await pass_at(controller, clock, 1000)
    keys = set(await timers(app))
    assert {"notready:pc01", "wake:pc01"} <= keys


async def test_ready_node_is_not_woken(db_session, k8s, controller, clock, sent):
    await setup(db_session, k8s, ready=True)
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 5000)
    assert sent == []


async def test_node_on_maintenance_is_not_woken(app, db_session, k8s, controller, clock, sent):
    await setup(db_session, k8s)
    await pass_at(controller, clock, 1000)
    await service.set_maintenance(db_session, "pc01", True)
    await pass_at(controller, clock, 1060)
    assert sent == []
    assert "wake:pc01" not in await timers(app)


async def test_node_outside_its_working_window_is_not_woken(db_session, k8s, controller, clock, sent):
    await setup(db_session, k8s, schedule_={"days": [1, 2], "start": "09:00", "end": "18:00"})  # not on Wednesday
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 5000)
    assert sent == []


async def test_node_inside_its_working_window_is_woken(db_session, k8s, controller, clock, sent):
    await setup(db_session, k8s, schedule_={"days": [3], "start": "09:00", "end": "18:00"})
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1060)
    assert len(sent) == 1


async def test_node_of_a_disabled_deployment_is_not_woken(db_session, k8s, controller, clock, sent):
    await setup(db_session, k8s, enabled=False)
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 5000)
    assert sent == []


async def test_unassigned_node_is_not_woken(db_session, k8s, controller, clock, sent):
    k8s.add_node("pc01", accel="cpu", ready=False)
    db_session.add(NodeWake(node_name="pc01", mac=MAC, ip="10.1.2.3", prefix=24))
    await db_session.commit()
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 5000)
    assert sent == []


async def test_node_without_a_reported_nic_is_not_woken(db_session, k8s, controller, clock, sent):
    await setup(db_session, k8s, nic=False)
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 5000)
    assert sent == []


async def test_wol_disabled_sends_nothing(app, db_session, k8s, controller, clock, sent, settings):
    settings.wol_enabled = False
    await setup(db_session, k8s)
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 5000)
    assert sent == []
    assert not any(k.startswith("wake:") for k in await timers(app))


async def test_override_and_no_unicast(db_session, k8s, controller, clock, sent, settings):
    settings.wol_broadcast_override = "10.1.0.0/16=10.1.255.255"
    settings.wol_unicast = False
    await setup(db_session, k8s)
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1060)
    assert sent == [(MAC, ["10.1.255.255"])]


async def test_send_errors_are_logged_and_the_attempt_counts(app, db_session, k8s, controller, clock, monkeypatch, caplog):
    monkeypatch.setattr(wake, "send", lambda mac, addrs: [f"{a}:9: [Errno 101] Network is unreachable" for a in addrs])
    await setup(db_session, k8s)
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1060)
    assert "Network is unreachable" in caplog.text
    assert (await timers(app))["wake:pc01"] == (1000, "1")


async def test_stale_standalone_node_is_woken(db_session, k8s, controller, clock, sent):
    k8s.add_node("llm-main", is_main=True)
    dep = await service.create(db_session, make_hf(), DeploymentIn(name="m1", repo=REPO, file=FILE, auto_assign=False))
    old = datetime.now(UTC) - timedelta(minutes=10)
    db_session.add_all(
        [
            StandaloneNode(name="win-07", url="http://192.168.3.77:18080", accel="cpu", version="v", last_seen_at=old),
            StandaloneNode(name="win-08", url="http://192.168.3.78:18080", accel="cpu", version="v"),  # alive
            NodeAssignment(node_name="win-07", deployment_id=dep.id, source="standalone", manual=True),
            NodeAssignment(node_name="win-08", deployment_id=dep.id, source="standalone", manual=True),
            NodeWake(node_name="win-07", mac=MAC, ip="192.168.3.77", prefix=24),
            NodeWake(node_name="win-08", mac="00:1a:2b:3c:4d:5f", ip="192.168.3.78", prefix=24),
        ]
    )
    await db_session.commit()
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1060)
    assert sent == [(MAC, ["192.168.3.255", "192.168.3.77"])]


async def test_unavailable_cluster_wakes_nothing(db_session, k8s, controller, clock, sent):
    await setup(db_session, k8s)
    await pass_at(controller, clock, 1000)
    k8s.unavailable = True
    await pass_at(controller, clock, 1060)
    assert sent == []


# ---- the fallback: a live node of the same subnet sends the packet ---------------------------------------


def job_of(job) -> tuple[str, list[str]]:
    pod = job["spec"]["template"]["spec"]
    return pod["nodeName"], pod["containers"][0]["command"]


async def test_after_mains_two_attempts_a_live_node_of_the_subnet_sends_it(db_session, k8s, controller, clock, sent, caplog):
    await setup(db_session, k8s)
    k8s.add_node("pc03", accel="nvidia", internal_ip="10.1.2.33")
    k8s.add_node("pc02", accel="cpu", internal_ip="10.1.2.32")  # first by name: the relay
    k8s.add_node("pc04", accel="cpu", internal_ip="10.1.9.4")  # another subnet
    caplog.set_level(logging.INFO, logger="app.controllers.workloads")
    for t in (1000, 1060, 1120):
        await pass_at(controller, clock, t)
    assert len(sent) == 2 and k8s.jobs == []  # main's own packets first
    await pass_at(controller, clock, 1240)
    assert len(sent) == 3  # main keeps sending too
    assert [job_of(j) for j in k8s.jobs] == [("pc02", ["wol-send.sh", MAC, "10.1.2.255"])]
    assert "via node pc02" in caplog.text


async def test_via_node_always_from_the_first_attempt(db_session, k8s, controller, clock, sent, settings):
    settings.wol_via_node = "always"
    await setup(db_session, k8s)
    k8s.add_node("pc02", accel="cpu", internal_ip="10.1.2.32")
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1060)
    assert [job_of(j)[0] for j in k8s.jobs] == ["pc02"]


async def test_via_node_off(db_session, k8s, controller, clock, sent, settings):
    settings.wol_via_node = "off"
    await setup(db_session, k8s)
    k8s.add_node("pc02", accel="cpu", internal_ip="10.1.2.32")
    for t in (1000, 1060, 1120, 1240, 1480):
        await pass_at(controller, clock, t)
    assert k8s.jobs == []


async def test_a_down_node_or_the_reported_nic_decides_the_subnet(db_session, k8s, controller, clock, sent, settings):
    settings.wol_via_node = "always"
    await setup(db_session, k8s)
    k8s.add_node("pc02", accel="cpu", internal_ip="10.1.2.32", ready=False)  # down itself: no relay
    k8s.add_node("pc05", accel="cpu", internal_ip="10.9.9.5")  # the cluster sees another address,
    db_session.add(NodeWake(node_name="pc05", mac="00:1a:2b:3c:4d:60", ip="10.1.2.5", prefix=24))  # its NIC is here
    await db_session.commit()
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1060)
    assert [job_of(j)[0] for j in k8s.jobs] == ["pc05"]


async def test_standalone_relay_gets_the_command_through_redis(db_session, k8s, state, controller, clock, sent, settings):
    settings.wol_via_node = "always"
    await setup(db_session, k8s)
    db_session.add(StandaloneNode(name="win-08", url="http://10.1.2.8:18080", accel="cpu", version="v"))
    await db_session.commit()
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1060)
    assert k8s.jobs == []
    queued = [json.loads(x) for x in await state.redis.lrange("wol:relay:win-08", 0, -1)]
    assert queued == [{"target": "pc01", "mac": MAC, "broadcast": "10.1.2.255"}]
    assert 0 < await state.redis.ttl("wol:relay:win-08") <= 300


async def test_no_live_node_in_the_subnet_is_logged(db_session, k8s, controller, clock, sent, settings, caplog):
    settings.wol_via_node = "always"
    caplog.set_level(logging.INFO, logger="app.controllers.workloads")
    await setup(db_session, k8s)
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1060)
    assert len(sent) == 1 and k8s.jobs == []
    assert "no live node in 10.1.2.0/24" in caplog.text


# ---- stopped standalone PCs are woken; uninstalled ones are not (fix round 1) ------------------------------


async def standalone_setup(db_session, **row):
    dep = await service.create(db_session, make_hf(), DeploymentIn(name="m1", repo=REPO, file=FILE, auto_assign=False))
    db_session.add_all(
        [
            NodeAssignment(node_name="win-07", deployment_id=dep.id, source="standalone", manual=True),
            NodeWake(node_name="win-07", mac=MAC, ip="192.168.3.77", prefix=24),
        ]
    )
    if row:
        db_session.add(StandaloneNode(name="win-07", url="http://192.168.3.77:18080", accel="cpu", version="v", **row))
    await db_session.commit()


async def test_cleanly_stopped_standalone_node_is_woken_after_the_delay(db_session, k8s, controller, clock, sent):
    k8s.add_node("llm-main", is_main=True)
    await standalone_setup(db_session, stopped_at=datetime.now(UTC))  # heartbeat fresh, but it said it stops
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1059)
    assert sent == []
    await pass_at(controller, clock, 1060)
    assert sent == [(MAC, ["192.168.3.255", "192.168.3.77"])]


async def test_standalone_pc_off_over_the_weekend_is_still_woken(db_session, k8s, controller, clock, sent):
    """The standalone record was purged after 24 h (2-19); the NIC and the assignment are enough."""
    k8s.add_node("llm-main", is_main=True)
    await standalone_setup(db_session)  # no StandaloneNode row at all
    await pass_at(controller, clock, 1000)
    await pass_at(controller, clock, 1060)
    assert sent == [(MAC, ["192.168.3.255", "192.168.3.77"])]


async def test_uninstalled_standalone_node_is_never_woken(app, db_session, k8s, controller, clock, sent):
    from app.nodes import registry

    k8s.add_node("llm-main", is_main=True)
    await standalone_setup(db_session, stopped_at=datetime.now(UTC))
    await registry.remove(db_session, "win-07")  # DELETE /api/nodes/win-07 of run-node -Uninstall
    for t in (1000, 1060, 5000):
        await pass_at(controller, clock, t)
    assert sent == []
