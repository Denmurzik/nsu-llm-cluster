"""GET /api/admin/status: the «Состояние» page (nodes, deployments, gateway counters, backups, main GPU)."""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.controllers.backends import BACKEND_STATUS_KEY
from app.gateway_stats import bucket_key
from app.models import ModelDeployment, NodeAssignment, PortalFlag, StandaloneNode
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}


@pytest.fixture
def settings(settings, tmp_path):
    settings.backup_dir = str(tmp_path / "backups")
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
    app.state.backend_status = {"at": "2026-11-03T10:00:00+00:00", "backends": []}


async def get_status(client, app):
    h = csrf_headers(await login(client, app, ADMIN))
    r = await client.get("/api/admin/status", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_status_is_for_admins_only(client, app):
    assert (await client.get("/api/admin/status")).status_code == 401
    h = csrf_headers(await login(client, app, USER))
    r = await client.get("/api/admin/status", headers=h)
    assert r.status_code == 403 and r.json()["detail"] == "Недостаточно прав"


async def test_nodes_show_memory_accelerator_heartbeat_age_and_maintenance(client, app):
    k8s = app.state.k8s
    now = datetime.now(UTC)
    k8s.add_node("llm-main", accel="cpu", mem_mib=262144, is_main=True)
    k8s.add_node("gpu01", accel="nvidia", vram_mib=12288, mem_mib=32768)
    k8s.add_node("gpu02", accel="nvidia", vram_mib=12288, mem_mib=32768, ready=False)
    k8s.add_node("quiet", accel="cpu", mem_mib=8192)  # no lease
    k8s.heartbeats = {
        "llm-main": now - timedelta(seconds=3),
        "gpu01": now - timedelta(seconds=9),
        "gpu02": now - timedelta(seconds=400),
    }
    async with app.state.sessionmaker() as db:
        db.add(
            StandaloneNode(
                name="win-07",
                url="http://192.168.3.77:18080",
                accel="cpu",
                ram_mib=16384,
                last_seen_at=now - timedelta(seconds=20),
            )
        )
        await db.commit()

    body = await get_status(client, app)
    nodes = {n["name"]: n for n in body["nodes"]}
    assert body["k8s_available"] is True
    assert set(nodes) == {"llm-main", "gpu01", "gpu02", "quiet", "win-07"}
    gpu = nodes["gpu01"]
    assert (gpu["accel"], gpu["vram_mib"], gpu["mem_mib"], gpu["ready"], gpu["maintenance"], gpu["source"]) == (
        "nvidia",
        12288,
        32768,
        True,
        False,
        "k8s",
    )
    assert 9 <= gpu["heartbeat_age_s"] <= 15
    assert nodes["gpu02"]["maintenance"] is False and nodes["gpu02"]["ready"] is False
    assert 400 <= nodes["gpu02"]["heartbeat_age_s"] <= 406
    assert nodes["quiet"]["heartbeat_age_s"] is None
    assert nodes["llm-main"]["is_main"] is True
    win = nodes["win-07"]
    assert (win["source"], win["maintenance"]) == ("standalone", False)
    assert 20 <= win["heartbeat_age_s"] <= 26


async def test_a_lease_listing_error_keeps_the_nodes_table(client, app):
    k8s = app.state.k8s
    k8s.add_node("llm-main", accel="cpu", mem_mib=262144, is_main=True)
    k8s.add_node("gpu01", accel="nvidia", vram_mib=12288, mem_mib=32768)
    k8s.heartbeats_unavailable = True

    body = await get_status(client, app)
    nodes = {n["name"]: n for n in body["nodes"]}
    assert body["k8s_available"] is True
    assert set(nodes) == {"llm-main", "gpu01"}
    assert nodes["gpu01"]["ready"] is True and nodes["gpu01"]["heartbeat_age_s"] is None


async def test_maintenance_is_taken_from_the_nodes_api_row_when_it_has_the_field(client, app, monkeypatch):
    from app.api import admin_status

    app.state.k8s.add_node("gpu01", accel="nvidia", vram_mib=12288)
    app.state.k8s.add_node("gpu02", accel="nvidia", vram_mib=12288)
    real = admin_status.collect_nodes

    async def with_flag(*a, **k):
        available, rows, backends, heartbeats = await real(*a, **k)
        for row in rows:
            if row["name"] == "gpu02":
                row["maintenance"] = True
        return available, rows, backends, heartbeats

    monkeypatch.setattr(admin_status, "collect_nodes", with_flag)
    nodes = {n["name"]: n for n in (await get_status(client, app))["nodes"]}
    assert (nodes["gpu01"]["maintenance"], nodes["gpu02"]["maintenance"]) == (False, True)


async def test_deployments_show_state_and_backends(client, app):
    app.state.k8s.add_node("pc01", accel="cpu")
    spec = {"repo": "o/r", "file": "f.gguf", "ctx_per_request": 4096, "parallel": 1}
    async with app.state.sessionmaker() as db:
        ready = ModelDeployment(name="ready-m", **spec)
        starting = ModelDeployment(name="starting-m", **spec)
        down = ModelDeployment(name="down-m", **spec)
        off = ModelDeployment(name="off-m", enabled=False, **spec)
        split = ModelDeployment(name="big-split", mode="split", group_size=3, **spec)
        db.add_all([ready, starting, down, off, split])
        await db.flush()
        db.add_all(
            [
                NodeAssignment(node_name="pc01", deployment_id=ready.id, source="k8s"),
                NodeAssignment(node_name="pc02", deployment_id=starting.id, source="k8s"),
            ]
        )
        await db.commit()
    backend = {"id": "pm-1", "model_name": "ready-m", "node": "pc01", "source": "k8s", "api_base": "x", "max_input_tokens": 4096}
    status = {"at": datetime.now(UTC).isoformat(), "backends": [backend]}
    await app.state.redis.set(BACKEND_STATUS_KEY, json.dumps(status))

    deployments = {d["name"]: d for d in (await get_status(client, app))["deployments"]}
    assert {n: (d["status"], d["ready_backends"]) for n, d in deployments.items()} == {
        "ready-m": ("ready", 1),
        "starting-m": ("starting", 0),
        "down-m": ("down", 0),
        "off-m": ("off", None),
        "big-split": ("down", 0),
    }
    assert deployments["big-split"]["mode"] == "split" and deployments["ready-m"]["mode"] == "replicas"
    assert deployments["starting-m"]["assigned_nodes"] == 1 and deployments["down-m"]["assigned_nodes"] == 0


async def test_gateway_counters_for_five_minutes_and_an_hour(client, app):
    minute = int(time.time() // 60)
    await app.state.redis.set(bucket_key("429", minute), 4)
    await app.state.redis.set(bucket_key("5xx", minute - 2), 1)
    await app.state.redis.set(bucket_key("429", minute - 30), 10)
    await app.state.redis.set(bucket_key("5xx", minute - 59), 6)
    await app.state.redis.set(bucket_key("5xx", minute - 90), 100)  # older than an hour
    gateway = (await get_status(client, app))["gateway"]
    assert gateway == {
        "available": True,
        "last_5m": {"rate_limited": 4, "server_errors": 1},
        "last_1h": {"rate_limited": 14, "server_errors": 7},
    }


async def test_gateway_counters_unknown_when_redis_is_down(client, app):
    h = csrf_headers(await login(client, app, ADMIN))

    class Broken:
        async def mget(self, keys):
            raise OSError("redis is down")

        async def get(self, key):
            raise OSError("redis is down")

    app.state.redis = Broken()
    r = await client.get("/api/admin/status", headers=h)
    assert r.status_code == 200
    assert r.json()["gateway"] == {"available": False, "last_5m": None, "last_1h": None}


async def test_cluster_unavailable_keeps_standalone_nodes_and_the_rest(client, app):
    app.state.k8s.unavailable = True
    async with app.state.sessionmaker() as db:
        db.add(StandaloneNode(name="win-07", url="http://192.168.3.77:18080", accel="cpu", ram_mib=16384))
        await db.commit()
    body = await get_status(client, app)
    assert body["k8s_available"] is False
    assert [n["name"] for n in body["nodes"]] == ["win-07"]
    assert body["gateway"]["available"] is True and body["deployments"] == []


async def test_backups_summary(client, app, settings):
    directory = Path(settings.backup_dir)
    directory.mkdir()
    for db, size in (("portal", 100), ("litellm", 200)):
        (directory / f"{db}-20261103T031700Z.dump").write_bytes(b"x" * size)
    backups = (await get_status(client, app))["backups"]
    age = backups.pop("last_age_s")
    assert age > 0
    assert backups == {"available": True, "count": 1, "last_at": "2026-11-03T03:17:00+00:00", "last_size_bytes": 300}


async def test_main_gpu_comes_from_the_setting(client, app, settings):
    assert (await get_status(client, app))["main"] == {"gpu": None}
    settings.main_gpu = "NVIDIA GeForce RTX 4090, 24564 MiB"
    assert (await get_status(client, app))["main"] == {"gpu": "NVIDIA GeForce RTX 4090, 24564 MiB"}
