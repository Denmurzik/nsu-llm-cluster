"""The «Проверить» probe: POST /api/admin/deployments/{id}/probe."""

import asyncio
import json

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.api import admin_deployments
from app.litellm_client import LiteLLMBusy, LiteLLMError
from app.models import ModelDeployment, NodeAssignment
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru"}


def chunk(content: str | None = None, usage: dict | None = None) -> bytes:
    event: dict = {"choices": [{"index": 0, "delta": {"content": content} if content is not None else {}}]}
    if usage is not None:
        event["usage"] = usage
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


@pytest_asyncio.fixture
async def dep_ids(app):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(NodeAssignment))
        await db.execute(delete(ModelDeployment))
        on = ModelDeployment(name="probe-on", repo="o/r", file="f.gguf", ctx_per_request=4096, parallel=1)
        off = ModelDeployment(name="probe-off", repo="o/r", file="f.gguf", ctx_per_request=4096, parallel=1, enabled=False)
        db.add_all([on, off])
        await db.commit()
        return {"on": on.id, "off": off.id}


@pytest_asyncio.fixture
async def fake(app):
    app.state.litellm = FakeLiteLLM()
    return app.state.litellm


@pytest_asyncio.fixture
async def admin(client, app, fake):
    return csrf_headers(await login(client, app, ADMIN))


def stream_of(*parts: bytes, delay: float = 0.0):
    async def stream_chat(self, api_key, payload):
        self.chat_calls.append((api_key, payload))
        for part in parts:
            if delay:
                await asyncio.sleep(delay)
            yield part
        yield b"data: [DONE]\n\n"

    return stream_chat


async def probe(client, headers, dep_id):
    return await client.post(f"/api/admin/deployments/{dep_id}/probe", headers=headers)


async def test_probe_measures_ttft_and_speed_and_sends_the_fixed_question(client, fake, admin, dep_ids, monkeypatch):
    monkeypatch.setattr(
        FakeLiteLLM, "stream_chat", stream_of(chunk("Мос"), chunk("ква"), chunk(None, {"completion_tokens": 2}), delay=0.05)
    )
    r = await probe(client, admin, dep_ids["on"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["status"] == 200 and body["error"] is None
    assert body["answer"] == "Москва"
    assert body["ttft_ms"] >= 40
    assert body["tokens_per_s"] > 0
    ((key, payload),) = fake.chat_calls
    assert payload == {
        "model": "probe-on",
        "messages": [{"role": "user", "content": "Ответь одним словом: столица России?"}],
        "max_tokens": 16,
        "stream": True,
    }
    assert key in {k["key"] for k in fake.keys.values()}  # the admin's Playground key


async def test_probe_counts_chunks_when_the_stream_has_no_usage(client, fake, admin, dep_ids, monkeypatch):
    monkeypatch.setattr(FakeLiteLLM, "stream_chat", stream_of(chunk("a"), chunk("b"), chunk("c"), delay=0.05))
    body = (await probe(client, admin, dep_ids["on"])).json()
    assert body["ok"] is True and body["answer"] == "abc"
    # 3 chunks over about 0.1 s after the first one
    assert 5 < body["tokens_per_s"] < 60


async def test_probe_answer_is_cut_to_200_characters(client, fake, admin, dep_ids, monkeypatch):
    monkeypatch.setattr(FakeLiteLLM, "stream_chat", stream_of(chunk("я" * 500)))
    body = (await probe(client, admin, dep_ids["on"])).json()
    assert len(body["answer"]) == 200


async def test_probe_line_split_across_network_chunks_is_reassembled(client, fake, admin, dep_ids, monkeypatch):
    whole = chunk("Москва")
    monkeypatch.setattr(FakeLiteLLM, "stream_chat", stream_of(whole[:17], whole[17:]))
    body = (await probe(client, admin, dep_ids["on"])).json()
    assert body["ok"] is True and body["answer"] == "Москва"


async def test_probe_times_out(client, fake, admin, dep_ids, monkeypatch):
    monkeypatch.setattr(admin_deployments, "PROBE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(FakeLiteLLM, "stream_chat", stream_of(chunk("x"), delay=5))
    body = (await probe(client, admin, dep_ids["on"])).json()
    assert body["ok"] is False and body["status"] == 0
    assert "Нет ответа" in body["error"]
    assert body["ttft_ms"] is None and body["tokens_per_s"] is None and body["answer"] == ""


async def test_probe_of_a_disabled_deployment_is_409(client, fake, admin, dep_ids):
    r = await probe(client, admin, dep_ids["off"])
    assert r.status_code == 409
    assert r.json() == {"detail": "Развёртывание выключено"}
    assert not fake.chat_calls


async def test_probe_of_an_unknown_deployment_is_404(client, fake, admin, dep_ids):
    assert (await probe(client, admin, 999_999)).status_code == 404


async def test_probe_is_for_admins(client, app, fake, dep_ids):
    h = csrf_headers(await login(client, app, USER))
    assert (await probe(client, h, dep_ids["on"])).status_code == 403
    assert not fake.chat_calls


async def test_probe_requires_csrf(client, fake, admin, dep_ids):
    assert (await client.post(f"/api/admin/deployments/{dep_ids['on']}/probe")).status_code == 403
    assert not fake.chat_calls


async def test_probe_upstream_error_is_a_result_not_an_http_error(client, fake, admin, dep_ids, monkeypatch):
    async def refuse(self, api_key, payload):
        raise LiteLLMError(400, json.dumps({"error": {"message": "context length exceeded"}}))
        yield b""

    monkeypatch.setattr(FakeLiteLLM, "stream_chat", refuse)
    r = await probe(client, admin, dep_ids["on"])
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["status"] == 400
    assert body["error"] == "Модель вернула ошибку (context length exceeded)"


async def test_probe_gateway_down_and_busy(client, fake, admin, dep_ids, monkeypatch):
    fake.unavailable = True
    body = (await probe(client, admin, dep_ids["on"])).json()
    assert body["ok"] is False and body["status"] == 503 and body["error"] == "Шлюз моделей недоступен"
    fake.unavailable = False

    async def busy(self, api_key, payload):
        raise LiteLLMBusy("pool")
        yield b""

    monkeypatch.setattr(FakeLiteLLM, "stream_chat", busy)
    body = (await probe(client, admin, dep_ids["on"])).json()
    assert body["ok"] is False and body["status"] == 429


async def test_probe_error_event_in_the_stream(client, fake, admin, dep_ids, monkeypatch):
    err = f"data: {json.dumps({'error': {'message': 'модель упала'}}, ensure_ascii=False)}\n\n".encode()
    monkeypatch.setattr(FakeLiteLLM, "stream_chat", stream_of(err))
    body = (await probe(client, admin, dep_ids["on"])).json()
    assert body["ok"] is False and body["error"] == "модель упала"


async def test_probe_empty_answer_is_not_ok(client, fake, admin, dep_ids, monkeypatch):
    monkeypatch.setattr(FakeLiteLLM, "stream_chat", stream_of(chunk(None)))
    body = (await probe(client, admin, dep_ids["on"])).json()
    assert body["ok"] is False and body["error"] == "Модель вернула пустой ответ"


async def test_probe_reasoning_model_that_spent_its_16_tokens_thinking_is_alive(client, fake, admin, dep_ids, monkeypatch):
    def thought(text):
        return (f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'reasoning_content': text}}]})}" + chr(10) * 2).encode()

    monkeypatch.setattr(FakeLiteLLM, "stream_chat", stream_of(thought("Надо"), thought(" подумать"), delay=0.05))
    body = (await probe(client, admin, dep_ids["on"])).json()
    assert body["ok"] is True and body["error"] is None and body["answer"] == ""
    assert body["ttft_ms"] is not None and body["tokens_per_s"] is not None
    assert "thinking" not in body


async def test_probe_retries_once_on_a_revoked_playground_key(client, fake, admin, dep_ids):
    assert (await probe(client, admin, dep_ids["on"])).json()["ok"] is True
    fake.keys.clear()  # revoked behind the portal's back
    body = (await probe(client, admin, dep_ids["on"])).json()
    assert body["ok"] is True
    assert len(fake.keys) == 1
