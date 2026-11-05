"""The admin usage read (`LiteLLMClient.global_usage`) against real LiteLLM v1.106.6 + Postgres + fake backend (Docker).

`GET /user/daily/activity` without `user_id` is the global view of the master key; the cube (day, model, user) is
built from `breakdown.model_groups[<model>].api_key_breakdown[<key>]` (see the docstring of `global_usage`).
"""

import asyncio
import time
import uuid
from datetime import date, timedelta

import pytest

from app.api.admin_usage import build
from app.litellm_client import LiteLLMClient
from tests.integration.stack import start_stack

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def stack():
    with start_stack() as s:
        yield s


async def _chat(client: LiteLLMClient, key: str, model: str) -> None:
    payload = {"model": model, "messages": [{"role": "user", "content": "привет"}], "stream": True}
    raw = b"".join([chunk async for chunk in client.stream_chat(key, payload)])
    assert raw.rstrip().endswith(b"data: [DONE]")


async def test_real_global_usage_sums_users_models_and_days(stack):
    client = LiteLLMClient(stack.url, stack.master_key)
    base = f"http://{stack.backend_alias}:{stack.backend_port}/v1"
    try:
        await client.add_model("pm-us-a", "us-model-a", base, "k8s", "n1", 4096)
        await client.add_model("pm-us-b", "us-model-b", base, "k8s", "n1", 4096)
        ids = [f"it-usage-{i}-{uuid.uuid4().hex[:6]}" for i in (1, 2)]
        for uid in ids:
            await client.ensure_user(uid, f"{uid}@g.nsu.ru")
        k1a = await client.generate_key(ids[0], f"{ids[0]}:a", None, None, None, {})
        k1b = await client.generate_key(ids[0], f"{ids[0]}:b", None, None, None, {})
        k2 = await client.generate_key(ids[1], f"{ids[1]}:a", None, None, None, {})
        # user 1: two keys, two models (3 requests); user 2: one key, one model (1 request)
        await _chat(client, k1a.key, "us-model-a")
        await _chat(client, k1b.key, "us-model-a")
        await _chat(client, k1a.key, "us-model-b")
        await _chat(client, k2.key, "us-model-a")

        today = date.today()
        first, last = today - timedelta(days=1), today + timedelta(days=1)
        wanted = {(ids[0], "us-model-a"): 2, (ids[0], "us-model-b"): 1, (ids[1], "us-model-a"): 1}
        deadline = time.monotonic() + 90
        while True:
            rows = [r for r in await client.global_usage(first, last) if r.user_id in ids]
            got = {(r.user_id, r.model): r.requests for r in rows}
            if got == wanted or time.monotonic() > deadline:
                break
            await asyncio.sleep(3)
        assert got == wanted, rows  # two keys of user 1 are one row; models are the public names, not openai/<id>
        assert all(r.prompt_tokens > 0 and r.completion_tokens > 0 for r in rows)
        assert {r.user_email for r in rows} == {f"{ids[0]}@g.nsu.ru", f"{ids[1]}@g.nsu.ru"}
        assert {r.day for r in rows} <= {today - timedelta(days=1), today, today + timedelta(days=1)}

        # the same rows through the report the admin page builds
        names = {ids[0]: ("Первый", f"{ids[0]}@g.nsu.ru"), ids[1]: ("Второй", f"{ids[1]}@g.nsu.ru")}
        out = build(rows, names, first, last, "model")
        assert out["totals"]["requests"] == 4
        assert out["totals"]["tokens"] == sum(r.prompt_tokens + r.completion_tokens for r in rows)
        assert [(i["key"], i["requests"]) for i in out["top_models"]] == [("us-model-a", 3), ("us-model-b", 1)]
        assert [(i["label"], i["requests"]) for i in out["top_users"]] == [("Первый", 3), ("Второй", 1)]
        assert sum(d["requests"] for d in out["by_day"]) == 4

        # a period without traffic is empty
        assert [r for r in await client.global_usage(date(2020, 1, 1), date(2020, 1, 2))] == []
        await client.delete_keys([k1a.token, k1b.token, k2.token])
    finally:
        await client.delete_model("pm-us-a")
        await client.delete_model("pm-us-b")
        await client.aclose()
