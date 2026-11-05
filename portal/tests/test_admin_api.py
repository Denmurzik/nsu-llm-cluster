import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import delete

from app.models import ModelDeployment, NodeAssignment, PortalFlag, StandaloneNode
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}
MiB = 2**20
GiB = 2**30


@pytest.fixture
def settings(settings):
    settings.hf_endpoint = "http://127.0.0.1:9"
    settings.main_advertise_ip = "192.168.3.21"
    return settings


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(NodeAssignment))
        await db.execute(delete(StandaloneNode))
        await db.execute(delete(ModelDeployment))
        await db.execute(delete(PortalFlag))
        await db.commit()
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()
    app.state.backend_status = {"at": "2026-11-02T10:00:00+00:00", "backends": []}


async def as_admin(client, app) -> dict:
    return csrf_headers(await login(client, app, ADMIN))


async def test_admin_endpoints_forbid_regular_user(client, app):
    h = csrf_headers(await login(client, app, USER))
    for url in ("/api/admin/nodes", "/api/admin/join", "/api/admin/users", "/api/admin/pods/p1/logs"):
        r = await client.get(url, headers=h)
        assert r.status_code == 403, url
        assert r.json()["detail"] == "Недостаточно прав", url


async def test_nodes_merges_k8s_standalone_assignment_and_capacity(client, app):
    k8s = app.state.k8s
    k8s.add_node("llm-main", accel="cpu", internal_ip="192.168.3.21", mem_mib=32768, is_main=True)
    k8s.add_node("pc01", accel="cpu", internal_ip="192.168.3.41", mem_mib=16384)
    k8s.add_node("gpu01", accel="nvidia", internal_ip="192.168.3.64", vram_mib=12288, mem_mib=32768)
    k8s.add_node("wan01", accel="cpu", internal_ip="10.42.0.9", external_ip="192.168.3.90")  # LAN address wins
    now = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        dep = ModelDeployment(name="m1", repo="o/r", file="f.gguf", ctx_per_request=4096, parallel=1)
        db.add(dep)
        await db.flush()
        db.add_all(
            [
                NodeAssignment(node_name="pc01", deployment_id=dep.id, source="k8s", manual=True),
                NodeAssignment(node_name="win-07", deployment_id=dep.id, source="standalone", manual=False),
                StandaloneNode(
                    name="win-07",
                    url="http://192.168.3.77:18080",
                    accel="cpu",
                    vram_mib=None,
                    ram_mib=16384,
                    version="0.1.0",
                    last_seen_at=now,
                ),
                StandaloneNode(
                    name="win-08",
                    url="http://192.168.3.78:18080",
                    accel="nvidia",
                    vram_mib=8192,
                    ram_mib=16384,
                    version="0.1.0",
                    last_seen_at=now - timedelta(seconds=600),
                ),
            ]
        )
        await db.commit()
    backend = {
        "id": "pm-1",
        "model_name": "m1",
        "api_base": "http://192.168.3.41:18080/v1",
        "source": "k8s",
        "node": "pc01",
        "max_input_tokens": 4096,
    }
    app.state.backend_status = {"at": "t", "backends": [backend]}

    h = await as_admin(client, app)
    r = await client.get("/api/admin/nodes", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["k8s_available"] is True
    assert body["backends"] == [backend]
    nodes = {n["name"]: n for n in body["nodes"]}
    assert set(nodes) == {"llm-main", "pc01", "gpu01", "wan01", "win-07", "win-08"}
    assert nodes["wan01"]["ip"] == "192.168.3.90"
    assert nodes["llm-main"]["is_main"] is True
    assert nodes["pc01"] == {
        "name": "pc01",
        "source": "k8s",
        "accel": "cpu",
        "ip": "192.168.3.41",
        "ready": True,
        "vram_mib": None,
        "mem_mib": 16384,
        "capacity_bytes": 16384 * MiB * 8 // 10 - GiB,
        "deployment": "m1",
        "backends": 1,
        "version": "v1.36.5+k3s1",
        "is_main": False,
        "maintenance": False,
        "models_bytes": None,
        "wakeable": False,
        "wake": None,
        # approval is off in the tests' settings: a cluster node without an identity counts as approved (task SEC-1)
        "approved": True,
        "has_key": False,
        "claim_ip": None,
        "legacy": False,
        "claims": [],
        "known_addresses": [],
        "claims_refused": 0,
    }
    assert nodes["gpu01"]["capacity_bytes"] == 12288 * MiB - 1536 * MiB
    assert nodes["gpu01"]["deployment"] is None and nodes["gpu01"]["backends"] == 0
    win = nodes["win-07"]
    assert (win["source"], win["ip"], win["ready"], win["deployment"]) == ("standalone", "192.168.3.77", True, "m1")
    assert (win["mem_mib"], win["version"], win["is_main"]) == (16384, "0.1.0", False)
    assert win["capacity_bytes"] == 16384 * MiB * 8 // 10 - GiB
    assert nodes["win-08"]["ready"] is False
    assert nodes["win-08"]["capacity_bytes"] == 8192 * MiB - 1536 * MiB


async def test_a_cluster_node_wins_over_a_standalone_record_of_the_same_name(client, app):
    app.state.k8s.add_node("pc01", accel="nvidia", internal_ip="192.168.3.41", vram_mib=8188, mem_mib=16384, ready=False)
    async with app.state.sessionmaker() as db:
        db.add(StandaloneNode(name="pc01", url="http://192.168.3.41:18080", accel="cpu", version="0.1.0",
                              last_seen_at=datetime.now(UTC)))
        await db.commit()
    r = await client.get("/api/admin/nodes", headers=await as_admin(client, app))
    rows = [n for n in r.json()["nodes"] if n["name"] == "pc01"]
    assert [(n["source"], n["accel"]) for n in rows] == [("k8s", "nvidia")]


async def test_nodes_when_cluster_unavailable_lists_standalone_only(client, app):
    app.state.k8s.add_node("pc01", accel="cpu")
    app.state.k8s.unavailable = True
    async with app.state.sessionmaker() as db:
        db.add(StandaloneNode(name="win-07", url="http://192.168.3.77:18080", accel="cpu", ram_mib=8192, version="v"))
        await db.commit()
    h = await as_admin(client, app)
    body = (await client.get("/api/admin/nodes", headers=h)).json()
    assert body["k8s_available"] is False
    assert [n["name"] for n in body["nodes"]] == ["win-07"]


async def test_join_returns_the_agent_token_and_commands(client, app):
    token = "K10abc::node:agent-test"  # /var/lib/rancher/k3s/server/agent-token, written by k3s
    app.state.k8s.agent_token = token
    h = await as_admin(client, app)
    body = (await client.get("/api/admin/join", headers=h)).json()
    assert body["agent_token"] == token
    assert "cluster_token" not in body
    assert body["main_host"] == "192.168.3.21"
    assert body["linux"] == f"sudo ./run-node.sh --main 192.168.3.21 --token {token}"
    assert body["linux_standalone"] == f"sudo ./run-node.sh --main 192.168.3.21 --token {token} --mode standalone"
    assert body["windows"] == f".\\run-node.ps1 -Main 192.168.3.21 -Token {token}"

    # before k3s has written the file: the raw agent token
    app.state.k8s.agent_token = None
    app.state.settings.main_advertise_ip = ""
    body = (await client.get("/api/admin/join", headers=h)).json()
    host = app.state.settings.public_hostname
    assert body["agent_token"] == "agent-test"
    assert body["main_host"] == host
    assert f"--main {host} --token agent-test" in body["linux"]


async def test_join_never_shows_the_server_token(client, app):
    h = await as_admin(client, app)
    # right after an upgrade agent-token is still k3s's link to server/token until k3s restarts with the agent token
    app.state.k8s.agent_token = "K10abc::server:tok-test"
    body = (await client.get("/api/admin/join", headers=h)).json()
    assert body["agent_token"] == "agent-test"
    assert "tok-test" not in str(body)

    app.state.settings.agent_token = SecretStr("")
    r = await client.get("/api/admin/join", headers=h)
    assert r.status_code == 503
    assert r.json()["detail"] == "Токен подключения нод не создан: перезапустите главный сервер"
    assert "tok-test" not in r.text


async def test_logs_only_for_owned_pods_and_line_bounds(client, app):
    k8s = app.state.k8s
    async with app.state.sessionmaker() as db:
        db.add(ModelDeployment(name="m1", repo="o/r", file="f.gguf", ctx_per_request=4096, parallel=1))
        await db.commit()
    k8s.add_pod("llm-m1-cpu-abc", "pc01", "inference", "m1")
    k8s.add_pod("litellm-123", "llm-main", "inference", "other")  # not ours: no known deployment
    k8s.logs["llm-m1-cpu-abc"] = "\n".join(f"line {i}" for i in range(300))
    k8s.logs["litellm-123"] = "secret"

    h = await as_admin(client, app)
    r = await client.get("/api/admin/pods/llm-m1-cpu-abc/logs", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "llm-m1-cpu-abc"
    lines = body["lines"].splitlines()
    assert len(lines) == 200 and lines[-1] == "line 299"
    r = await client.get("/api/admin/pods/llm-m1-cpu-abc/logs?lines=5", headers=h)
    assert r.json()["lines"] == "\n".join(f"line {i}" for i in range(295, 300))

    assert (await client.get("/api/admin/pods/litellm-123/logs", headers=h)).status_code == 404
    assert (await client.get("/api/admin/pods/nope/logs", headers=h)).status_code == 404
    for bad in ("0", "2001", "-1", "abc"):
        r = await client.get(f"/api/admin/pods/llm-m1-cpu-abc/logs?lines={bad}", headers=h)
        assert r.status_code == 422, bad
    assert (await client.get("/api/admin/pods/llm-m1-cpu-abc/logs?lines=2000", headers=h)).status_code == 200

    k8s.unavailable = True
    r = await client.get("/api/admin/pods/llm-m1-cpu-abc/logs", headers=h)
    assert r.status_code == 503


async def test_nodes_reads_backend_status_from_redis_when_controllers_run_elsewhere(client, app):
    from app.controllers.backends import BACKEND_STATUS_KEY

    del app.state.backend_status  # API worker: the controllers run in their own process
    app.state.k8s.add_node("pc01", accel="cpu", internal_ip="192.168.3.41")
    backend = {
        "id": "pm-1",
        "model_name": "m1",
        "api_base": "http://192.168.3.41:18080/v1",
        "source": "k8s",
        "node": "pc01",
        "max_input_tokens": 4096,
    }
    await app.state.redis.set(BACKEND_STATUS_KEY, json.dumps({"at": "t", "backends": [backend]}))
    h = await as_admin(client, app)
    body = (await client.get("/api/admin/nodes", headers=h)).json()
    assert body["backends"] == [backend]
    assert {n["name"]: n["backends"] for n in body["nodes"]}["pc01"] == 1

    await app.state.redis.delete(BACKEND_STATUS_KEY)  # not published yet (or expired)
    body = (await client.get("/api/admin/nodes", headers=h)).json()
    assert body["backends"] == []


async def test_maintenance_is_set_through_the_api_and_listed(client, app):
    app.state.k8s.add_node("pc01", accel="cpu")
    async with app.state.sessionmaker() as db:
        dep = ModelDeployment(name="m1", repo="o/r", file="f.gguf", ctx_per_request=4096, parallel=1)
        db.add(dep)
        await db.flush()
        db.add(NodeAssignment(node_name="pc01", deployment_id=dep.id, source="k8s", manual=True))
        await db.commit()
    h = await as_admin(client, app)
    r = await client.put("/api/admin/nodes/PC01/maintenance", json={"maintenance": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"node": "pc01", "maintenance": True, "deployment_id": dep.id}
    (node,) = (await client.get("/api/admin/nodes", headers=h)).json()["nodes"]
    assert (node["maintenance"], node["deployment"]) == (True, "m1")  # the assignment is kept for the return

    r = await client.put("/api/admin/nodes/pc01/maintenance", json={"maintenance": False}, headers=h)
    assert r.json()["maintenance"] is False
    (node,) = (await client.get("/api/admin/nodes", headers=h)).json()["nodes"]
    assert node["maintenance"] is False


async def test_maintenance_needs_admin_and_csrf(client, app):
    h = csrf_headers(await login(client, app, USER))
    r = await client.put("/api/admin/nodes/pc01/maintenance", json={"maintenance": True}, headers=h)
    assert r.status_code == 403
    await login(client, app, ADMIN)
    r = await client.put("/api/admin/nodes/pc01/maintenance", json={"maintenance": True})
    assert r.status_code == 403  # no CSRF token


async def test_maintenance_of_an_unknown_node_is_404_and_main_is_refused(client, app):
    app.state.k8s.add_node("llm-main", is_main=True)
    h = await as_admin(client, app)
    r = await client.put("/api/admin/nodes/ghost/maintenance", json={"maintenance": True}, headers=h)
    assert r.status_code == 404 and r.json()["detail"] == "Нода не найдена"
    r = await client.put("/api/admin/nodes/llm-main/maintenance", json={"maintenance": True}, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "Главный узел нельзя перевести на обслуживание"
    app.state.k8s.unavailable = True
    r = await client.put("/api/admin/nodes/pc02/maintenance", json={"maintenance": True}, headers=h)
    assert r.status_code == 503 and r.json()["detail"] == "Кластер недоступен"
