"""LiteLLM client against real LiteLLM v1.106.6 + Postgres + fake OpenAI backend (Docker)."""

import asyncio
import json
import time
import uuid
from datetime import date, timedelta

import httpx
import pytest

from app.litellm_client import LiteLLMClient, LiteLLMError, LiteLLMUnavailable
from tests.integration.stack import start_stack

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def stack():
    with start_stack() as s:
        yield s


@pytest.fixture
async def client(stack):
    c = LiteLLMClient(stack.url, stack.master_key)
    yield c
    await c.aclose()


def _backend_base(stack, port: int | None = None) -> str:
    return f"http://{stack.backend_alias}:{port or stack.backend_port}/v1"


def _payload(model: str = "test-model") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "привет мир"}], "stream": True}


async def _chat_text(client: LiteLLMClient, key: str, model: str = "test-model") -> str:
    raw = b"".join([chunk async for chunk in client.stream_chat(key, _payload(model))]).decode()
    assert raw.rstrip().endswith("data: [DONE]")
    text = ""
    for line in raw.splitlines():
        if line.startswith("data: {"):
            for choice in json.loads(line[6:]).get("choices", []):
                text += choice.get("delta", {}).get("content") or ""
    return text


async def test_real_health(client):
    assert await client.health() is True


async def test_real_key_lifecycle(stack, client):
    user = f"it-user-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru")
    await client.ensure_user(user, f"{user}@g.nsu.ru")  # second call: "already exists" is fine

    await client.add_model("pm-lifecycle", "test-model", _backend_base(stack), "k8s", "node-a", 4096)
    try:
        key = await client.generate_key(
            user, f"{user}:main", "30d", rpm_limit=60, tpm_limit=100000, metadata={"portal": "1"}
        )
        assert key.key.startswith("sk-") and key.token and key.expires is not None

        # the key works for every model (models: []) and the reply is streamed through
        assert await _chat_text(client, key.key) == "Привет из тестовой модели"

        # usage shows up after LiteLLM's batched spend writes
        today = date.today()
        deadline = time.monotonic() + 60
        activity = await client.user_activity(user, today - timedelta(days=1), today + timedelta(days=1))
        while activity.requests < 1 and time.monotonic() < deadline:
            await asyncio.sleep(3)
            activity = await client.user_activity(user, today - timedelta(days=1), today + timedelta(days=1))
        assert activity.requests >= 1
        assert activity.prompt_tokens > 0 and activity.completion_tokens > 0
        assert key.token in activity.by_key
        assert activity.by_key[key.token].requests >= 1 and activity.by_key[key.token].tokens > 0
        assert sum(d.requests for d in activity.by_day) == activity.requests
        assert sum(d.tokens for d in activity.by_day) == activity.prompt_tokens + activity.completion_tokens

        # another user's activity is empty
        other = await client.user_activity("it-nobody", today - timedelta(days=1), today + timedelta(days=1))
        assert other.requests == 0 and other.by_key == {} and other.by_day == []

        await client.delete_keys([key.token])
        await client.delete_keys([key.token])  # already gone: no error
        with pytest.raises(LiteLLMError) as exc:
            async for _ in client.stream_chat(key.key, _payload()):
                pass
        assert exc.value.status == 401
    finally:
        await client.delete_model("pm-lifecycle")


async def test_real_duplicate_alias_is_an_error(client):
    user = f"it-user-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru")
    key = await client.generate_key(user, f"{user}:dup", None, rpm_limit=None, tpm_limit=None, metadata={})
    assert key.expires is None
    try:
        with pytest.raises(LiteLLMError) as exc:
            await client.generate_key(user, f"{user}:dup", None, rpm_limit=None, tpm_limit=None, metadata={})
        assert exc.value.status == 400
    finally:
        await client.delete_keys([key.token])


async def test_real_model_add_list_delete(stack, client):
    base = _backend_base(stack)
    await client.add_model("pm-it-a", "it-model", base, "k8s", "node-a", 8192)
    await client.add_model("pm-it-b", "it-model", base, "standalone", "node-b", 4096)
    await client.add_model("pm-it-c", "it-model-nolimit", base, "standalone", "node-c", None)
    try:
        # a model added by hand (not portal_managed) must not be listed
        async with httpx.AsyncClient(base_url=stack.url, headers={"Authorization": f"Bearer {stack.master_key}"}) as raw:
            resp = await raw.post(
                "/model/new",
                json={
                    "model_name": "it-manual",
                    "litellm_params": {"model": "openai/it-manual", "api_base": base, "api_key": "none"},
                    "model_info": {"id": "manual-it"},
                },
            )
            assert resp.status_code == 200

        managed = {m.id: m for m in await client.list_managed_models()}
        assert set(managed) >= {"pm-it-a", "pm-it-b"} and "manual-it" not in managed
        a, b = managed["pm-it-a"], managed["pm-it-b"]
        assert (a.model_name, a.api_base, a.source, a.node, a.max_input_tokens) == (
            "it-model",
            base,
            "k8s",
            "node-a",
            8192,
        )
        assert (b.source, b.node, b.max_input_tokens) == ("standalone", "node-b", 4096)
        # LiteLLM does not invent a limit for a deployment that has none
        assert managed["pm-it-c"].max_input_tokens is None

        status = await client.model_group_status()
        assert status["it-model"] == 2 and status["it-manual"] == 1

        await client.delete_model("pm-it-a")
        await client.delete_model("pm-it-a")  # unknown id: no error
        assert "pm-it-a" not in {m.id for m in await client.list_managed_models()}
        assert (await client.model_group_status())["it-model"] == 1
    finally:
        await client.delete_model("pm-it-a")
        await client.delete_model("pm-it-b")
        await client.delete_model("pm-it-c")
        await client.delete_model("manual-it")


async def test_dead_backend_removed_stops_errors(stack, client):
    user = f"it-user-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru")
    key = await client.generate_key(user, f"{user}:dead", None, rpm_limit=None, tpm_limit=None, metadata={})
    await client.add_model("pm-live", "dead-test", _backend_base(stack), "k8s", "node-live", 4096)
    # same host, closed port: connection refused
    await client.add_model("pm-dead", "dead-test", _backend_base(stack, 18099), "k8s", "node-dead", 4096)
    try:
        assert (await client.model_group_status())["dead-test"] == 2
        for _ in range(10):  # traffic while the dead backend is still registered must not crash the gateway
            try:
                await _chat_text(client, key.key, "dead-test")
            except LiteLLMError:
                pass
        await client.delete_model("pm-dead")
        assert (await client.model_group_status())["dead-test"] == 1
        results = [await _chat_text(client, key.key, "dead-test") for _ in range(10)]
        assert results == ["Привет из тестовой модели"] * 10
    finally:
        await client.delete_model("pm-dead")
        await client.delete_model("pm-live")
        await client.delete_keys([key.token])


async def test_real_update_model_key_keeps_id_and_reaches_the_backend(stack):
    """`PATCH /model/{id}/update` swaps the backend key in place: same id, same settings, the router uses the new key."""
    from app.config import key_fingerprint

    first = LiteLLMClient(stack.url, stack.master_key, backend_api_key="backend-key-one")
    second = LiteLLMClient(stack.url, stack.master_key, backend_api_key="backend-key-two")
    user = f"it-user-{uuid.uuid4().hex[:8]}"
    model = "it-key-model"
    try:
        await first.ensure_user(user, f"{user}@g.nsu.ru")
        key = await first.generate_key(user, f"{user}:k", None, rpm_limit=None, tpm_limit=None, metadata={})
        await first.add_model("pm-it-key", model, _backend_base(stack), "standalone", "node-k", 4096)

        async def last_auth() -> str:
            async with httpx.AsyncClient() as raw:
                return (await raw.get(stack.backend_url + "/_debug/last_auth")).json()["authorization"]

        assert await _chat_text(first, key.key, model) == "Привет из тестовой модели"
        assert await last_auth() == "Bearer backend-key-one"
        (entry,) = [m for m in await first.list_managed_models() if m.id == "pm-it-key"]
        assert entry.key_fingerprint == key_fingerprint("backend-key-one")

        await second.update_model_key("pm-it-key")

        (entry,) = [m for m in await second.list_managed_models() if m.id == "pm-it-key"]
        assert entry.key_fingerprint == key_fingerprint("backend-key-two")
        assert (entry.model_name, entry.source, entry.node, entry.max_input_tokens) == (model, "standalone", "node-k", 4096)
        assert entry.api_base == _backend_base(stack)
        # the router picked the new key up (it may take a moment to reload)
        deadline = time.monotonic() + 30
        while True:
            assert await _chat_text(second, key.key, model) == "Привет из тестовой модели"
            if await last_auth() == "Bearer backend-key-two" or time.monotonic() > deadline:
                break
            await asyncio.sleep(2)
        assert await last_auth() == "Bearer backend-key-two"
    finally:
        await first.delete_model("pm-it-key")
        await first.aclose()
        await second.aclose()


async def test_real_duplicate_model_add(stack, client):
    """What v1.106.6 answers when an id is added twice (a stale worker listing can make the controller try).

    It is a generic 500 "Failed to add model to db" (the unique-constraint detail stays in LiteLLM's log), so the
    client cannot tell it from another database failure: the backend controller treats it as a failed add of
    that one entry and does not retry it within its recent-writes window. The first entry is untouched.
    """
    base = _backend_base(stack)
    await client.add_model("pm-dup", "dup-model", base, "k8s", "node-a", 4096, max_parallel_requests=2)
    try:
        with pytest.raises(LiteLLMUnavailable) as exc:
            await client.add_model("pm-dup", "dup-model", base, "k8s", "node-a", 4096, max_parallel_requests=2)
        assert "Failed to add model to db" in str(exc.value)
        assert [m.id for m in await client.list_managed_models()].count("pm-dup") == 1
        await client.update_model_limit("pm-dup", 3)
        (m,) = [m for m in await client.list_managed_models() if m.id == "pm-dup"]
        assert m.max_parallel_requests == 3
    finally:
        await client.delete_model("pm-dup")


# ---------- access groups and the system prompt (2-23) ----------


async def _eventually(check, what: str, timeout: float = 45.0):
    """Polls an async check until it returns a truthy value (the router reloads an edited entry within a moment)."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(1.5)
    raise AssertionError(f"{what} did not happen within {timeout:.0f} s (last: {last!r})")


async def _refused(client: LiteLLMClient, key: str, model: str) -> LiteLLMError | None:
    try:
        await _chat_text(client, key, model)
    except LiteLLMError as exc:
        return exc
    return None


async def _model_ids(stack, key: str) -> set[str]:
    async with httpx.AsyncClient(base_url=stack.url, headers={"Authorization": f"Bearer {key}"}) as raw:
        resp = await raw.get("/v1/models")
        assert resp.status_code == 200, resp.text
        return {m["id"] for m in resp.json()["data"]}


async def _backend_messages(stack) -> list[dict]:
    async with httpx.AsyncClient() as raw:
        return (await raw.get(stack.backend_url + "/_debug/last_messages")).json()["messages"]


async def test_real_access_groups_separate_public_and_experimental_models(stack, client):
    from app.litellm_client import models_for_role

    base = _backend_base(stack)
    user = f"it-user-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru")
    await client.add_model("pm-ag-pub", "ag-pub", base, "k8s", "n1", 4096)
    await client.add_model("pm-ag-exp", "ag-exp", base, "k8s", "n1", 4096, experimental=True)
    u = await client.generate_key(user, f"{user}:u", None, None, None, {}, models=models_for_role("user"))
    a = await client.generate_key(user, f"{user}:a", None, None, None, {}, models=models_for_role("admin"))
    old = await client.generate_key(user, f"{user}:old", None, None, None, {}, models=[])
    try:
        assert await _chat_text(client, u.key, "ag-pub") == "Привет из тестовой модели"
        denied = await _refused(client, u.key, "ag-exp")
        assert denied is not None and denied.status == 403, denied
        assert "key_model_access_denied" in denied.body, denied.body  # what the playground maps to Russian
        assert await _chat_text(client, a.key, "ag-pub") == "Привет из тестовой модели"
        assert await _chat_text(client, a.key, "ag-exp") == "Привет из тестовой модели"
        assert await _chat_text(client, old.key, "ag-exp") == "Привет из тестовой модели"  # models: [] is every model

        # the key's own model list shows only what it can call
        user_models = await _model_ids(stack, u.key)
        assert "ag-pub" in user_models and "ag-exp" not in user_models
        assert {"ag-pub", "ag-exp"} <= await _model_ids(stack, a.key)

        # the list of a managed entry reports the group
        managed = {m.id: m for m in await client.list_managed_models()}
        assert managed["pm-ag-pub"].access_groups == ("public",)
        assert managed["pm-ag-exp"].access_groups == ("experimental",)

        # a flag change moves the live entry to the other group in place (same id), no delete and add
        await client.update_model_access("pm-ag-pub", True, None)
        await _eventually(lambda: _refused(client, u.key, "ag-pub"), "the user key refused on the model made experimental")
        assert await _chat_text(client, a.key, "ag-pub") == "Привет из тестовой модели"
        await client.update_model_access("pm-ag-pub", False, None)

        async def allowed_again():
            return (await _refused(client, u.key, "ag-pub")) is None

        await _eventually(allowed_again, "the user key allowed again on the model made public")
        managed = {m.id: m for m in await client.list_managed_models()}
        assert managed["pm-ag-pub"].access_groups == ("public",)

        # removing the admin role: the same key value stops reaching the experimental group; update_key_models
        # takes the token hash that generate_key returned
        await client.update_key_models([a.token], models_for_role("user"))
        denied = await _eventually(lambda: _refused(client, a.key, "ag-exp"), "the demoted admin key refused")
        assert denied.status == 403
        assert await _chat_text(client, a.key, "ag-pub") == "Привет из тестовой модели"  # the key itself lives on
        # the old unrestricted key is brought in line the same way
        await client.update_key_models([old.token], models_for_role("user"))
        await _eventually(lambda: _refused(client, old.key, "ag-exp"), "the migrated old key refused")
        # promotion
        await client.update_key_models([a.token], models_for_role("admin"))

        async def promoted():
            return (await _refused(client, a.key, "ag-exp")) is None

        await _eventually(promoted, "the promoted key allowed")
    finally:
        await client.delete_keys([u.token, a.token, old.token])
        await client.delete_model("pm-ag-pub")
        await client.delete_model("pm-ag-exp")


async def test_real_model_info_of_a_user_key_does_not_list_experimental_models(stack, client):
    """/model/info answers any valid key: it must not show the experimental models or their prompts.

    It does show the entries a key can call WITH their `model_info.system_prompt` (checked on v1.106.6): the prompt of
    a public model is readable by any user who holds a key. That is by the design of the task brief (the prompt is a
    class-wide default, not a secret); the experimental ones stay hidden, which is what is asserted here."""
    from app.litellm_client import models_for_role

    base = _backend_base(stack)
    user = f"it-user-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru")
    await client.add_model(
        "pm-leak-exp", "leak-exp", base, "k8s", "n1", 4096, experimental=True, system_prompt="СЕКРЕТНЫЙ ПРОМПТ"
    )
    await client.add_model("pm-leak-pub", "leak-pub", base, "k8s", "n1", 4096, system_prompt="ПУБЛИЧНЫЙ ПРОМПТ")
    u = await client.generate_key(user, f"{user}:u", None, None, None, {}, models=models_for_role("user"))
    try:
        async with httpx.AsyncClient(base_url=stack.url, headers={"Authorization": f"Bearer {u.key}"}) as raw:
            resp = await raw.get("/model/info")
        assert resp.status_code == 200
        assert "СЕКРЕТНЫЙ ПРОМПТ" not in resp.text and "leak-exp" not in resp.text, (resp.status_code, resp.text[:500])
    finally:
        await client.delete_keys([u.token])
        await client.delete_model("pm-leak-exp")
        await client.delete_model("pm-leak-pub")


async def _chat_raw(stack, key: str, model: str, messages: list[dict], stream: bool) -> httpx.Response:
    async with httpx.AsyncClient(base_url=stack.url, headers={"Authorization": f"Bearer {key}"}, timeout=60) as raw:
        return await raw.post("/v1/chat/completions", json={"model": model, "messages": messages, "stream": stream})


async def test_real_system_prompt_is_added_only_to_chats_without_one(stack, client):
    base = _backend_base(stack)
    user = f"it-user-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru")
    prompt = "Отвечай строго по-русски."
    await client.add_model("pm-sp", "sp-model", base, "k8s", "n1", 4096, system_prompt=prompt)
    await client.add_model("pm-sp-none", "sp-none", base, "k8s", "n1", 4096)
    key = await client.generate_key(user, f"{user}:sp", None, None, None, {})
    hi = {"role": "user", "content": "привет"}
    try:
        for stream in (False, True):
            r = await _chat_raw(stack, key.key, "sp-model", [hi], stream)
            assert r.status_code == 200, r.text
            assert await _backend_messages(stack) == [{"role": "system", "content": prompt}, hi], stream
        # a system or developer message of the client keeps the request as it is
        for role in ("system", "developer"):
            mine = {"role": role, "content": "мои правила"}
            r = await _chat_raw(stack, key.key, "sp-model", [mine, hi], True)
            assert r.status_code == 200, r.text
            seen = [m.get("content") for m in await _backend_messages(stack)]
            assert prompt not in seen and mine["content"] in seen, role
        # a model without a prompt is untouched
        r = await _chat_raw(stack, key.key, "sp-none", [hi], False)
        assert r.status_code == 200, r.text
        assert await _backend_messages(stack) == [hi]

        # a new text applies in place; clearing it stops the insertion
        await client.update_model_access("pm-sp", False, "Новый текст")

        async def new_text_applied():
            await _chat_raw(stack, key.key, "sp-model", [hi], False)
            return (await _backend_messages(stack))[0].get("content") == "Новый текст"

        await _eventually(new_text_applied, "the new system prompt")
        await client.update_model_access("pm-sp", False, None)

        async def cleared():
            await _chat_raw(stack, key.key, "sp-model", [hi], False)
            return await _backend_messages(stack) == [hi]

        await _eventually(cleared, "the cleared system prompt")
        (entry,) = [m for m in await client.list_managed_models() if m.id == "pm-sp"]
        assert entry.system_prompt == "" and entry.access_groups == ("public",)
    finally:
        await client.delete_keys([key.token])
        await client.delete_model("pm-sp")
        await client.delete_model("pm-sp-none")


async def test_real_v1_metadata_of_a_user_key_does_not_reveal_an_experimental_model(stack, client):
    """/v1/models, /v1/models/<id> and /v1/model/info are what a user reaches through the public address."""
    from app.litellm_client import models_for_role

    base = _backend_base(stack)
    user = f"it-user-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru")
    await client.add_model("pm-v1-pub", "v1-pub", base, "k8s", "n1", 4096)
    await client.add_model("pm-v1-exp", "v1-exp", base, "k8s", "n1", 4096, experimental=True, system_prompt="ЗАКРЫТЫЙ")
    u = await client.generate_key(user, f"{user}:u", None, None, None, {}, models=models_for_role("user"))
    a = await client.generate_key(user, f"{user}:a", None, None, None, {}, models=models_for_role("admin"))
    try:
        async with httpx.AsyncClient(base_url=stack.url, headers={"Authorization": f"Bearer {u.key}"}) as raw:
            listing = await raw.get("/v1/models")
            one = await raw.get("/v1/models/v1-exp")
            info = await raw.get("/v1/model/info")
            public_one = await raw.get("/v1/models/v1-pub")
        assert listing.status_code == 200 and "v1-exp" not in listing.text and "v1-pub" in listing.text
        assert one.status_code in (401, 403, 404), (one.status_code, one.text[:300])
        assert "ЗАКРЫТЫЙ" not in one.text
        assert info.status_code == 200 and "v1-exp" not in info.text and "ЗАКРЫТЫЙ" not in info.text
        # v1.106.6 does not serve /v1/models/<id> to an llm_api key at all (403 "not allowed to call this route"),
        # for a public model as for an experimental one: the single-model card is no way to probe for a model
        assert public_one.status_code == one.status_code == 403
        assert "v1-exp" not in one.text.replace("/v1/models/v1-exp", "")
        assert "v1-exp" in await _model_ids(stack, a.key)  # while the admin's key lists it
    finally:
        await client.delete_keys([u.token, a.token])
        await client.delete_model("pm-v1-pub")
        await client.delete_model("pm-v1-exp")
