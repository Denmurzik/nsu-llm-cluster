"""Deep-review fixes in the portal API: connections not held across LiteLLM calls, ready-backend counts from
Redis, the sliding cookie on every kind of response, login redirects, clean validation messages."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from app.api.playground import MAX_CONTENT_CHARS, MAX_MESSAGES
from app.auth.sessions import create_session
from app.controllers.backends import BACKEND_STATUS_KEY
from app.models import ModelDeployment, NodeAssignment, PortalFlag, User, WebSession
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, install_fake_oidc, login

pytestmark = pytest.mark.docker

USER = {"sub": "usr", "email": "ivan@g.nsu.ru"}
CHAT = {"model": "m1", "messages": [{"role": "user", "content": "Привет"}]}
NODE = {"name": "lab-1", "url": "http://10.0.0.5:18080", "accel": "cpu"}


@pytest_asyncio.fixture
async def fake(app):
    app.state.litellm = FakeLiteLLM()
    return app.state.litellm


def watch_pool(app, fake, name: str) -> list[int]:
    """Records how many pooled connections are checked out whenever the fake LiteLLM method is called."""
    seen: list[int] = []
    original = getattr(fake, name)

    async def wrapper(*args, **kwargs):
        seen.append(app.state.engine.pool.checkedout())
        return await original(*args, **kwargs)

    setattr(fake, name, wrapper)
    return seen


async def add_deployment(app, name: str = "m1") -> None:
    async with app.state.sessionmaker() as db:
        await db.execute(delete(NodeAssignment))
        await db.execute(delete(ModelDeployment))
        await db.execute(delete(PortalFlag))
        db.add(ModelDeployment(name=name, repo="o/r", file="f.gguf", ctx_per_request=8192, parallel=2))
        await db.commit()


# ---------- no pooled connection while LiteLLM is called ----------


async def test_catalog_does_not_hold_a_connection_while_asking_the_gateway(client, app, fake):
    await add_deployment(app)
    await fake.add_model("pm-1", "m1", "http://k1:18080/v1", "k8s", "k1", 8192)
    seen = watch_pool(app, fake, "model_group_status")
    assert (await client.get("/api/public/status")).status_code == 200
    await login(client, app, USER)
    assert (await client.get("/api/models")).status_code == 200
    assert seen == [0, 0]


async def test_authenticated_endpoints_do_not_hold_a_connection_during_litellm_calls(client, app, fake):
    me = await login(client, app, USER)
    created = await client.post("/api/keys", json={"alias": "k"}, headers=csrf_headers(me))
    assert created.status_code == 201, created.text
    usage = watch_pool(app, fake, "user_activity")
    deleting = watch_pool(app, fake, "delete_keys")
    assert (await client.get("/api/usage", params={"days": 7})).status_code == 200
    assert (await client.get("/api/keys")).status_code == 200
    assert (await client.delete(f"/api/keys/{created.json()['id']}", headers=csrf_headers(me))).status_code == 204
    assert usage == [0, 0]
    assert deleting == [0]


# ---------- ready-backend counts: Redis first, short cache ----------


def status_in_redis(app, backends: list[dict], age_s: float = 0):
    at = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    return app.state.redis.set(BACKEND_STATUS_KEY, json.dumps({"at": at, "backends": backends}))


async def test_counts_come_from_the_controllers_status_in_redis(client, app, fake):
    await add_deployment(app)
    await status_in_redis(app, [{"model_name": "m1", "node": "a"}, {"model_name": "m1", "node": "b"}])
    fake.unavailable = True  # the gateway is not consulted while the status is fresh
    r = await client.get("/api/public/status")
    assert r.json() == {"login_provider": "SSO", "models": [{"name": "m1", "status": "ready", "ready_backends": 2}]}


async def test_a_stale_status_in_redis_falls_back_to_the_gateway(client, app, fake):
    await add_deployment(app)
    await status_in_redis(app, [{"model_name": "m1", "node": "a"}], age_s=3600)
    fake.unavailable = True
    r = await client.get("/api/public/status")
    assert r.json() == {"login_provider": "SSO", "models": [{"name": "m1", "status": "unknown", "ready_backends": None}]}


async def test_without_a_status_in_redis_the_gateway_is_asked(client, app, fake):
    await add_deployment(app)
    await fake.add_model("pm-1", "m1", "http://k1:18080/v1", "k8s", "k1", 8192)
    r = await client.get("/api/public/status")
    assert r.json() == {"login_provider": "SSO", "models": [{"name": "m1", "status": "ready", "ready_backends": 1}]}


async def test_counts_are_cached_for_the_ttl(client, app, fake):
    await add_deployment(app)
    await fake.add_model("pm-1", "m1", "http://k1:18080/v1", "k8s", "k1", 8192)
    app.state.backend_counts.ttl_s = 60
    calls = []
    original = fake.model_group_status

    async def counting():
        calls.append(1)
        return await original()

    fake.model_group_status = counting
    for _ in range(3):
        assert (await client.get("/api/public/status")).status_code == 200
    assert len(calls) == 1
    app.state.backend_counts.ttl_s = 0
    await client.get("/api/public/status")
    assert len(calls) == 2


# ---------- sessions ----------


async def test_create_session_removes_expired_sessions(db_session):
    user = User(sub="s1", email="a@b.c")
    db_session.add(user)
    await db_session.commit()
    old, _ = await create_session(db_session, user, "t", 12)
    await db_session.execute(update(WebSession).values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    await db_session.commit()
    fresh, _ = await create_session(db_session, user, "t", 12)
    rows = (await db_session.scalars(select(WebSession))).all()
    assert len(rows) == 1 and old != fresh


async def slide(db_session) -> None:
    await db_session.execute(update(WebSession).values(expires_at=datetime.now(UTC) + timedelta(hours=2)))
    await db_session.commit()


async def test_sliding_cookie_survives_a_204_response(client, app, fake, db_session):
    me = await login(client, app, USER)
    key = (await client.post("/api/keys", json={"alias": "k"}, headers=csrf_headers(me))).json()
    await slide(db_session)
    r = await client.delete(f"/api/keys/{key['id']}", headers=csrf_headers(me))
    assert r.status_code == 204
    assert "Max-Age=43200" in r.headers["set-cookie"]


async def test_sliding_cookie_survives_an_http_exception(client, app, fake, db_session):
    me = await login(client, app, USER)
    await slide(db_session)
    r = await client.delete("/api/keys/00000000-0000-0000-0000-000000000000", headers=csrf_headers(me))
    assert r.status_code == 404
    assert "Max-Age=43200" in r.headers["set-cookie"]


async def test_sliding_cookie_survives_a_streaming_response(client, app, fake, db_session):
    me = await login(client, app, USER)
    await slide(db_session)
    r = await client.post("/api/playground/chat", json=CHAT, headers=csrf_headers(me))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "Max-Age=43200" in r.headers["set-cookie"]


# ---------- login ----------


async def test_overlong_return_to_becomes_the_root_not_a_500(client, app, db_session):
    install_fake_oidc(app)
    r = await client.get("/auth/login", params={"return_to": "/" + "a" * 3000})
    assert r.status_code == 302
    (key,) = [k async for k in app.state.redis.scan_iter("oauth:state:*")]
    assert json.loads(await app.state.redis.get(key))["return_to"] == "/"


async def test_login_on_the_ip_address_moves_to_the_public_name(client, app, db_session):
    install_fake_oidc(app)
    r = await client.get("/auth/login", params={"return_to": "/keys?a=1"}, headers={"Host": "192.168.3.21"})
    assert r.status_code == 302
    assert r.headers["location"] == "https://double.fresh.nsuts.ru/auth/login?return_to=%2Fkeys%3Fa%3D1"
    r = await client.get("/auth/login", headers={"Host": "192.168.3.21:443"})
    assert r.headers["location"] == "https://double.fresh.nsuts.ru/auth/login"
    assert [k async for k in app.state.redis.scan_iter("oauth:state:*")] == []  # the redirect stores no state
    r = await client.get("/auth/login", headers={"Host": "double.fresh.nsuts.ru"})
    assert r.headers["location"].startswith("http://fake-oidc")


async def test_login_does_not_loop_when_the_public_name_is_the_ip_address(client, app, monkeypatch):
    # PUBLIC_HOSTNAME=<MAIN_ADVERTISE_IP>: the redirect to the public name would land on the same host again
    install_fake_oidc(app)
    monkeypatch.setattr(app.state.settings, "public_hostname", "192.168.3.21")
    for host in ("192.168.3.21", "192.168.3.21:443"):
        r = await client.get("/auth/login", params={"return_to": "/keys"}, headers={"Host": host})
        assert r.status_code == 302
        assert r.headers["location"].startswith("http://fake-oidc")


# ---------- request validation ----------


async def test_validator_messages_are_clean_russian_text(client, app, fake):
    me = await login(client, app, USER)
    r = await client.post("/api/keys", json={"alias": "__x"}, headers=csrf_headers(me))
    assert r.status_code == 422
    assert [e["msg"] for e in r.json()["detail"]] == ["Название не может начинаться с «__»"]
    assert "ctx" not in r.json()["detail"][0]


async def test_chat_request_limits(client, app, fake):
    me = await login(client, app, USER)
    too_many = {"model": "m1", "messages": [{"role": "user", "content": "x"}] * (MAX_MESSAGES + 1)}
    too_long = {"model": "m1", "messages": [{"role": "user", "content": "x" * (MAX_CONTENT_CHARS + 1)}]}
    for payload in (too_many, too_long):
        r = await client.post("/api/playground/chat", json=payload, headers=csrf_headers(me))
        assert r.status_code == 422
        assert "x" * 100 not in r.text  # the rejected value is not echoed back
    ok = {"model": "m1", "messages": [{"role": "user", "content": "x" * 100_000}] * MAX_MESSAGES}
    assert (await client.post("/api/playground/chat", json=ok, headers=csrf_headers(me))).status_code == 200


async def test_node_registration_validator_messages_are_russian(client):
    r = await client.post(
        "/api/nodes/register",
        json={**NODE, "url": "http://127.0.0.1:18080"},
        headers={"Authorization": "Bearer agent-test"},
    )
    assert r.status_code == 422
    assert [e["msg"] for e in r.json()["detail"]] == ["Адреса loopback, link-local и неопределённые не допускаются"]
