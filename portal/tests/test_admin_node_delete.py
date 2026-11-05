"""«Удалить из кластера…» (task 2-31): DELETE /api/admin/nodes/{name} and the node's own DELETE with the tokens an installed
node really holds. Real Postgres and the real routers; Redis is fakeredis, the cluster is FakeK8s."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.api import admin_versions
from app.deployments import service
from app.metrics import live
from app.models import (
    AuditLog,
    ControllerTimer,
    MetricSample,
    NodeAssignment,
    NodeModels,
    NodeWake,
    StandaloneNode,
)
from app.nodes import cleanup, registry, relay, versions
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

AUTH = {"Authorization": "Bearer agent-test"}
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}
REG = {"name": "win-07", "url": "http://192.168.3.77:18080", "accel": "cpu", "ram_mib": 16384, "version": "0.1.0"}
NIC = {"mac": "00:1a:2b:3c:4d:5e", "ip": "192.168.3.77", "prefix": 24}
CA = "K10" + "ab" * 32  # what k3s puts before `::` (the hash of the cluster CA)


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()
    app.state.backend_status = {"at": "t", "backends": []}


async def admin(client, app) -> dict:
    return csrf_headers(await login(client, app, ADMIN))


async def seed_standalone(app, name="win-07", deployment: str | None = None) -> None:
    """A registered standalone node with everything the portal keeps for it: NIC, models size, controller pauses, Redis keys."""
    async with app.state.sessionmaker() as db:
        db.add_all(
            [
                StandaloneNode(name=name, url="http://192.168.3.77:18080", accel="cpu", ram_mib=16384, version="0.1.0",
                               last_seen_at=datetime.now(UTC)),
                NodeWake(node_name=name, **{"mac": NIC["mac"], "ip": NIC["ip"], "prefix": NIC["prefix"]}),
                NodeModels(node_name=name, models_bytes=41_200_000_000),
                ControllerTimer(key=f"wake:{name}", since=1.0, members="2"),
                ControllerTimer(key=f"purge:{name}", since=1.0, members="{}"),
                ControllerTimer(key=f"cache:{name}", since=1.0, members="{}"),
                ControllerTimer(key=f"notready:{name}", since=1.0),
                ControllerTimer(key="wake:other-pc", since=1.0, members="1"),
            ]
        )
        dep_id = None
        if deployment:
            dep_id = (await service.get_by_name(db, deployment)).id
        db.add(NodeAssignment(node_name=name, deployment_id=dep_id, source="standalone", manual=True))
        db.add(MetricSample(at=datetime.now(UTC), kind="node", name=name, data={"cpu": 1}))
        await db.commit()
    redis = app.state.redis
    for key in (
        versions.KEY_REPORT + name, versions.KEY_REQUEST + name, admin_versions.KEY_SEEN + name, live.NODE_KEY + name,
        live.RATE_KEY + name, cleanup.ORDER_PREFIX + name, cleanup.RESULT_PREFIX + name, relay.KEY_PREFIX + name,
    ):
        await redis.set(key, json.dumps({"x": 1}))
    await redis.set(live.NODE_KEY + "other-pc", "{}")


async def kept(app, name="win-07") -> dict[str, bool]:
    """What the portal still holds for `name`."""
    async with app.state.sessionmaker() as db:
        timers = {t.key for t in (await db.scalars(select(ControllerTimer))).all()}
        found = {
            "standalone": await db.get(StandaloneNode, name) is not None,
            "wake": await db.get(NodeWake, name) is not None,
            "models": await db.get(NodeModels, name) is not None,
            "assignment": await db.get(NodeAssignment, name) is not None,
            "timers": any(k.endswith(f":{name}") for k in timers),
            "other_timer": "wake:other-pc" in timers,
        }
    redis = app.state.redis
    found["redis"] = any(
        [
            await redis.exists(p + name)
            for p in (
                versions.KEY_REPORT, versions.KEY_REQUEST, admin_versions.KEY_SEEN, live.NODE_KEY, live.RATE_KEY,
                cleanup.ORDER_PREFIX, cleanup.RESULT_PREFIX, relay.KEY_PREFIX,
            )
        ]
    )
    found["other_redis"] = bool(await redis.exists(live.NODE_KEY + "other-pc"))
    return found


GONE = {"standalone": False, "wake": False, "models": False, "assignment": False, "timers": False, "redis": False}
OTHERS = {"other_timer": True, "other_redis": True}


def only(found: dict[str, bool], *keys: str) -> dict[str, bool]:
    return {k: found[k] for k in keys}


# ---- the node's own DELETE: the tokens an installed node really holds ------------------------------------------------


def test_the_registry_row_names_match_the_versions_module():
    assert registry.KEY_UPDATE_SEEN == admin_versions.KEY_SEEN


@pytest.mark.parametrize(
    "bearer",
    [
        "agent-test",  # the raw agent token
        f"{CA}::node:agent-test",  # what «Подключение ноды» shows
        "tok-test",  # a node installed before the agent token: the raw k3s server token
        f"{CA}::server:tok-test",  # the same in the secure form k3s writes for the server
    ],
)
async def test_node_delete_takes_every_token_form_an_installer_holds(client, app, settings, bearer):
    """What `-Uninstall` can hold: the panel's token (secure form or raw) or, from before the agent token, the server token."""
    settings.node_legacy_token = True  # an install upgraded from before the agent token
    app.state.k8s.add_node("win-07", accel="cpu")
    await seed_standalone(app)
    r = await client.delete("/api/nodes/win-07", headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == 204, r.text
    assert app.state.k8s.deleted_nodes == ["win-07"]
    assert only(await kept(app), *GONE) == GONE


@pytest.mark.parametrize(
    "bearer",
    [
        "other",
        f"{CA}::node:other",  # a token of an earlier install of main: what a PC keeps after main was reinstalled (401 on -Uninstall)
        f"{CA}::node:tok-test",  # the server secret is no agent token: the legacy form of it is `::server:`
        f"{CA}::admin:tok-test",  # a user k3s has no token for
        f"{CA}::node:agent",  # a prefix of the secret
        "",
    ],
)
async def test_node_delete_still_refuses_other_tokens(client, app, bearer):
    app.state.k8s.add_node("win-07", accel="cpu")
    await seed_standalone(app)
    r = await client.delete("/api/nodes/win-07", headers={"Authorization": f"Bearer {bearer}"})
    assert r.status_code == 401
    assert app.state.k8s.deleted_nodes == []
    assert (await kept(app))["standalone"] is True


async def test_server_secret_is_refused_once_legacy_tokens_are_off(client, app, settings):
    settings.node_legacy_token = False
    app.state.k8s.add_node("win-07", accel="cpu")
    for bearer in (f"{CA}::server:tok-test", "tok-test"):
        r = await client.delete("/api/nodes/win-07", headers={"Authorization": f"Bearer {bearer}"})
        assert r.status_code == 401
    r = await client.delete("/api/nodes/win-07", headers={"Authorization": f"Bearer {CA}::node:agent-test"})
    assert r.status_code == 204


async def test_node_delete_removes_the_assignment_too(client, app):
    """One code path with the admin's button: the deployment the node served is released with it."""
    await seed_standalone(app, deployment="gemma-4-e4b")
    assert (await client.delete("/api/nodes/WIN-07", headers=AUTH)).status_code == 204
    found = await kept(app)
    assert only(found, *GONE) == GONE and only(found, *OTHERS) == OTHERS


async def test_the_nodes_own_delete_leaves_an_audit_row_without_an_actor(client, app):
    app.state.k8s.add_node("win-07", accel="cpu")
    await seed_standalone(app)
    assert (await client.delete("/api/nodes/win-07", headers=AUTH)).status_code == 204
    assert (await client.delete("/api/nodes/ghost", headers=AUTH)).status_code == 204  # a name nobody knew: no row
    async with app.state.sessionmaker() as db:
        (row,) = (await db.scalars(select(AuditLog).where(AuditLog.action == "node.delete"))).all()
    assert (row.actor_id, row.actor_email, row.target_type, row.target) == (None, None, "node", "win-07")
    assert row.details == {"by": "node", "k8s": "deleted"}


async def test_node_delete_works_without_the_cluster(client, app):
    app.state.k8s.unavailable = True
    await seed_standalone(app)
    assert (await client.delete("/api/nodes/win-07", headers=AUTH)).status_code == 204
    assert (await kept(app))["standalone"] is False


# ---- the admin's DELETE ----------------------------------------------------------------------------------------------


async def test_it_removes_everything_and_audits_it(client, app):
    app.state.k8s.add_node("win-07", accel="cpu", internal_ip="192.168.3.77")
    await seed_standalone(app)
    h = await admin(client, app)
    r = await client.delete("/api/admin/nodes/win-07", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["node"], body["online"], body["k8s"], body["unassigned"]) == ("win-07", True, "deleted", [])
    assert body["uninstall"] == {
        "windows": r".\install-node.ps1 -Uninstall -Name win-07",
        "linux": "sudo ./run-node.sh --uninstall --main 192.168.3.21 --name win-07",
    }
    assert "tok" not in json.dumps(body) and "agent" not in json.dumps(body)  # no secret in the answer
    assert app.state.k8s.deleted_nodes == ["win-07"]
    found = await kept(app)
    assert only(found, *GONE) == GONE and only(found, *OTHERS) == OTHERS
    async with app.state.sessionmaker() as db:
        (row,) = (await db.scalars(select(AuditLog).where(AuditLog.action == "node.delete"))).all()
        samples = (await db.scalars(select(MetricSample))).all()
    assert (row.actor_email, row.target_type, row.target) == ("admin@g.nsu.ru", "node", "win-07")
    assert row.details == {"source": "k8s", "forced": False, "online": True, "deployments": [], "k8s": "deleted"}
    assert [(m.kind, m.name) for m in samples] == [("node", "win-07")]  # the history rows stay


async def test_a_standalone_node_is_removed_and_a_silent_one_is_offline(client, app):
    await seed_standalone(app)
    async with app.state.sessionmaker() as db:
        await db.execute(update(StandaloneNode).values(last_seen_at=datetime.now(UTC) - timedelta(minutes=10)))
        await db.commit()
    h = await admin(client, app)
    r = await client.delete("/api/admin/nodes/win-07", headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["online"], r.json()["k8s"]) == (False, "absent")
    assert only(await kept(app), *GONE) == GONE


async def test_a_stopped_standalone_node_is_not_online(client, app):
    await seed_standalone(app)
    async with app.state.sessionmaker() as db:
        await db.execute(update(StandaloneNode).values(stopped_at=datetime.now(UTC)))
        await db.commit()
    r = await client.delete("/api/admin/nodes/win-07", headers=await admin(client, app))
    assert r.json()["online"] is False


async def test_a_node_that_is_down_in_the_cluster_is_offline(client, app):
    app.state.k8s.add_node("pc01", accel="cpu", ready=False)
    r = await client.delete("/api/admin/nodes/pc01", headers=await admin(client, app))
    assert r.status_code == 200, r.text
    assert (r.json()["online"], r.json()["k8s"]) == (False, "deleted")


async def test_the_main_node_is_refused(client, app):
    app.state.k8s.add_node("llm-main", accel="cpu", is_main=True)
    h = await admin(client, app)
    for name in ("llm-main", "LLM-Main"):
        for force in ("", "?force=1"):
            r = await client.delete(f"/api/admin/nodes/{name}{force}", headers=h)
            assert r.status_code == 409 and r.json()["detail"] == "Главный сервер нельзя удалить"
    assert app.state.k8s.deleted_nodes == []
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(AuditLog))).all() == []


async def test_an_unknown_node_is_404_and_without_the_cluster_503(client, app):
    h = await admin(client, app)
    r = await client.delete("/api/admin/nodes/ghost", headers=h)
    assert r.status_code == 404 and r.json()["detail"] == "Нода не найдена"
    app.state.k8s.unavailable = True
    r = await client.delete("/api/admin/nodes/ghost", headers=h)
    assert r.status_code == 503


async def test_a_ghost_with_only_leftovers_can_be_cleaned_up(client, app):
    """An assignment or a NIC left by a node that is gone from the cluster and the registry: the button removes them."""
    async with app.state.sessionmaker() as db:
        db.add(NodeWake(node_name="ghost", **{"mac": NIC["mac"], "ip": NIC["ip"], "prefix": NIC["prefix"]}))
        await db.commit()
    r = await client.delete("/api/admin/nodes/ghost", headers=await admin(client, app))
    assert r.status_code == 200, r.text
    async with app.state.sessionmaker() as db:
        assert await db.get(NodeWake, "ghost") is None


async def test_the_cluster_may_be_down_for_a_standalone_node(client, app):
    app.state.k8s.unavailable = True
    await seed_standalone(app)
    r = await client.delete("/api/admin/nodes/win-07", headers=await admin(client, app))
    assert r.status_code == 200, r.text
    assert r.json()["k8s"] == "unavailable"
    assert only(await kept(app), *GONE) == GONE


async def test_it_needs_an_admin_and_the_csrf_header(client, app):
    app.state.k8s.add_node("pc01", accel="cpu")
    assert (await client.delete("/api/admin/nodes/pc01")).status_code == 401
    await login(client, app, ADMIN)
    assert (await client.delete("/api/admin/nodes/pc01")).status_code == 403  # no X-CSRF-Token
    assert app.state.k8s.deleted_nodes == []
    client.cookies.clear()
    h = csrf_headers(await login(client, app, USER))
    assert (await client.delete("/api/admin/nodes/pc01", headers=h)).status_code == 403
    assert app.state.k8s.deleted_nodes == []


# ---- a busy node ----------------------------------------------------------------------------------------------------


async def test_a_node_with_a_deployment_is_refused_and_force_unassigns_it(client, app):
    await seed_standalone(app, deployment="gemma-4-e4b")
    h = await admin(client, app)
    r = await client.delete("/api/admin/nodes/win-07", headers=h)
    assert r.status_code == 409, r.text
    body = r.json()
    assert "gemma-4-e4b" in body["detail"] and "split" not in body["detail"]
    assert body["deployments"] == [{"name": "gemma-4-e4b", "mode": "replicas"}]
    assert (await kept(app))["standalone"] is True and app.state.k8s.deleted_nodes == []
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(AuditLog))).all() == []  # a refusal writes nothing

    r = await client.delete("/api/admin/nodes/win-07?force=1", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["unassigned"] == ["gemma-4-e4b"]
    found = await kept(app)
    assert only(found, *GONE) == GONE and only(found, *OTHERS) == OTHERS
    async with app.state.sessionmaker() as db:
        (row,) = (await db.scalars(select(AuditLog).where(AuditLog.action == "node.delete"))).all()
        dep = await service.get_by_name(db, "gemma-4-e4b")
        assert dep is not None  # the deployment itself stays
    assert row.details["forced"] is True and row.details["deployments"] == ["gemma-4-e4b"]


async def test_a_member_of_a_split_group_is_named_with_its_mode(client, app):
    async with app.state.sessionmaker() as db:
        dep = await service.get_by_name(db, "gemma-4-e4b")
        dep.mode = "split"
        await db.commit()
    app.state.k8s.add_node("pc01", accel="cpu")
    app.state.k8s.add_pod("gemma-4-e4b-0", "pc01", "leader", "gemma-4-e4b")  # a pod, no assignment row at all
    h = await admin(client, app)
    r = await client.delete("/api/admin/nodes/pc01", headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["deployments"] == [{"name": "gemma-4-e4b", "mode": "split"}]
    assert "gemma-4-e4b (split)" in r.json()["detail"] and "группа split остановится" in r.json()["detail"]
    assert app.state.k8s.deleted_nodes == []
    assert (await client.delete("/api/admin/nodes/pc01?force=1", headers=h)).status_code == 200
    assert app.state.k8s.deleted_nodes == ["pc01"]


async def test_a_pod_of_another_node_does_not_make_this_one_busy(client, app):
    app.state.k8s.add_node("pc01", accel="cpu")
    app.state.k8s.add_node("pc02", accel="cpu")
    app.state.k8s.add_pod("gemma-4-e4b-0", "pc02", "replica", "gemma-4-e4b")
    r = await client.delete("/api/admin/nodes/pc01", headers=await admin(client, app))
    assert r.status_code == 200, r.text


async def test_a_node_on_maintenance_without_a_deployment_is_not_busy(client, app):
    app.state.k8s.add_node("pc01", accel="cpu")
    async with app.state.sessionmaker() as db:
        db.add(NodeAssignment(node_name="pc01", deployment_id=None, source="k8s", manual=False, maintenance=True))
        await db.commit()
    r = await client.delete("/api/admin/nodes/pc01", headers=await admin(client, app))
    assert r.status_code == 200, r.text
    async with app.state.sessionmaker() as db:
        assert await db.get(NodeAssignment, "pc01") is None


async def test_a_failing_audit_row_leaves_the_node_in_the_database(client, app, monkeypatch):
    from app import audit

    await seed_standalone(app)

    async def boom(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit, "record", boom)
    h = await admin(client, app)
    with pytest.raises(RuntimeError):
        await client.delete("/api/admin/nodes/win-07", headers=h)
    found = await kept(app)
    assert found["standalone"] and found["wake"] and found["models"] and found["assignment"]


async def test_a_node_that_registers_again_comes_back_clean(client, app):
    """The agent of an online PC re-registers with its next heartbeat: a fresh record, no assignment, no old size."""
    await seed_standalone(app, deployment="gemma-4-e4b")
    h = await admin(client, app)
    assert (await client.delete("/api/admin/nodes/win-07?force=1", headers=h)).status_code == 200
    r = await client.post("/api/nodes/heartbeat", json={"name": "win-07"}, headers=AUTH)
    assert r.status_code == 404  # unknown: the registrar registers again
    assert (await client.post("/api/nodes/register", json=REG, headers=AUTH)).status_code == 200
    async with app.state.sessionmaker() as db:
        row = await db.get(NodeAssignment, "win-07")
        assert row is not None and row.deployment_id is None and row.manual is False
        assert await db.get(NodeModels, "win-07") is None
