import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.api.playground import KEY_TTL_S
from app.litellm_client import Activity, DayUsage, LiteLLMError
from app.models import ApiKeyRef
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

CHAT = {"model": "gemma-4-e4b", "messages": [{"role": "user", "content": "Привет"}]}


@pytest.fixture
async def fake(app):
    app.state.litellm = FakeLiteLLM(reply="Привет из теста")
    return app.state.litellm


@pytest.fixture
async def me(client, app, fake):
    return await login(client, app)


async def chat(client, me, payload=None):
    return await client.post("/api/playground/chat", json=payload or CHAT, headers=csrf_headers(me))


def deltas(body: str) -> str:
    out = []
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[6:])["choices"][0]["delta"].get("content", ""))
    return "".join(out)


async def test_usage_aggregates_days(client, fake, me):
    today = datetime.now(UTC).date()
    fake.activity[me["id"]] = Activity(
        prompt_tokens=100,
        completion_tokens=50,
        requests=3,
        by_day=[
            DayUsage(day=today - timedelta(days=2), tokens=40, requests=1),
            DayUsage(day=today, tokens=110, requests=2),
        ],
    )
    r = await client.get("/api/usage", params={"days": 7})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 7
    assert (body["prompt_tokens"], body["completion_tokens"], body["requests"]) == (100, 50, 3)
    by_day = {d["day"]: d for d in body["by_day"]}
    assert len(body["by_day"]) == 7
    assert [d["day"] for d in body["by_day"]] == sorted(by_day)
    assert by_day[today.isoformat()] == {"day": today.isoformat(), "tokens": 110, "requests": 2}
    assert by_day[(today - timedelta(days=2)).isoformat()]["tokens"] == 40
    yesterday = (today - timedelta(days=1)).isoformat()
    assert by_day[yesterday] == {"day": yesterday, "tokens": 0, "requests": 0}


async def test_usage_defaults_and_validates_days(client, fake, me):
    assert (await client.get("/api/usage")).json()["days"] == 7
    assert len((await client.get("/api/usage", params={"days": 1})).json()["by_day"]) == 1
    assert len((await client.get("/api/usage", params={"days": 30})).json()["by_day"]) == 30
    assert (await client.get("/api/usage", params={"days": 5})).status_code == 422


async def test_usage_litellm_down_503(client, fake, me):
    fake.unavailable = True
    r = await client.get("/api/usage")
    assert r.status_code == 503
    assert r.json() == {"detail": "Шлюз моделей недоступен"}


async def test_playground_streams_and_reuses_key(client, app, fake, me, db_session):
    r1 = await chat(client, me)
    assert r1.status_code == 200
    assert r1.headers["content-type"].startswith("text/event-stream")
    assert r1.text.rstrip().endswith("data: [DONE]")
    assert deltas(r1.text) == "Привет из теста"
    r2 = await chat(client, me)
    assert r2.status_code == 200 and r2.text.rstrip().endswith("data: [DONE]")

    assert len(fake.keys) == 1
    (record,) = fake.keys.values()
    assert record["alias"] == f"{me['id'][:8]}-__playground"
    assert [call[0] for call in fake.chat_calls] == [record["key"]] * 2
    payload = fake.chat_calls[0][1]
    assert payload["model"] == "gemma-4-e4b" and payload["stream"] is True
    assert payload["messages"] == CHAT["messages"]
    assert "max_tokens" not in payload

    row = (await db_session.scalars(select(ApiKeyRef))).one()
    assert row.is_playground
    assert record["key"] not in {row.litellm_token, row.alias, row.masked}
    assert record["key"] not in r1.text
    assert (await client.get("/api/keys")).json() == []


async def test_playground_passes_max_tokens(client, fake, me):
    r = await chat(client, me, {**CHAT, "max_tokens": 256})
    assert r.status_code == 200
    assert fake.chat_calls[0][1]["max_tokens"] == 256


async def test_playground_reissues_key_after_restart(client, app, fake, me, db_session):
    await chat(client, me)
    (old_record,) = fake.keys.values()
    await app.state.redis.flushall()  # Redis restarted: the raw key is gone
    r = await chat(client, me)
    assert r.status_code == 200 and r.text.rstrip().endswith("data: [DONE]")
    (new_record,) = fake.keys.values()
    assert new_record["key"] != old_record["key"]
    assert len((await db_session.scalars(select(ApiKeyRef))).all()) == 1


async def test_playground_retries_once_on_401(client, app, fake, me, db_session):
    await chat(client, me)
    fake.keys.clear()  # key revoked behind the portal's back
    r = await chat(client, me)
    assert r.status_code == 200 and r.text.rstrip().endswith("data: [DONE]")
    assert len(fake.keys) == 1
    assert len((await db_session.scalars(select(ApiKeyRef))).all()) == 1


async def test_playground_rejects_large_max_tokens(client, fake, me):
    r = await chat(client, me, {**CHAT, "max_tokens": 4097})
    assert r.status_code == 422
    r = await chat(client, me, {**CHAT, "max_tokens": 0})
    assert r.status_code == 422
    assert not fake.keys and not fake.chat_calls


async def test_playground_requires_csrf_and_valid_body(client, fake, me):
    assert (await client.post("/api/playground/chat", json=CHAT)).status_code == 403
    assert (await chat(client, me, {"model": "m", "messages": []})).status_code == 422
    assert not fake.chat_calls


async def test_playground_upstream_400_is_russian_with_the_original_in_parentheses(client, fake, me):
    async def refuse(api_key, payload):
        raise LiteLLMError(400, json.dumps({"error": {"message": "context length exceeded"}}))
        yield b""

    fake.stream_chat = refuse
    r = await chat(client, me)
    assert r.status_code == 400
    assert r.json() == {"detail": "Модель вернула ошибку (context length exceeded)"}

    async def refuse_plain(api_key, payload):
        raise LiteLLMError(422, "not json")
        yield b""

    fake.stream_chat = refuse_plain
    r = await chat(client, me)
    # other refusals than 400/413/429 do not show the gateway's text (task SEC-1, L6)
    assert r.status_code == 422 and r.json()["detail"] == "Модель вернула ошибку"


async def test_playground_litellm_down_503(client, fake, me):
    fake.unavailable = True
    r = await chat(client, me)
    assert r.status_code == 503
    assert r.json() == {"detail": "Шлюз моделей недоступен"}


async def test_playground_key_lives_only_in_redis_for_12_hours(client, app, fake, me, db_session):
    assert (await chat(client, me)).status_code == 200
    (record,) = fake.keys.values()
    uid = me["id"]
    assert await app.state.redis.keys("pg:*") == [f"pg:{uid}"]
    assert await app.state.redis.get(f"pg:{uid}") == record["key"]
    assert KEY_TTL_S == 12 * 3600 and KEY_TTL_S - 60 < await app.state.redis.ttl(f"pg:{uid}") <= KEY_TTL_S
    assert not hasattr(app.state, "playground_keys")
    (ref,) = (await db_session.scalars(select(ApiKeyRef))).all()
    assert record["key"] not in (ref.litellm_token, ref.masked)


async def test_playground_key_miss_holds_no_transaction_across_litellm_calls(client, app, fake, me):
    # ensure_user and generate_key are network calls: a transaction left open by the SELECT would sit
    # "idle in transaction" on a pooled connection for as long as LiteLLM takes to answer
    seen = []
    original = fake.generate_key

    async def spy(*args, **kwargs):
        async with app.state.sessionmaker() as probe:
            seen.append(await probe.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE pid <> pg_backend_pid()"
                " AND datname = current_database() AND state LIKE 'idle in transaction%'")))
        return await original(*args, **kwargs)

    fake.generate_key = spy
    assert (await chat(client, me)).status_code == 200
    assert seen == [0]


async def test_playground_uses_the_key_another_worker_cached(client, app, fake, me):
    assert (await chat(client, me)).status_code == 200
    other = await fake.generate_key(me["id"], "other-worker", None, rpm_limit=None, tpm_limit=None, metadata={})
    await app.state.redis.set(f"pg:{me['id']}", other.key, ex=KEY_TTL_S)  # issued by another API worker
    assert (await chat(client, me)).status_code == 200
    assert fake.chat_calls[-1][0] == other.key
    assert len(fake.keys) == 2  # nothing new was issued


async def test_playground_429_keeps_the_russian_message_and_retry_after(client, fake, me):
    text = "Модель gemma-4-e4b перегружена: все слоты и очередь заняты. Повторите запрос через 5 с."

    async def overloaded(api_key, payload):
        raise LiteLLMError(429, json.dumps({"error": {"message": text}}), retry_after="5")
        yield b""

    fake.stream_chat = overloaded
    r = await chat(client, me)
    assert r.status_code == 429
    assert r.json() == {"detail": text}
    assert r.headers["retry-after"] == "5"

    async def key_limit(api_key, payload):
        raise LiteLLMError(429, json.dumps({"error": {"message": "Rate limit exceeded"}}))
        yield b""

    fake.stream_chat = key_limit
    r = await chat(client, me)
    assert r.status_code == 429
    assert r.json() == {"detail": "Слишком много запросов, повторите позже (Rate limit exceeded)"}
    assert "retry-after" not in r.headers


async def test_playground_redis_down_503(client, app, fake, me, monkeypatch):
    import redis.exceptions

    async def broken(*args, **kwargs):
        raise redis.exceptions.ConnectionError("down")

    monkeypatch.setattr(app.state.redis, "get", broken)
    r = await chat(client, me)
    assert r.status_code == 503
    assert r.json() == {"detail": "Шлюз моделей недоступен"}
    assert not fake.keys


async def test_playground_full_connection_pool_is_a_russian_429(client, fake, me):
    from app.litellm_client import LiteLLMBusy

    async def busy(api_key, payload):
        raise LiteLLMBusy("no free connection to LiteLLM")
        yield b""

    fake.stream_chat = busy
    r = await chat(client, me)
    assert r.status_code == 429
    assert r.json() == {"detail": "Слишком много одновременных запросов к шлюзу. Повторите запрос через 5 с."}
    assert r.headers["retry-after"] == "5"


async def test_playground_lock_is_released_only_by_its_holder(app):
    from app.api.playground import _user_lock

    redis = app.state.redis
    async with _user_lock(redis, "u1"):
        assert await redis.get("pg-lock:u1")
    assert await redis.get("pg-lock:u1") is None

    async with _user_lock(redis, "u2"):
        await redis.set("pg-lock:u2", "someone-else")  # our lock expired and another worker took it
    assert await redis.get("pg-lock:u2") == "someone-else"  # not deleted on our way out
