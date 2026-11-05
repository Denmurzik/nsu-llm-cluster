"""Task SEC-1, rules 7 and 8 and L6: limits of the user as a whole, the key creation rate, the Playground's places in
flight and memory budget, and neutral gateway errors."""

import asyncio
import json
import time

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app import settings_store, user_limits
from app.api import playground
from app.controllers.user_limits import SWEEP_EVERY_S, UserLimitsController
from app.litellm_client import LiteLLMError
from app.models import User
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}
CHAT = {"model": "gemma-4-e4b", "messages": [{"role": "user", "content": "Привет"}]}


@pytest.fixture
def settings(settings):
    settings.key_create_per_hour = 3
    settings.playground_max_inflight = 2
    return settings


@pytest_asyncio.fixture
async def fake(app):
    app.state.litellm = FakeLiteLLM(reply="Привет из теста")
    return app.state.litellm


async def user_row(app, email: str) -> User:
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(User).where(User.email == email))


async def other_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


# ---- rule 7: the user's limits in LiteLLM --------------------------------------------------------------------------


async def test_a_new_key_gives_its_owner_the_same_limits_as_a_whole(client, app, fake):
    me = await login(client, app, USER)
    r = await client.post("/api/keys", json={"alias": "a"}, headers=csrf_headers(me))
    assert r.status_code == 201, r.text
    assert fake.user_limits[me["id"]] == {"rpm_limit": 60, "tpm_limit": 100000}


async def test_the_playground_key_gives_its_owner_the_limits_too(client, app, fake):
    me = await login(client, app, USER)
    async with app.state.sessionmaker() as db:
        u = await db.get(User, (await user_row(app, USER["email"])).id)
        u.rpm_limit = 7
        await db.commit()
    r = await client.post("/api/playground/chat", json=CHAT, headers=csrf_headers(me))
    assert r.status_code == 200, r.text
    assert fake.user_limits[me["id"]] == {"rpm_limit": 7, "tpm_limit": 100000}


async def test_the_admin_changing_a_users_limits_reaches_the_user_even_without_keys(client, app, fake):
    other = await other_client(app)
    await login(other, app, USER)
    ivan = await user_row(app, USER["email"])
    h = csrf_headers(await login(client, app, ADMIN))
    await app.state.redis.hset(user_limits.SYNCED_KEY, str(ivan.id), "60:100000")
    r = await client.patch(f"/api/admin/users/{ivan.id}", json={"rpm_limit": 5, "tpm_limit": 900}, headers=h)
    assert r.status_code == 200, r.text
    assert fake.user_limit_updates[-1] == (str(ivan.id), 5, 900)
    # the sweep sends them once more (a failed change would otherwise stay in LiteLLM)
    assert await app.state.redis.hget(user_limits.SYNCED_KEY, str(ivan.id)) is None


async def test_the_sweep_sends_each_user_with_a_key_once_and_again_after_a_change(client, app, fake):
    me = await login(client, app, USER)
    assert (await client.post("/api/keys", json={"alias": "a"}, headers=csrf_headers(me))).status_code == 201
    other = await other_client(app)
    await login(other, app, {**USER, "sub": "nokeys", "email": "nokeys@g.nsu.ru"})  # no key: not sent
    fake.user_limit_updates.clear()

    assert await user_limits.sweep(app.state) == 1
    assert fake.user_limit_updates == [(me["id"], 60, 100000)]
    assert 0 < await app.state.redis.ttl(user_limits.SYNCED_KEY) <= user_limits.SYNCED_TTL_S
    assert await user_limits.sweep(app.state) == 0  # nothing changed: no call

    async with app.state.sessionmaker() as db:  # the global setting changed in the panel
        await settings_store.set(db, "key_rpm_limit", 30, None, settings=app.state.settings)
        await db.commit()
    assert await user_limits.sweep(app.state) == 1
    assert fake.user_limit_updates[-1] == (me["id"], 30, 100000)

    await user_limits.forget(app.state.redis, me["id"])
    assert await user_limits.sweep(app.state) == 1


async def test_a_refused_user_is_left_for_the_next_sweep(client, app, fake, monkeypatch):
    me = await login(client, app, USER)
    assert (await client.post("/api/keys", json={"alias": "a"}, headers=csrf_headers(me))).status_code == 201

    async def refuse(user_id, rpm, tpm):
        raise LiteLLMError(400, "nope")

    monkeypatch.setattr(fake, "update_user_limits", refuse)
    assert await user_limits.sweep(app.state) == 0
    assert await app.state.redis.hget(user_limits.SYNCED_KEY, me["id"]) is None


async def test_the_controller_sweeps_at_most_once_a_minute(app, fake, monkeypatch):
    calls = []

    async def sweep(state):
        calls.append(1)
        return 0

    monkeypatch.setattr(user_limits, "sweep", sweep)
    now = [1000.0]
    ctl = UserLimitsController(app.state, clock=lambda: now[0])
    await ctl.reconcile_once()
    await ctl.reconcile_once()
    assert calls == [1]
    now[0] += SWEEP_EVERY_S
    await ctl.reconcile_once()
    assert calls == [1, 1]
    fake.fail_health = True  # LiteLLM down: nothing to send to
    now[0] += SWEEP_EVERY_S
    await ctl.reconcile_once()
    assert calls == [1, 1]


async def test_creating_keys_is_limited_per_hour(client, app, fake):
    me = await login(client, app, USER)
    h = csrf_headers(me)
    for alias in ("a", "b", "c"):
        r = await client.post("/api/keys", json={"alias": alias}, headers=h)
        assert r.status_code == 201, r.text
        assert (await client.delete(f"/api/keys/{r.json()['id']}", headers=h)).status_code in (200, 204)
    r = await client.post("/api/keys", json={"alias": "d"}, headers=h)  # deleting and re-creating is no way around
    assert r.status_code == 429
    assert r.json()["detail"] == "Слишком много новых ключей: не больше 3 в час. Попробуйте позже."
    assert 0 < int(r.headers["Retry-After"]) <= 3600
    assert len(fake.keys) == 0


# ---- rule 8: the Playground's places in flight and memory budget ---------------------------------------------------


async def test_a_user_with_all_places_taken_gets_429_before_the_body_is_read(client, app, fake, monkeypatch):
    me = await login(client, app, USER)
    read = []
    original = playground._read_chat_request

    async def spy(*args, **kwargs):
        read.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(playground, "_read_chat_request", spy)
    name = playground.INFLIGHT_PREFIX + me["id"]
    await app.state.redis.zadd(name, {"r1": time.time(), "r2": time.time()})  # two answers of this user stream now
    r = await client.post("/api/playground/chat", json=CHAT, headers=csrf_headers(me))
    assert r.status_code == 429
    assert r.json()["detail"] == playground.too_many_inflight(2)
    assert r.headers["Retry-After"] == "5"
    assert read == [] and not fake.chat_calls

    # a place whose holder stopped refreshing it (its worker died) is free again
    await app.state.redis.zadd(name, {"r1": time.time() - playground.INFLIGHT_STALE_S - 1})
    r = await client.post("/api/playground/chat", json=CHAT, headers=csrf_headers(me))
    assert r.status_code == 200, r.text
    assert read == [1]
    assert await app.state.redis.zrange(name, 0, -1) == ["r2"]  # its own place is given back at the end


async def test_places_are_per_user(client, app, fake):
    me = await login(client, app, USER)
    other = await other_client(app)
    them = await login(other, app, {**USER, "sub": "petr", "email": "petr@g.nsu.ru"})
    now = time.time()
    await app.state.redis.zadd(playground.INFLIGHT_PREFIX + me["id"], {"r1": now, "r2": now})
    r = await other.post("/api/playground/chat", json=CHAT, headers=csrf_headers(them))
    assert r.status_code == 200, r.text


async def test_a_refused_request_gives_its_place_back(client, app, fake):
    me = await login(client, app, USER)

    async def refuse(api_key, payload):
        raise LiteLLMError(400, json.dumps({"error": {"message": "context length exceeded"}}))
        yield b""

    fake.stream_chat = refuse
    for _ in range(3):  # more than the 2 places: each refusal gave its place back
        assert (await client.post("/api/playground/chat", json=CHAT, headers=csrf_headers(me))).status_code == 400
    assert await app.state.redis.zcard(playground.INFLIGHT_PREFIX + me["id"]) == 0
    assert playground.BODY_BUDGET.used == 0


async def test_the_workers_body_budget_spent_is_503_and_comes_back(client, app, fake, monkeypatch):
    me = await login(client, app, USER)
    limit = app.state.settings.playground_body_budget_mib * 1024 * 1024
    monkeypatch.setattr(playground.BODY_BUDGET, "used", limit - 10)  # other requests of this worker hold the rest
    r = await client.post("/api/playground/chat", json=CHAT, headers=csrf_headers(me))
    assert r.status_code == 503
    assert r.json()["detail"] == playground.OVERLOADED
    assert r.headers["Retry-After"] == "5"
    assert playground.BODY_BUDGET.used == limit - 10  # nothing of the refused request stays taken
    assert not fake.chat_calls
    assert await app.state.redis.zcard(playground.INFLIGHT_PREFIX + me["id"]) == 0

    playground.BODY_BUDGET.used = 0
    r = await client.post("/api/playground/chat", json=CHAT, headers=csrf_headers(me))
    assert r.status_code == 200 and r.text.rstrip().endswith("data: [DONE]")
    assert playground.BODY_BUDGET.used == 0  # given back once the answer ended


def test_the_budget_takes_all_or_nothing():
    b = playground.BodyBudget()
    assert b.take(60, 100) and not b.take(41, 100) and b.used == 60
    b.give(60)
    b.give(5)
    assert b.used == 0


# ---- L6: neutral gateway errors -------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 403, 422, 408])
async def test_other_gateway_refusals_do_not_show_its_text(client, app, fake, status):
    me = await login(client, app, USER)
    inside = "No deployments available for model; api_base=http://10.0.0.7:18080/v1 fallbacks=['grp-x']"

    async def refuse(api_key, payload):
        raise LiteLLMError(status, json.dumps({"error": {"message": inside}}))
        yield b""

    fake.stream_chat = refuse
    r = await client.post("/api/playground/chat", json=CHAT, headers=csrf_headers(me))
    assert r.status_code == status
    assert r.json() == {"detail": "Модель вернула ошибку"}
    assert "10.0.0.7" not in r.text and "grp-x" not in r.text


async def test_a_400_and_a_413_keep_the_models_own_reason(client, app, fake):
    me = await login(client, app, USER)
    for status in (400, 413):
        async def refuse(api_key, payload, status=status):
            raise LiteLLMError(status, json.dumps({"error": {"message": "context length exceeded"}}))
            yield b""

        fake.stream_chat = refuse
        r = await client.post("/api/playground/chat", json=CHAT, headers=csrf_headers(me))
        assert r.status_code == status
        assert r.json() == {"detail": "Модель вернула ошибку (context length exceeded)"}


# ---- fix round 1 of the SEC-1 review ---------------------------------------------------------------------------------


async def test_a_key_the_gateway_refused_to_create_does_not_count(client, app, fake, monkeypatch):
    """Minor 7: only keys that were created count in the hourly window; a LiteLLM failure spends nothing."""
    from app.litellm_client import LiteLLMUnavailable

    me = await login(client, app, USER)
    h = csrf_headers(me)
    original = fake.generate_key

    async def down(*args, **kwargs):
        raise LiteLLMUnavailable("down")

    monkeypatch.setattr(fake, "generate_key", down)
    for alias in ("x1", "x2", "x3", "x4"):
        assert (await client.post("/api/keys", json={"alias": alias}, headers=h)).status_code == 503
    monkeypatch.setattr(fake, "generate_key", original)
    for alias in ("a", "b", "c"):
        assert (await client.post("/api/keys", json={"alias": alias}, headers=h)).status_code == 201
    assert (await client.post("/api/keys", json={"alias": "d"}, headers=h)).status_code == 429


class _FakePlace:
    def __init__(self):
        self.released = 0

    async def release(self):
        self.released += 1


async def _never_runs():
    yield b"data: never\n\n"


@pytest.mark.parametrize("failure", [OSError("client gone"), ConnectionResetError("reset")])
async def test_an_answer_that_never_started_gives_the_budget_and_the_place_back(failure):
    """Important 4: the client went away before the first byte of the answer (`http.response.start` fails), so the body
    generator never ran and its `finally` never came; the response gives the budget and the place back itself, once."""
    before = playground.BODY_BUDGET.used
    budget = playground._Body(10**9)
    budget.take(5_000_000)
    place = _FakePlace()
    upstream = _never_runs()
    response = playground._ReleasingResponse(
        playground._relay(upstream, b"", place, budget), upstream=upstream, place=place, budget=budget,
        media_type="text/event-stream",
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        raise failure

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "method": "POST", "path": "/", "headers": []}
    with pytest.raises(Exception):
        await response(scope, receive, send)
    assert playground.BODY_BUDGET.used == before
    assert place.released == 1


async def test_an_answer_that_streamed_gives_the_budget_back_once():
    before = playground.BODY_BUDGET.used
    budget = playground._Body(10**9)
    budget.take(1000)
    place = _FakePlace()

    async def upstream_gen():
        yield b"data: x\n\n"

    upstream = upstream_gen()
    first = await anext(upstream)
    response = playground._ReleasingResponse(
        playground._relay(upstream, first, place, budget), upstream=upstream, place=place, budget=budget,
        media_type="text/event-stream",
    )
    sent = []

    async def receive():
        await asyncio.sleep(3600)  # the client stays

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "method": "POST", "path": "/", "headers": []}
    await response(scope, receive, send)
    assert any(m.get("body") == b"data: x\n\n" for m in sent)
    assert playground.BODY_BUDGET.used == before
    assert place.released >= 1  # the generator and the response both ask, the place itself is freed once (_Place.held)
