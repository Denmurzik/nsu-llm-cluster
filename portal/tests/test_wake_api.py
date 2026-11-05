"""Wake-on-LAN (task 2-18): the NIC a node reports, the «Разбудить» button and the wake state on the admin pages."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models import AuditLog, ControllerTimer, ModelDeployment, NodeAssignment, NodeWake, PortalFlag, StandaloneNode
from app.nodes import wake
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

AUTH = {"Authorization": "Bearer agent-test"}
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}
NIC = {"mac": "00-1A-2B-3C-4D-5E", "ip": "192.168.0.193", "prefix": 24}
REG = {"name": "win-07", "url": "http://192.168.3.77:18080", "accel": "cpu", "ram_mib": 16384, "version": "0.1.0"}


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    async with app.state.sessionmaker() as db:
        for table in (NodeWake, ControllerTimer, NodeAssignment, StandaloneNode, ModelDeployment, PortalFlag):
            await db.execute(delete(table))
        await db.commit()
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()
    app.state.backend_status = {"at": "t", "backends": []}


class Sent(list):
    """Magic packets the portal sent: (mac, targets); `fail` makes every send fail."""

    fail = False


@pytest.fixture
def sent(monkeypatch):
    calls = Sent()

    def fake_send(mac, addrs):
        calls.append((mac, list(addrs)))
        return [f"{a}:{p}: unreachable" for a in addrs for p in wake.PORTS] if calls.fail else []

    monkeypatch.setattr(wake, "send", fake_send)
    return calls


async def rows(app) -> dict[str, tuple]:
    async with app.state.sessionmaker() as db:
        return {r.node_name: (r.mac, r.ip, r.prefix) for r in (await db.scalars(select(NodeWake))).all()}


# ---- the node reports its NIC -------------------------------------------------------------------------


async def test_k3s_node_reports_its_nic(client, app):
    assert (await client.post("/api/nodes/wake", json={"name": "pc01", **NIC})).status_code == 401
    r = await client.post("/api/nodes/wake", json={"name": "pc01", **NIC}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert await rows(app) == {"pc01": ("00:1a:2b:3c:4d:5e", "192.168.0.193", 24)}
    # a new adapter or subnet replaces the old one
    r = await client.post("/api/nodes/wake", json={"name": "pc01", "mac": "00:1a:2b:3c:4d:5f", "ip": "10.1.2.3", "prefix": 23}, headers=AUTH)
    assert r.status_code == 200
    assert await rows(app) == {"pc01": ("00:1a:2b:3c:4d:5f", "10.1.2.3", 23)}


@pytest.mark.parametrize(
    "bad",
    [
        {"mac": "ff:ff:ff:ff:ff:ff"},
        {"mac": "00:1a:2b"},
        {"ip": "127.0.0.1"},
        {"ip": "224.0.0.1"},
        {"ip": "0.0.0.0"},
        {"ip": "nsu.ru"},
        {"prefix": 0},
        {"prefix": 33},
        {"name": "Bad_Name"},
    ],
)
async def test_bad_nic_reports_are_refused(client, app, bad):
    r = await client.post("/api/nodes/wake", json={"name": "pc01", **NIC, **bad}, headers=AUTH)
    assert r.status_code == 422, r.text
    assert await rows(app) == {}


async def test_standalone_node_reports_its_nic_in_register_and_heartbeat(client, app):
    r = await client.post("/api/nodes/register", json={**REG, "wake": NIC}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert await rows(app) == {"win-07": ("00:1a:2b:3c:4d:5e", "192.168.0.193", 24)}
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07", "wake": {**NIC, "prefix": 22}}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert await rows(app) == {"win-07": ("00:1a:2b:3c:4d:5e", "192.168.0.193", 22)}
    # a heartbeat without it (an older node image) keeps what is stored
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07"}, headers=AUTH)
    assert r.status_code == 200
    assert await rows(app) == {"win-07": ("00:1a:2b:3c:4d:5e", "192.168.0.193", 22)}
    # a malformed one never costs the node its heartbeat: ignored, the stored one stays
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07", "wake": {**NIC, "mac": "nope"}}, headers=AUTH)
    assert r.status_code == 200, r.text
    r = await client.post("/api/nodes/register", json={**REG, "wake": {"mac": "x"}}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert await rows(app) == {"win-07": ("00:1a:2b:3c:4d:5e", "192.168.0.193", 22)}


async def test_heartbeat_of_an_unknown_node_stores_no_nic(client, app):
    r = await client.post("/api/nodes/heartbeat", json={"name": "ghost", "wake": NIC}, headers=AUTH)
    assert r.status_code == 404
    assert await rows(app) == {}


async def test_uninstalled_node_forgets_its_nic(client, app):
    """DELETE /api/nodes/<name> (run-node -Uninstall, the registrar stopping) removes it: never woken again."""
    await client.post("/api/nodes/wake", json={"name": "pc01", **NIC}, headers=AUTH)
    await client.post("/api/nodes/register", json={**REG, "wake": NIC}, headers=AUTH)
    assert (await client.delete("/api/nodes/pc01", headers=AUTH)).status_code == 204
    assert (await client.delete("/api/nodes/win-07", headers=AUTH)).status_code == 204
    assert await rows(app) == {}


# ---- the admin pages ------------------------------------------------------------------------------


async def test_nodes_and_status_show_wake_state(client, app):
    k8s = app.state.k8s
    k8s.add_node("llm-main", accel="cpu", internal_ip="192.168.3.21", is_main=True)
    k8s.add_node("pc01", accel="cpu", internal_ip="192.168.0.193", ready=False)
    k8s.add_node("pc02", accel="cpu", internal_ip="192.168.0.194", ready=False)
    k8s.add_node("pc03", accel="cpu", internal_ip="192.168.0.195")
    since = datetime(2026, 11, 4, 12, 0, tzinfo=UTC).timestamp()
    async with app.state.sessionmaker() as db:
        db.add_all(
            [
                NodeWake(node_name="pc01", mac="00:1a:2b:3c:4d:5e", ip="192.168.0.193", prefix=24),
                NodeWake(node_name="pc02", mac="00:1a:2b:3c:4d:5f", ip="192.168.0.194", prefix=24),
                ControllerTimer(key="wake:pc01", since=since, members="2"),
                ControllerTimer(key="wake:pc02", since=since, members="6"),
                ControllerTimer(key="notready:pc01", since=since),
            ]
        )
        await db.commit()
    h = csrf_headers(await login(client, app, ADMIN))
    for url in ("/api/admin/nodes", "/api/admin/status"):
        r = await client.get(url, headers=h)
        assert r.status_code == 200, r.text
        nodes = {n["name"]: n for n in r.json()["nodes"]}
        assert nodes["pc01"]["wakeable"] is True
        assert nodes["pc01"]["wake"] == {"tries": 2, "max_tries": 6, "since": "2026-11-04T12:00:00+00:00"}
        assert nodes["pc02"]["wake"] == {"tries": 6, "max_tries": 6, "since": "2026-11-04T12:00:00+00:00"}
        assert (nodes["pc03"]["wakeable"], nodes["pc03"]["wake"]) == (False, None)
        assert (nodes["llm-main"]["wakeable"], nodes["llm-main"]["wake"]) == (False, None)


async def test_wake_button_sends_the_packet_now(client, app, sent, settings):
    settings.wol_broadcast_override = "192.168.0.0/24=192.168.0.7"
    app.state.k8s.add_node("pc01", accel="cpu", internal_ip="192.168.0.200", ready=False)
    async with app.state.sessionmaker() as db:
        db.add(NodeWake(node_name="pc01", mac="00:1a:2b:3c:4d:5e", ip="192.168.0.193", prefix=24))
        await db.commit()
    h = csrf_headers(await login(client, app, ADMIN))
    assert (await client.post("/api/admin/nodes/pc01/wake")).status_code == 403  # no CSRF header
    r = await client.post("/api/admin/nodes/PC01/wake", headers=h)
    assert r.status_code == 200, r.text
    # the override of its subnet, then the address the cluster knows now (DHCP may have moved it)
    assert r.json() == {"node": "pc01", "targets": ["192.168.0.7", "192.168.0.200"], "relay": None}
    assert sent == [("00:1a:2b:3c:4d:5e", ["192.168.0.7", "192.168.0.200"])]
    async with app.state.sessionmaker() as db:
        (row,) = (await db.scalars(select(AuditLog).where(AuditLog.action == "node.wake"))).all()
    assert (row.actor_email, row.target, row.details) == (
        "admin@g.nsu.ru", "pc01", {"targets": ["192.168.0.7", "192.168.0.200"], "relay": None},
    )


async def test_wake_button_without_a_reported_nic(client, app, sent):
    h = csrf_headers(await login(client, app, ADMIN))
    r = await client.post("/api/admin/nodes/pc09/wake", headers=h)
    assert r.status_code == 404
    assert "сетевой карт" in r.json()["detail"]
    assert sent == []


async def test_wake_button_when_nothing_could_be_sent(client, app, sent):
    sent.fail = True
    async with app.state.sessionmaker() as db:
        db.add(NodeWake(node_name="pc01", mac="00:1a:2b:3c:4d:5e", ip="192.168.0.193", prefix=24))
        await db.commit()
    h = csrf_headers(await login(client, app, ADMIN))
    r = await client.post("/api/admin/nodes/pc01/wake", headers=h)
    assert r.status_code == 502
    assert r.json()["detail"].startswith("Пакет пробуждения не отправлен")


async def test_wake_button_is_for_admins(client, app, sent):
    h = csrf_headers(await login(client, app, USER))
    assert (await client.post("/api/admin/nodes/pc01/wake", headers=h)).status_code == 403
    assert sent == []


async def test_standalone_node_without_cluster_is_still_listed_with_wake(client, app):
    app.state.k8s.unavailable = True
    async with app.state.sessionmaker() as db:
        db.add(StandaloneNode(name="win-07", url="http://192.168.3.77:18080", accel="cpu", version="v",
                              last_seen_at=datetime.now(UTC) - timedelta(minutes=5)))
        db.add(NodeWake(node_name="win-07", mac="00:1a:2b:3c:4d:5e", ip="192.168.3.77", prefix=24))
        await db.commit()
    h = csrf_headers(await login(client, app, ADMIN))
    node = (await client.get("/api/admin/nodes", headers=h)).json()["nodes"][0]
    assert (node["name"], node["ready"], node["wakeable"], node["wake"]) == ("win-07", False, True, None)


# ---- the fallback through a live node of the subnet -------------------------------------------------------


async def test_heartbeat_hands_a_standalone_relay_its_packets_once(client, app):
    await client.post("/api/nodes/register", json=REG, headers=AUTH)
    cmd = {"target": "pc01", "mac": "00:1a:2b:3c:4d:5e", "broadcast": "192.168.3.255"}
    await app.state.redis.rpush("wol:relay:win-07", json.dumps(cmd))
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["wake"] == [{"mac": "00:1a:2b:3c:4d:5e", "broadcast": "192.168.3.255"}]
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07"}, headers=AUTH)
    assert "wake" not in r.json()  # taken: an older registrar never sees the key


async def test_wake_button_also_goes_through_a_live_node_of_the_subnet(client, app, sent):
    k8s = app.state.k8s
    k8s.add_node("pc01", accel="cpu", internal_ip="192.168.0.193", ready=False)
    k8s.add_node("pc02", accel="nvidia", internal_ip="192.168.0.194")
    async with app.state.sessionmaker() as db:
        db.add(NodeWake(node_name="pc01", mac="00:1a:2b:3c:4d:5e", ip="192.168.0.193", prefix=24))
        await db.commit()
    h = csrf_headers(await login(client, app, ADMIN))
    r = await client.post("/api/admin/nodes/pc01/wake", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"node": "pc01", "targets": ["192.168.0.255", "192.168.0.193"], "relay": "pc02"}
    (job,) = k8s.jobs
    pod = job["spec"]["template"]["spec"]
    assert pod["nodeName"] == "pc02" and pod["containers"][0]["command"] == ["wol-send.sh", "00:1a:2b:3c:4d:5e", "192.168.0.255"]


async def test_wake_button_without_a_relay_or_with_via_node_off(client, app, sent, settings):
    app.state.k8s.add_node("pc01", accel="cpu", internal_ip="192.168.0.193", ready=False)
    app.state.k8s.add_node("pc02", accel="cpu", internal_ip="192.168.0.194")
    async with app.state.sessionmaker() as db:
        db.add(NodeWake(node_name="pc01", mac="00:1a:2b:3c:4d:5e", ip="192.168.0.193", prefix=24))
        await db.commit()
    settings.wol_via_node = "off"
    h = csrf_headers(await login(client, app, ADMIN))
    r = await client.post("/api/admin/nodes/pc01/wake", headers=h)
    assert r.json()["relay"] is None and app.state.k8s.jobs == []


# ---- stopped is not uninstalled (fix round 1) --------------------------------------------------------------


async def test_clean_stop_takes_the_node_out_of_routing_and_keeps_its_nic(client, app):
    from app.deployments import service

    await client.post("/api/nodes/register", json={**REG, "wake": NIC}, headers=AUTH)
    assert (await client.post("/api/nodes/win-07/stop")).status_code == 401
    r = await client.post("/api/nodes/win-07/stop", headers=AUTH)
    assert r.status_code == 204
    async with app.state.sessionmaker() as db:
        assert [n.name for n in await service.live_nodes(db, 45)] == []  # out of routing at once
        row = await db.get(StandaloneNode, "win-07")
        assert row is not None and row.stopped_at is not None and row.applied_spec_hash is None
    assert await rows(app) == {"win-07": ("00:1a:2b:3c:4d:5e", "192.168.0.193", 24)}  # the NIC stays
    h = csrf_headers(await login(client, app, ADMIN))
    node = next(n for n in (await client.get("/api/admin/nodes", headers=h)).json()["nodes"] if n["name"] == "win-07")
    assert (node["ready"], node["wakeable"]) == (False, True)
    # back: the next heartbeat (or registration) makes it live again
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07"}, headers=AUTH)
    assert r.status_code == 200
    async with app.state.sessionmaker() as db:
        assert [n.name for n in await service.live_nodes(db, 45)] == ["win-07"]
    # a stop of an unknown node changes nothing and is not an error (an old record purged meanwhile)
    assert (await client.post("/api/nodes/ghost/stop", headers=AUTH)).status_code == 204


async def test_register_after_a_stop_is_live_again(client, app):
    from app.deployments import service

    await client.post("/api/nodes/register", json=REG, headers=AUTH)
    await client.post("/api/nodes/win-07/stop", headers=AUTH)
    await client.post("/api/nodes/register", json=REG, headers=AUTH)
    async with app.state.sessionmaker() as db:
        assert [n.name for n in await service.live_nodes(db, 45)] == ["win-07"]


async def test_purge_of_a_silent_standalone_record_keeps_its_nic(client, app):
    from app.deployments import service

    await client.post("/api/nodes/register", json={**REG, "wake": NIC}, headers=AUTH)
    async with app.state.sessionmaker() as db:
        row = await db.get(StandaloneNode, "win-07")
        row.last_seen_at = datetime.now(UTC) - timedelta(hours=25)
        await db.commit()
        assert await service.purge_standalone(db, [], 86400) == ["win-07"]
        assert await db.get(StandaloneNode, "win-07") is None
    assert await rows(app) == {"win-07": ("00:1a:2b:3c:4d:5e", "192.168.0.193", 24)}  # Monday's wake still knows the PC
