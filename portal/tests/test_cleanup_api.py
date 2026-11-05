"""Cleanup of model weights (task 2-26): what nodes report, the orders in the heartbeat reply, the «Очистить модели» button."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.catalog.gguf import GgufMeta
from app.catalog.hf import HfFile
from app.deployments import service
from app.deployments.service import DeploymentIn
from app.models import (
    AuditLog, ControllerTimer, ModelDeployment, NodeAssignment, NodeModels, NodeWake, PortalFlag, StandaloneNode,
)
from app.nodes import cleanup
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

AUTH = {"Authorization": "Bearer agent-test"}
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}
REG = {"name": "win-07", "url": "http://192.168.3.77:18080", "accel": "cpu", "ram_mib": 16384, "version": "0.1.0"}
GiB = 2**30
REPO, FILE = "org/model-GGUF", "model-Q4_K_M.gguf"
RESULT = 'removed org__old 7000\nRESULT {"freed_bytes": 7000, "models_bytes": 1234, "removed": ["org__old"], "skipped": [], "dry_run": false}'


@pytest_asyncio.fixture(autouse=True)
async def env(app, monkeypatch):
    async with app.state.sessionmaker() as db:
        for table in (NodeModels, NodeWake, ControllerTimer, NodeAssignment, StandaloneNode, ModelDeployment, PortalFlag):
            await db.execute(delete(table))
        await db.commit()
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()
    app.state.backend_status = {"at": "t", "backends": []}
    app.state.hf = FakeHf(files={REPO: [HfFile(FILE, 4 * GiB)]}, metas={(REPO, FILE): GgufMeta("llama", 32, 32, 8, 128, 4096, 32768)})
    monkeypatch.setattr(cleanup, "POLL_S", 0.01)


async def rows(app) -> dict[str, int]:
    async with app.state.sessionmaker() as db:
        return {r.node_name: r.models_bytes for r in (await db.scalars(select(NodeModels))).all()}


async def admin(client, app):
    return csrf_headers(await login(client, app, ADMIN))


# ---- the node reports its volume -----------------------------------------------------------------------------------------


async def test_k3s_node_reports_the_size_of_its_models_volume(client, app):
    assert (await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 5})).status_code == 401
    r = await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 41 * GiB}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert await rows(app) == {"pc01": 41 * GiB}
    r = await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 7}, headers=AUTH)
    assert await rows(app) == {"pc01": 7}


@pytest.mark.parametrize("bad", [{"models_bytes": -1}, {"models_bytes": "lots"}, {"name": "Bad_Name"}, {"models_bytes": None}])
async def test_a_bad_size_is_refused(client, app, bad):
    r = await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 1, **bad}, headers=AUTH)
    assert r.status_code == 422
    assert await rows(app) == {}


async def test_standalone_node_reports_the_size_with_register_and_heartbeat(client, app):
    r = await client.post("/api/nodes/register", json={**REG, "models_bytes": 100}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert await rows(app) == {"win-07": 100}
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07", "models_bytes": 250}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert await rows(app) == {"win-07": 250}
    # an older node image sends none: what is stored stays
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07"}, headers=AUTH)
    assert r.status_code == 200
    assert await rows(app) == {"win-07": 250}


async def test_uninstalled_node_forgets_its_size(client, app):
    await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 9}, headers=AUTH)
    await client.post("/api/nodes/register", json={**REG, "models_bytes": 9}, headers=AUTH)
    assert (await client.delete("/api/nodes/win-07", headers=AUTH)).status_code == 204
    assert await rows(app) == {"pc01": 9}


async def test_nodes_page_shows_the_size(client, app):
    k8s = app.state.k8s
    k8s.add_node("llm-main", accel="cpu", is_main=True)
    k8s.add_node("pc01", accel="cpu")
    k8s.add_node("pc02", accel="cpu")
    await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 3 * GiB}, headers=AUTH)
    await client.post("/api/nodes/register", json={**REG, "models_bytes": 5 * GiB}, headers=AUTH)
    h = await admin(client, app)
    nodes = {n["name"]: n for n in (await client.get("/api/admin/nodes", headers=h)).json()["nodes"]}
    assert (nodes["pc01"]["models_bytes"], nodes["pc02"]["models_bytes"], nodes["win-07"]["models_bytes"]) == (3 * GiB, None, 5 * GiB)


# ---- orders in the heartbeat reply ---------------------------------------------------------------------------------------


async def test_heartbeat_hands_a_standalone_node_its_cleanup_orders_once(client, app):
    await client.post("/api/nodes/register", json=REG, headers=AUTH)
    keep = cleanup.Keep((f"{REPO}:{FILE}",), rpc=False, vllm=False)
    ref, error = await cleanup.send(app.state, "win-07", "standalone", "cpu", keep)
    assert error is None
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07"}, headers=AUTH)
    assert r.json()["purge"] == [{"id": ref, "keep": [f"{REPO}:{FILE}"], "keep_rpc": False, "keep_vllm": False}]
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07"}, headers=AUTH)
    assert "purge" not in r.json()  # taken: an older registrar never sees the key


async def test_heartbeat_takes_the_result_of_an_order(client, app):
    await client.post("/api/nodes/register", json={**REG, "models_bytes": 9000}, headers=AUTH)
    ref, _ = await cleanup.send(app.state, "win-07", "standalone", "cpu", cleanup.Keep())
    result = {"id": ref, "freed_bytes": 7000, "models_bytes": 2000, "removed": ["org__old"], "skipped": []}
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07", "models_bytes": 9000, "purge_result": result}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert await rows(app) == {"win-07": 2000}  # what the cleanup left beats the size measured before it
    outcome = await cleanup.poll(app.state, "win-07", "standalone", ref)
    assert (outcome.state, outcome.freed_bytes, outcome.removed) == ("done", 7000, ["org__old"])
    other = await cleanup.poll(app.state, "win-07", "standalone", "someone-elses-order")
    assert other.state == "running"


async def test_heartbeat_with_junk_instead_of_a_result_still_works(client, app):
    await client.post("/api/nodes/register", json=REG, headers=AUTH)
    for junk in ({}, {"id": ""}, {"id": 5}, {"id": "x" * 100}, {"freed_bytes": 1}):
        r = await client.post("/api/nodes/heartbeat", json={"name": "win-07", "purge_result": junk}, headers=AUTH)
        assert r.status_code == 200, (junk, r.text)


async def test_a_failed_order_is_reported_as_failed(client, app):
    await client.post("/api/nodes/register", json=REG, headers=AUTH)
    ref, _ = await cleanup.send(app.state, "win-07", "standalone", "cpu", cleanup.Keep())
    r = await client.post(
        "/api/nodes/heartbeat", json={"name": "win-07", "purge_result": {"id": ref, "error": "models-cleanup: bad"}}, headers=AUTH
    )
    assert r.status_code == 200
    outcome = await cleanup.poll(app.state, "win-07", "standalone", ref)
    assert (outcome.state, outcome.error) == ("failed", "models-cleanup: bad")


# ---- the button ------------------------------------------------------------------------------------------------------


async def finish_when_created(k8s, ok=True, log=RESULT):
    """The Job the button created ends (the node ran it)."""
    for _ in range(200):
        if k8s.job_names:
            k8s.finish_job(k8s.job_names[-1], ok=ok, log=log)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no Job was created")


async def test_button_cleans_a_k3s_node_down_to_what_its_deployment_needs(client, app):
    k8s = app.state.k8s
    k8s.add_node("llm-main", accel="cpu", is_main=True)
    k8s.add_node("pc01", accel="cpu")
    async with app.state.sessionmaker() as db:
        await service.create(db, app.state.hf, DeploymentIn(name="m1", repo=REPO, file=FILE, nodes=["pc01"]))
    h = await admin(client, app)
    finisher = asyncio.create_task(finish_when_created(k8s))
    r = await client.post("/api/admin/nodes/PC01/purge-models", headers=h)
    await finisher
    assert r.status_code == 200, r.text
    assert r.json() == {
        "node": "pc01", "status": "done", "freed_bytes": 7000, "models_bytes": 1234, "removed": ["org__old"], "skipped": [],
    }
    (job,) = k8s.jobs
    pod = job["spec"]["template"]["spec"]
    assert pod["nodeName"] == "pc01"
    assert pod["containers"][0]["command"] == ["/opt/llm/bin/models-cleanup.sh", "--keep", f"{REPO}:{FILE}"]
    assert await rows(app) == {"pc01": 1234}


async def audit_rows(app) -> list[AuditLog]:
    async with app.state.sessionmaker() as db:
        return list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())


async def test_button_leaves_an_audit_row_with_what_stayed_and_what_was_freed(client, app):
    k8s = app.state.k8s
    k8s.add_node("pc01", accel="cpu")
    async with app.state.sessionmaker() as db:
        await service.create(db, app.state.hf, DeploymentIn(name="m1", repo=REPO, file=FILE, nodes=["pc01"]))
    h = await admin(client, app)
    finisher = asyncio.create_task(finish_when_created(k8s))
    r = await client.post("/api/admin/nodes/pc01/purge-models", headers=h)
    await finisher
    assert r.status_code == 200, r.text
    (row,) = [a for a in await audit_rows(app) if a.action == "node.purge_models"]
    assert (row.actor_email, row.target_type, row.target) == ("admin@g.nsu.ru", "node", "pc01")
    assert row.details == {"status": "done", "freed_bytes": 7000, "kept": [f"{REPO}:{FILE}"], "kept_rpc": False, "skipped": []}
    assert await rows(app) == {"pc01": 1234}  # the size and the audit row went in one commit


async def test_a_failed_cleanup_is_logged_too_and_a_refused_one_is_not(client, app):
    k8s = app.state.k8s
    k8s.add_node("pc01", accel="cpu")
    k8s.add_node("down", accel="cpu", ready=False)
    h = await admin(client, app)
    assert (await client.post("/api/admin/nodes/down/purge-models", headers=h)).status_code == 409
    assert [a for a in await audit_rows(app) if a.action == "node.purge_models"] == []
    finisher = asyncio.create_task(finish_when_created(k8s, ok=False, log="models-cleanup: boom"))
    r = await client.post("/api/admin/nodes/pc01/purge-models", headers=h)
    await finisher
    assert r.status_code == 502
    (row,) = [a for a in await audit_rows(app) if a.action == "node.purge_models"]
    assert row.details["status"] == "failed" and "boom" in row.details["error"]


async def test_button_on_a_node_without_a_deployment_removes_everything(client, app):
    k8s = app.state.k8s
    k8s.add_node("pc01", accel="nvidia")
    h = await admin(client, app)
    finisher = asyncio.create_task(finish_when_created(k8s))
    r = await client.post("/api/admin/nodes/pc01/purge-models", headers=h)
    await finisher
    assert r.status_code == 200, r.text
    pod = k8s.jobs[0]["spec"]["template"]["spec"]
    assert pod["containers"][0]["command"] == ["/opt/llm/bin/models-cleanup.sh"]
    assert pod["containers"][0]["image"].endswith("-gpu")


async def test_button_keeps_the_tensor_cache_of_a_split_node(client, app):
    k8s = app.state.k8s
    k8s.add_node("pc01", accel="cpu")
    k8s.add_node("pc02", accel="cpu")
    async with app.state.sessionmaker() as db:
        await service.create(
            db, app.state.hf, DeploymentIn(name="sp", repo=REPO, file=FILE, mode="split", group_size=2, accel="cpu", nodes=["pc01", "pc02"])
        )
    h = await admin(client, app)
    finisher = asyncio.create_task(finish_when_created(k8s))
    r = await client.post("/api/admin/nodes/pc02/purge-models", headers=h)
    await finisher
    assert r.status_code == 200, r.text
    assert k8s.jobs[0]["spec"]["template"]["spec"]["containers"][0]["command"][1:] == ["--keep", f"{REPO}:{FILE}", "--keep-rpc"]


async def test_button_says_pending_when_the_node_has_not_answered_in_time(client, app, monkeypatch):
    monkeypatch.setattr(cleanup, "WAIT_K8S_S", 0.05)
    app.state.k8s.add_node("pc01", accel="cpu")
    h = await admin(client, app)
    r = await client.post("/api/admin/nodes/pc01/purge-models", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending" and r.json()["freed_bytes"] is None
    assert len(app.state.k8s.jobs) == 1


async def test_button_reports_a_job_that_failed(client, app):
    k8s = app.state.k8s
    k8s.add_node("pc01", accel="cpu")
    h = await admin(client, app)
    finisher = asyncio.create_task(finish_when_created(k8s, ok=False, log="models-cleanup: LLAMA_CACHE must be absolute"))
    r = await client.post("/api/admin/nodes/pc01/purge-models", headers=h)
    await finisher
    assert r.status_code == 502
    assert "LLAMA_CACHE must be absolute" in r.json()["detail"]


async def test_button_when_the_cluster_refuses_the_job(client, app, monkeypatch):
    k8s = app.state.k8s
    k8s.add_node("pc01", accel="cpu")

    async def refuse(manifest):
        raise cleanup.K8sUnavailable("fake: down")

    monkeypatch.setattr(k8s, "create_job", refuse)
    h = await admin(client, app)
    r = await client.post("/api/admin/nodes/pc01/purge-models", headers=h)
    assert r.status_code == 502
    assert r.json()["detail"].startswith("Не удалось передать команду")


async def test_button_on_a_standalone_node_waits_for_two_heartbeats(client, app):
    await client.post("/api/nodes/register", json=REG, headers=AUTH)
    h = await admin(client, app)

    async def the_node():
        for _ in range(200):
            r = await client.post("/api/nodes/heartbeat", json={"name": "win-07"}, headers=AUTH)
            orders = r.json().get("purge")
            if orders:
                result = {"id": orders[0]["id"], "freed_bytes": 55, "models_bytes": 1, "removed": ["org__old"], "skipped": []}
                await client.post("/api/nodes/heartbeat", json={"name": "win-07", "purge_result": result}, headers=AUTH)
                return orders[0]
            await asyncio.sleep(0.01)
        raise AssertionError("no order")

    node = asyncio.create_task(the_node())
    r = await client.post("/api/admin/nodes/win-07/purge-models", headers=h)
    order = await node
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["freed_bytes"], r.json()["removed"]) == ("done", 55, ["org__old"])
    assert (order["keep"], order["keep_rpc"]) == ([], False)
    assert await rows(app) == {"win-07": 1}


async def test_button_refuses_what_cannot_be_cleaned(client, app):
    k8s = app.state.k8s
    k8s.add_node("llm-main", accel="cpu", is_main=True)
    k8s.add_node("down", accel="cpu", ready=False)
    async with app.state.sessionmaker() as db:
        db.add(StandaloneNode(name="gone", url="http://10.0.0.5:18080", accel="cpu", version="v",
                              last_seen_at=datetime.now(UTC) - timedelta(minutes=10)))
        await db.commit()
    h = await admin(client, app)
    assert (await client.post("/api/admin/nodes/llm-main/purge-models", headers=h)).status_code == 409
    assert (await client.post("/api/admin/nodes/ghost/purge-models", headers=h)).status_code == 404
    for name in ("down", "gone"):
        r = await client.post(f"/api/admin/nodes/{name}/purge-models", headers=h)
        assert r.status_code == 409, r.text
        assert "не в сети" in r.json()["detail"]
    assert k8s.jobs == []
    k8s.unavailable = True
    assert (await client.post("/api/admin/nodes/ghost/purge-models", headers=h)).status_code == 503


async def test_button_is_for_admins_with_csrf(client, app):
    app.state.k8s.add_node("pc01", accel="cpu")
    h = csrf_headers(await login(client, app, USER))
    assert (await client.post("/api/admin/nodes/pc01/purge-models", headers=h)).status_code == 403
    assert app.state.k8s.jobs == []
    h = await admin(client, app)
    assert (await client.post("/api/admin/nodes/pc01/purge-models")).status_code == 403  # no CSRF header
    assert app.state.k8s.jobs == []


async def test_orders_survive_redis_json_round_trip(app):
    keep = cleanup.Keep(("a/b:c.gguf", "local/x:y.gguf"), rpc=True, vllm=True)
    ref, _ = await cleanup.send(app.state, "n1", "standalone", "cpu", keep)
    (order,) = await cleanup.pop_orders(app.state.redis, "n1")
    assert order == {"id": ref, "keep": ["a/b:c.gguf", "local/x:y.gguf"], "keep_rpc": True, "keep_vllm": True}
    assert await cleanup.pop_orders(app.state.redis, "n1") == []
