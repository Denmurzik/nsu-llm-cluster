import asyncio
import inspect
import json
from datetime import date

import httpx
import pytest
import respx

from app.config import key_fingerprint
from app.litellm_client import (
    GeneratedKey,
    LiteLLMBusy,
    LiteLLMClient,
    LiteLLMError,
    LiteLLMUnavailable,
    ManagedModel,
    access_groups,
    models_for_role,
)
from tests.fakes.litellm import FakeLiteLLM

BASE = "http://litellm.test:4000"


@pytest.fixture
def router():
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        yield r


@pytest.fixture
async def client():
    c = LiteLLMClient(BASE, "sk-test")
    yield c
    await c.aclose()


async def test_generate_key_sends_expected_body(router, client):
    route = router.post("/key/generate").mock(
        return_value=httpx.Response(
            200, json={"key": "sk-abc", "token": "hash1", "expires": "2026-12-01T10:00:00.000000Z"}
        )
    )
    key = await client.generate_key("u1", "u1:main", "30d", rpm_limit=60, tpm_limit=100000, metadata={"portal": "1"})
    assert isinstance(key, GeneratedKey)
    assert (key.key, key.token) == ("sk-abc", "hash1")
    assert key.expires is not None and key.expires.year == 2026 and key.expires.tzinfo is not None
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body == {
        "user_id": "u1",
        "key_alias": "u1:main",
        "models": ["public"],  # a key made without an explicit list reaches the public group only
        "key_type": "llm_api",
        "duration": "30d",
        "rpm_limit": 60,
        "tpm_limit": 100000,
        "metadata": {"portal": "1"},
    }


async def test_generate_key_omits_duration_when_none(router, client):
    route = router.post("/key/generate").mock(
        return_value=httpx.Response(200, json={"key": "sk-abc", "token": "h", "expires": None})
    )
    key = await client.generate_key("u1", "a", None, rpm_limit=1, tpm_limit=2, metadata={})
    assert key.expires is None
    body = json.loads(route.calls.last.request.content)
    assert "duration" not in body
    assert body["models"] == ["public"] and body["key_type"] == "llm_api"


async def test_connect_error_raises_unavailable(router, client):
    router.post("/key/generate").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LiteLLMUnavailable):
        await client.generate_key("u1", "a", None, rpm_limit=1, tpm_limit=1, metadata={})


async def test_timeout_raises_unavailable(router, client):
    router.post("/key/delete").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LiteLLMUnavailable):
        await client.delete_keys(["h"])


async def test_500_raises_unavailable(router, client):
    router.get("/model/info").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(LiteLLMUnavailable):
        await client.list_managed_models()


async def test_4xx_raises_error_with_status_and_body(router, client):
    router.post("/key/generate").mock(return_value=httpx.Response(400, json={"error": {"message": "alias exists"}}))
    with pytest.raises(LiteLLMError) as exc:
        await client.generate_key("u1", "a", None, rpm_limit=1, tpm_limit=1, metadata={})
    assert exc.value.status == 400
    assert "alias exists" in exc.value.body
    assert not isinstance(exc.value, LiteLLMUnavailable)


async def test_delete_keys_posts_tokens_and_skips_empty(router, client):
    route = router.post("/key/delete").mock(return_value=httpx.Response(200, json={"deleted_keys": ["h1", "h2"]}))
    await client.delete_keys([])
    assert route.call_count == 0
    await client.delete_keys(["h1", "h2"])
    assert json.loads(route.calls.last.request.content) == {"keys": ["h1", "h2"]}


async def test_delete_keys_unknown_key_is_ok(router, client):
    router.post("/key/delete").mock(
        return_value=httpx.Response(404, json={"error": {"message": "{'error': 'No keys found'}"}})
    )
    await client.delete_keys(["gone"])


async def test_list_managed_models_filters_portal_managed(router, client):
    router.get("/model/info").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "model_name": "gemma",
                        "litellm_params": {"model": "openai/gemma", "api_base": "http://10.0.0.5:18080/v1"},
                        "model_info": {
                            "id": "pm-1",
                            "portal_managed": True,
                            "source": "k8s",
                            "node": "n1",
                            "max_input_tokens": 8192,
                        },
                    },
                    {
                        "model_name": "manual",
                        "litellm_params": {"model": "openai/x", "api_base": "http://z/v1"},
                        "model_info": {"id": "manual-1"},
                    },
                    {
                        "model_name": "other",
                        "litellm_params": {"model": "openai/x"},
                        "model_info": {"id": "o-1", "portal_managed": False},
                    },
                    {
                        "model_name": "legacy",
                        "litellm_params": {"model": "openai/legacy", "api_base": "http://10.0.0.6:18080/v1"},
                        "model_info": {"id": "pm-2", "portal_managed": True, "source": "standalone", "node": "n2"},
                    },
                ]
            },
        )
    )
    models = await client.list_managed_models()
    assert models == [
        ManagedModel("pm-1", "gemma", "http://10.0.0.5:18080/v1", "k8s", "n1", 8192),
        ManagedModel("pm-2", "legacy", "http://10.0.0.6:18080/v1", "standalone", "n2", None),
    ]


async def test_add_model_payload_matches_convention(router, client):
    route = router.post("/model/new").mock(return_value=httpx.Response(200, json={"model_id": "pm-1"}))
    await client.add_model("pm-1", "gemma", "http://10.0.0.5:18080/v1", "k8s", "n1", 8192)
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "model_name": "gemma",
        "litellm_params": {"model": "openai/gemma", "api_base": "http://10.0.0.5:18080/v1", "api_key": "none"},
        "model_info": {
            "id": "pm-1",
            "portal_managed": True,
            "source": "k8s",
            "node": "n1",
            "max_input_tokens": 8192,
            "access_groups": ["public"],
        },
    }


async def test_add_model_sends_the_backend_key_and_marks_the_entry(router):
    c = LiteLLMClient(BASE, "sk-test", backend_api_key="hex-backend-key")
    route = router.post("/model/new").mock(return_value=httpx.Response(200, json={}))
    try:
        await c.add_model("pm-1", "gemma", "http://10.0.0.5:18080/v1", "k8s", "n1", 8192)
    finally:
        await c.aclose()
    body = json.loads(route.calls.last.request.content)
    assert body["litellm_params"]["api_key"] == "hex-backend-key"
    assert body["model_info"]["backend_auth"] == key_fingerprint("hex-backend-key")
    assert "hex-backend-key" not in json.dumps(body["model_info"])  # only the fingerprint is stored in model_info


async def test_list_managed_models_reports_whether_the_entry_carries_the_key(router, client):
    def entry(model_id, **extra):
        return {
            "model_name": "m",
            "litellm_params": {"model": "openai/m", "api_base": f"http://{model_id}/v1"},
            "model_info": {"id": model_id, "portal_managed": True, "source": "k8s", "node": "n", **extra},
        }

    router.get("/model/info").mock(
        return_value=httpx.Response(200, json={"data": [entry("old"), entry("new", backend_auth="0123456789ab")]})
    )
    assert {m.id: m.key_fingerprint for m in await client.list_managed_models()} == {"old": "", "new": "0123456789ab"}


async def test_update_model_key_patches_the_entry_in_place(router):
    c = LiteLLMClient(BASE, "sk-test", backend_api_key="rotated-key")
    route = router.patch("/model/pm-1/update").mock(return_value=httpx.Response(200, json={}))
    try:
        await c.update_model_key("pm-1")
    finally:
        await c.aclose()
    assert json.loads(route.calls.last.request.content) == {
        "litellm_params": {"api_key": "rotated-key"},
        "model_info": {"backend_auth": key_fingerprint("rotated-key")},
    }
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test"


async def test_add_model_sends_max_parallel_requests(router, client):
    route = router.post("/model/new").mock(return_value=httpx.Response(200, json={}))
    await client.add_model("pm-1", "gemma", "http://h/v1", "k8s", "n1", 8192, max_parallel_requests=4)
    body = json.loads(route.calls.last.request.content)
    # litellm_params bounds the router's in-flight requests; model_info records it for the controller
    assert body["litellm_params"]["max_parallel_requests"] == 4
    assert body["model_info"]["max_parallel_requests"] == 4

    await client.add_model("pm-2", "gemma", "http://h/v1", "k8s", "n1", 8192)
    body = json.loads(route.calls.last.request.content)
    assert "max_parallel_requests" not in body["litellm_params"] and "max_parallel_requests" not in body["model_info"]


async def test_list_managed_models_reads_max_parallel_requests(router, client):
    def entry(model_id, **extra):
        return {
            "model_name": "m",
            "litellm_params": {"model": "openai/m", "api_base": f"http://{model_id}/v1"},
            "model_info": {"id": model_id, "portal_managed": True, "source": "k8s", "node": "n", **extra},
        }

    router.get("/model/info").mock(
        return_value=httpx.Response(200, json={"data": [entry("old"), entry("new", max_parallel_requests=4)]})
    )
    assert {m.id: m.max_parallel_requests for m in await client.list_managed_models()} == {"old": None, "new": 4}


async def test_add_model_without_max_input_tokens_omits_it(router, client):
    route = router.post("/model/new").mock(return_value=httpx.Response(200, json={}))
    await client.add_model("pm-1", "gemma", "http://h/v1", "standalone", "n1", None)
    assert "max_input_tokens" not in json.loads(route.calls.last.request.content)["model_info"]


async def test_delete_model_posts_id(router, client):
    route = router.post("/model/delete").mock(return_value=httpx.Response(200, json={"message": "ok"}))
    await client.delete_model("pm-1")
    assert json.loads(route.calls.last.request.content) == {"id": "pm-1"}


async def test_delete_model_404_is_ok(router, client):
    router.post("/model/delete").mock(return_value=httpx.Response(404, json={"detail": "not found"}))
    await client.delete_model("pm-1")


async def test_delete_model_missing_id_as_400_is_ok(router, client):
    # real LiteLLM v1.106.6 answers 400 "Model with id=... not found in db"
    router.post("/model/delete").mock(
        return_value=httpx.Response(400, json={"error": {"message": "{'error': 'Model with id=pm-1 not found in db'}"}})
    )
    await client.delete_model("pm-1")


async def test_delete_model_other_400_raises(router, client):
    router.post("/model/delete").mock(return_value=httpx.Response(400, json={"error": {"message": "bad request"}}))
    with pytest.raises(LiteLLMError):
        await client.delete_model("pm-1")


async def test_ensure_user_posts_without_auto_key(router, client):
    route = router.post("/user/new").mock(return_value=httpx.Response(200, json={"user_id": "u1"}))
    await client.ensure_user("u1", "u1@g.nsu.ru")
    body = json.loads(route.calls.last.request.content)
    assert body["user_id"] == "u1" and body["user_email"] == "u1@g.nsu.ru"
    assert body["auto_create_key"] is False


@pytest.mark.parametrize("status", [400, 409, 500])
async def test_ensure_user_already_exists_is_ok(router, client, status):
    router.post("/user/new").mock(
        return_value=httpx.Response(status, json={"error": {"message": "User with id u1 already exists"}})
    )
    await client.ensure_user("u1", "u1@g.nsu.ru")


async def test_ensure_user_other_error_raises(router, client):
    router.post("/user/new").mock(return_value=httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(LiteLLMError) as exc:
        await client.ensure_user("u1", "u1@g.nsu.ru")
    assert exc.value.status == 401


async def test_health_true_and_false_on_error(router, client):
    route = router.get("/health/readiness").mock(return_value=httpx.Response(200, json={"status": "healthy"}))
    assert await client.health() is True
    route.mock(return_value=httpx.Response(503, json={"detail": "db"}))
    assert await client.health() is False
    route.mock(side_effect=httpx.ConnectError("refused"))
    assert await client.health() is False


async def test_user_activity_maps_fields(router, client):
    route = router.get("/user/daily/activity").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "date": "2026-11-01",
                        "metrics": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "api_requests": 2},
                        "breakdown": {
                            "api_keys": {
                                "h1": {"metrics": {"total_tokens": 15, "api_requests": 2}, "metadata": {}},
                            }
                        },
                    },
                    {
                        "date": "2026-11-02",
                        "metrics": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "api_requests": 3},
                        "breakdown": {
                            "api_keys": {
                                "h1": {"metrics": {"total_tokens": 10, "api_requests": 1}},
                                "h2": {"metrics": {"total_tokens": 20, "api_requests": 2}},
                            }
                        },
                    },
                ],
                "metadata": {"page": 1, "total_pages": 1, "has_more": False},
            },
        )
    )
    act = await client.user_activity("u1", date(2026, 11, 1), date(2026, 11, 2))
    params = route.calls.last.request.url.params
    assert params["user_id"] == "u1"
    assert params["start_date"] == "2026-11-01" and params["end_date"] == "2026-11-02"
    assert (act.prompt_tokens, act.completion_tokens, act.requests) == (30, 15, 5)
    assert {k: (v.tokens, v.requests) for k, v in act.by_key.items()} == {"h1": (25, 3), "h2": (20, 2)}
    assert [(d.day, d.tokens, d.requests) for d in act.by_day] == [
        (date(2026, 11, 1), 15, 2),
        (date(2026, 11, 2), 30, 3),
    ]


async def test_user_activity_follows_pages(router, client):
    def page(request: httpx.Request) -> httpx.Response:
        p = int(request.url.params.get("page", "1"))
        day = f"2026-11-0{p}"
        return httpx.Response(
            200,
            json={
                "results": [{"date": day, "metrics": {"total_tokens": 1, "api_requests": 1}, "breakdown": {}}],
                "metadata": {"page": p, "total_pages": 2, "has_more": p < 2},
            },
        )

    router.get("/user/daily/activity").mock(side_effect=page)
    act = await client.user_activity("u1", date(2026, 11, 1), date(2026, 11, 2))
    assert act.requests == 2 and len(act.by_day) == 2


async def test_user_activity_empty(router, client):
    router.get("/user/daily/activity").mock(
        return_value=httpx.Response(200, json={"results": [], "metadata": {"has_more": False}})
    )
    act = await client.user_activity("u1", date(2026, 11, 1), date(2026, 11, 2))
    assert (act.prompt_tokens, act.completion_tokens, act.requests, act.by_key, act.by_day) == (0, 0, 0, {}, [])


async def test_model_group_status_counts_deployments(router, client):
    router.get("/model/info").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"model_name": "a", "model_info": {"id": "1"}},
                    {"model_name": "a", "model_info": {"id": "2"}},
                    {"model_name": "b", "model_info": {"id": "3"}},
                ]
            },
        )
    )
    assert await client.model_group_status() == {"a": 2, "b": 1}


async def test_stream_chat_yields_bytes_with_user_key(router, client):
    sse = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
    route = router.post("/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
    )
    payload = {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True}
    got = b"".join([c async for c in client.stream_chat("sk-user", payload)])
    assert got == sse
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer sk-user"
    assert json.loads(req.content) == payload


async def test_stream_chat_401_raises_error(router, client):
    router.post("/v1/chat/completions").mock(return_value=httpx.Response(401, json={"error": {"message": "bad"}}))
    with pytest.raises(LiteLLMError) as exc:
        async for _ in client.stream_chat("sk-bad", {"model": "m", "messages": []}):
            pass
    assert exc.value.status == 401


async def test_stream_chat_429_keeps_retry_after(router, client):
    router.post("/v1/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": {"message": "перегружена"}}, headers={"Retry-After": "5"})
    )
    with pytest.raises(LiteLLMError) as exc:
        async for _ in client.stream_chat("sk-user", {"model": "m", "messages": []}):
            pass
    assert exc.value.status == 429 and exc.value.retry_after == "5"
    assert LiteLLMError(400, "x").retry_after is None


async def test_stream_chat_connect_error_raises_unavailable(router, client):
    router.post("/v1/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LiteLLMUnavailable):
        async for _ in client.stream_chat("sk-user", {"model": "m", "messages": []}):
            pass


def _public_methods(cls):
    return {n for n, m in inspect.getmembers(cls, callable) if not n.startswith("_") and n != "aclose"}


def test_fake_litellm_matches_client_interface():
    real, fake = _public_methods(LiteLLMClient), _public_methods(FakeLiteLLM)
    assert real <= fake, f"fake misses {real - fake}"
    for name in real:
        r, f = getattr(LiteLLMClient, name), getattr(FakeLiteLLM, name)
        assert list(inspect.signature(r).parameters) == list(inspect.signature(f).parameters), name
        assert inspect.iscoroutinefunction(r) == inspect.iscoroutinefunction(f), name
        assert inspect.isasyncgenfunction(r) == inspect.isasyncgenfunction(f), name


async def test_fake_litellm_behaviour():
    fake = FakeLiteLLM()
    assert await fake.health() is True
    await fake.ensure_user("u1", "a@b")
    await fake.ensure_user("u1", "a@b")
    key = await fake.generate_key("u1", "u1:a", "1d", rpm_limit=1, tpm_limit=1, metadata={})
    assert key.key.startswith("sk-") and key.expires is not None
    chunks = [c async for c in fake.stream_chat(key.key, {"model": "m", "messages": []})]
    assert chunks[-1].strip().endswith(b"[DONE]")
    await fake.delete_keys([key.token])
    with pytest.raises(LiteLLMError) as exc:
        async for _ in fake.stream_chat(key.key, {"model": "m", "messages": []}):
            pass
    assert exc.value.status == 401
    await fake.add_model("pm-1", "m", "http://h/v1", "k8s", "n1", 4096)
    assert (await fake.model_group_status()) == {"m": 1}
    assert [m.id for m in await fake.list_managed_models()] == ["pm-1"]
    await fake.delete_model("pm-1")
    await fake.delete_model("pm-1")
    assert await fake.list_managed_models() == []
    fake.fail_health = True
    assert await fake.health() is False
    fake.unavailable = True
    with pytest.raises(LiteLLMUnavailable):
        await fake.list_managed_models()


@pytest.mark.parametrize("status", [400, 500])
async def test_add_model_that_already_exists_is_ok(router, client, status):
    router.post("/model/new").mock(
        return_value=httpx.Response(status, json={"error": {"message": "Unique constraint failed on the fields: (`model_id`)"}})
    )
    await client.add_model("pm-1", "gemma", "http://h/v1", "k8s", "n1", 8192)  # no error


async def test_add_model_other_error_still_raises(router, client):
    router.post("/model/new").mock(return_value=httpx.Response(500, json={"error": {"message": "boom"}}))
    with pytest.raises(LiteLLMUnavailable):
        await client.add_model("pm-1", "gemma", "http://h/v1", "k8s", "n1", 8192)


async def test_update_model_limit_patches_in_place(router, client):
    route = router.patch("/model/pm-1/update").mock(return_value=httpx.Response(200, json={}))
    await client.update_model_limit("pm-1", 3)
    assert json.loads(route.calls.last.request.content) == {
        "litellm_params": {"max_parallel_requests": 3},
        "model_info": {"max_parallel_requests": 3},
    }


class _SlowStreams:
    """A local LiteLLM stand-in whose chat streams last `seconds`; counts concurrent streams."""

    def __init__(self, seconds: float):
        import uvicorn
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, StreamingResponse
        from starlette.routing import Route

        self.in_flight = self.max_in_flight = 0

        async def chat(request):
            async def events():
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                try:
                    yield b"data: {}\n\n"
                    await asyncio.sleep(seconds)
                    yield b"data: [DONE]\n\n"
                finally:
                    self.in_flight -= 1

            return StreamingResponse(events(), media_type="text/event-stream")

        async def keys(request):
            return JSONResponse({"key": "sk-x", "token": "t", "expires": None})

        app = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]),
                                Route("/key/generate", keys, methods=["POST"])])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", backlog=2048))

    async def __aenter__(self):
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        await self.task


async def _drain(client, key="sk-u"):
    return b"".join([c async for c in client.stream_chat(key, {"model": "m", "messages": [], "stream": True})])


async def test_150_concurrent_streams_through_one_client():
    """httpx's default pool (100) would hold stream 101 and later until one ends; the stream pool holds 1000."""
    async with _SlowStreams(seconds=2.0) as srv:
        c = LiteLLMClient(srv.url, "sk-test", stream_pool_timeout=1.0)
        try:
            results = await asyncio.gather(*(_drain(c) for _ in range(150)), return_exceptions=True)
        finally:
            await c.aclose()
    assert [r for r in results if isinstance(r, BaseException)] == []
    assert srv.max_in_flight == 150


async def test_full_stream_pool_is_busy_and_admin_calls_still_work():
    async with _SlowStreams(seconds=3.0) as srv:
        c = LiteLLMClient(srv.url, "sk-test", stream_connections=2, stream_pool_timeout=0.3)
        try:
            running = [asyncio.create_task(_drain(c)) for _ in range(2)]
            while srv.in_flight < 2:
                await asyncio.sleep(0.02)
            started = asyncio.get_running_loop().time()
            with pytest.raises(LiteLLMBusy):
                await _drain(c)
            assert asyncio.get_running_loop().time() - started < 2  # after the pool timeout, not after a stream
            # admin and key calls have their own pool
            key = await c.generate_key("u", "u:a", None, rpm_limit=None, tpm_limit=None, metadata={})
            assert key.key == "sk-x"
            await asyncio.gather(*running)
        finally:
            await c.aclose()


# ---------- access groups and the system prompt (2-23) ----------


def test_access_groups_and_key_models_by_role():
    assert access_groups(False) == ["public"] and access_groups(True) == ["experimental"]
    assert models_for_role("user") == ["public"]
    assert models_for_role("admin") == ["public", "experimental"]


async def test_generate_key_sends_the_models_it_is_given(router, client):
    route = router.post("/key/generate").mock(return_value=httpx.Response(200, json={"key": "sk-a", "token": "h"}))
    await client.generate_key("u1", "a", None, None, None, {}, models=["public", "experimental"])
    assert json.loads(route.calls.last.request.content)["models"] == ["public", "experimental"]


async def test_add_model_puts_the_model_into_its_access_group_and_carries_the_prompt(router, client):
    route = router.post("/model/new").mock(return_value=httpx.Response(200, json={}))
    await client.add_model("pm-1", "m", "http://h/v1", "k8s", "n1", 8192, experimental=True, system_prompt="Будь краток")
    info = json.loads(route.calls.last.request.content)["model_info"]
    assert info["access_groups"] == ["experimental"] and info["system_prompt"] == "Будь краток"
    await client.add_model("pm-2", "m", "http://h/v1", "k8s", "n1", 8192, system_prompt="")
    info = json.loads(route.calls.last.request.content)["model_info"]
    assert info["access_groups"] == ["public"] and "system_prompt" not in info  # an empty prompt is not sent


async def test_update_model_access_patches_the_entry_in_place(router, client):
    route = router.patch("/model/pm-1/update").mock(return_value=httpx.Response(200, json={}))
    await client.update_model_access("pm-1", True, "Привет")
    assert json.loads(route.calls.last.request.content) == {
        "model_info": {"access_groups": ["experimental"], "system_prompt": "Привет"}
    }
    # a cleared prompt is an empty string: the PATCH merges, so a missing key would keep the old text
    await client.update_model_access("pm-1", False, None)
    assert json.loads(route.calls.last.request.content) == {
        "model_info": {"access_groups": ["public"], "system_prompt": ""}
    }


async def test_list_managed_models_reads_access_groups_and_prompt(router, client):
    def entry(model_id, **extra):
        return {
            "model_name": "m",
            "litellm_params": {"api_base": "http://h/v1"},
            "model_info": {"id": model_id, "portal_managed": True, "source": "k8s", "node": "n1", **extra},
        }

    router.get("/model/info").mock(
        return_value=httpx.Response(
            200,
            json={"data": [entry("a", access_groups=["experimental"], system_prompt="П"), entry("b")]},
        )
    )
    a, b = await client.list_managed_models()
    assert (a.access_groups, a.system_prompt) == (("experimental",), "П")
    assert (b.access_groups, b.system_prompt) == ((), "")  # an entry from before the feature


async def test_update_key_models_updates_each_key_by_token_and_skips_deleted_ones(router, client):
    def answer(request):
        token = json.loads(request.content)["key"]
        return httpx.Response(404 if token == "gone" else 200, json={})

    route = router.post("/key/update").mock(side_effect=answer)
    await client.update_key_models(["h1", "gone", "h2"], ["public"])
    assert [json.loads(c.request.content) for c in route.calls] == [
        {"key": "h1", "models": ["public"]},
        {"key": "gone", "models": ["public"]},
        {"key": "h2", "models": ["public"]},
    ]
    router.post("/key/update").mock(return_value=httpx.Response(500, text="x"))
    with pytest.raises(LiteLLMUnavailable):
        await client.update_key_models(["h1"], ["public"])


async def test_fake_litellm_enforces_access_groups_like_the_gateway():
    fake = FakeLiteLLM()
    await fake.add_model("pm-e", "exp", "http://h/v1", "k8s", "n1", 4096, experimental=True)
    await fake.add_model("pm-p", "pub", "http://h/v1", "k8s", "n1", 4096)
    user = await fake.generate_key("u1", "u", None, None, None, {}, models=["public"])
    admin = await fake.generate_key("u2", "a", None, None, None, {}, models=["public", "experimental"])
    payload = {"messages": []}
    assert [c async for c in fake.stream_chat(user.key, {"model": "pub", **payload})]
    assert [c async for c in fake.stream_chat(admin.key, {"model": "exp", **payload})]
    with pytest.raises(LiteLLMError) as exc:
        async for _ in fake.stream_chat(user.key, {"model": "exp", **payload}):
            pass
    assert exc.value.status == 403 and "key_model_access_denied" in exc.value.body
    await fake.update_key_models([admin.token], ["public"])  # demoted
    with pytest.raises(LiteLLMError):
        async for _ in fake.stream_chat(admin.key, {"model": "exp", **payload}):
            pass
