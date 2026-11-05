"""Playground request schema: text or content parts with images, limits, and the vision check."""

import base64

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete

from app.api import playground
from app.api.playground import MAX_IMAGE_BYTES, MAX_IMAGES, ChatRequest
from app.models import ModelDeployment, NodeAssignment
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

HEADS = {
    "png": bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
    "jpeg": bytes([0xFF, 0xD8, 0xFF, 0xE0]),
    "webp": b"RIFF" + bytes(4) + b"WEBPVP8 ",
}


def encoded(mime: str, size: int = 64) -> str:
    """base64 of `size` bytes that start like a real file of the type"""
    return base64.b64encode(HEADS[mime] + b"0" * (size - len(HEADS[mime]))).decode()


def image(mime: str = "png", data: str | None = None) -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{data or encoded(mime)}"}}


def request(*content, model: str = "vision-m") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": list(content)}]}


# -- schema (no database) ---------------------------------------------------------------------------


def test_content_is_a_string_or_a_list_of_parts():
    assert ChatRequest.model_validate({"model": "m", "messages": [{"role": "user", "content": "Привет"}]})
    parsed = ChatRequest.model_validate(request({"type": "text", "text": "Что тут?"}, image("jpeg"), image("webp")))
    assert [p.type for p in parsed.messages[0].content] == ["text", "image_url", "image_url"]
    assert parsed.messages[0].has_images


def test_a_part_of_an_unknown_type_is_refused():
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(request({"type": "video_url", "video_url": {"url": "x"}}))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a.png",  # not a data URL: the model server would be sent to fetch it
        "data:image/gif;base64,R0lGODlh",
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:text/html;base64,PGI+",
        "data:image/png;base64,не-base64",
        "data:image/png;base64,QUJD\nQUJD",
    ],
)
def test_only_png_jpeg_webp_data_urls_are_accepted(url):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(request({"type": "image_url", "image_url": {"url": url}}))


@pytest.mark.parametrize("declared,actual", [("png", "jpeg"), ("jpeg", "webp"), ("webp", "png")])
def test_the_bytes_must_match_the_declared_type(declared, actual):
    with pytest.raises(ValidationError, match="не совпадает с его типом"):
        ChatRequest.model_validate(request(image(declared, encoded(actual))))
    with pytest.raises(ValidationError, match="не совпадает с его типом"):  # plain text under an image label
        ChatRequest.model_validate(request(image("png", base64.b64encode(b"just some text, not a picture").decode())))


def test_every_supported_type_with_its_own_signature_passes():
    for mime in ("png", "jpeg", "webp"):
        ChatRequest.model_validate(request(image(mime)))


def test_up_to_four_images_per_message():
    ChatRequest.model_validate(request(*[image() for _ in range(MAX_IMAGES)]))
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(request(*[image() for _ in range(MAX_IMAGES + 1)]))


def test_an_image_is_at_most_5_mb_decoded():
    ok = encoded("png", MAX_IMAGE_BYTES)
    ChatRequest.model_validate(request(image(data=ok)))
    too_big = encoded("png", MAX_IMAGE_BYTES + 1)
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(request(image(data=too_big)))


def test_images_only_in_user_messages_and_a_list_is_not_empty():
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"model": "m", "messages": [{"role": "assistant", "content": [image()]}]})
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"model": "m", "messages": [{"role": "user", "content": []}]})


def test_a_message_takes_five_text_files_of_512_kb():
    """What the web client allows: 5 x 512 KB of text plus the typed question in one message."""
    text = "я" * (5 * 512 * 1024 + 10_000)
    assert len(text) <= playground.MAX_CONTENT_CHARS
    ChatRequest.model_validate({"model": "m", "messages": [{"role": "user", "content": text}]})
    ChatRequest.model_validate(request({"type": "text", "text": text}))


def test_text_limit_still_applies_to_a_string_and_to_a_text_part():
    long = "я" * (playground.MAX_CONTENT_CHARS + 1)
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"model": "m", "messages": [{"role": "user", "content": long}]})
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(request({"type": "text", "text": long}))


def test_all_the_turns_together_stay_under_20_mb():
    """Every earlier turn is sent again with its pictures: the sum of the whole request is bounded, not only one image."""
    big = encoded("png", MAX_IMAGE_BYTES)  # about 7 MB as text
    turns = [{"role": "user", "content": [image(data=big)]} for _ in range(3)]
    ChatRequest.model_validate({"model": "m", "messages": turns[:2]})
    with pytest.raises(ValidationError, match="20 МБ"):
        ChatRequest.model_validate({"model": "m", "messages": turns})


# -- the endpoint (database) ------------------------------------------------------------------------


@pytest_asyncio.fixture
async def env(app):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(NodeAssignment))
        await db.execute(delete(ModelDeployment))
        db.add_all(
            [
                ModelDeployment(
                    name="vision-m", repo="o/r", file="f.gguf", ctx_per_request=4096, parallel=1, mmproj_file="mmproj-F16.gguf"
                ),
                ModelDeployment(name="text-m", repo="o/r", file="f.gguf", ctx_per_request=4096, parallel=1),
            ]
        )
        await db.commit()
    app.state.litellm = FakeLiteLLM(reply="Это кот")
    return app.state.litellm


async def chat(client, app, payload):
    me = await login(client, app)
    return await client.post("/api/playground/chat", json=payload, headers=csrf_headers(me))


@pytest.mark.docker
async def test_images_reach_a_vision_model_as_content_parts(client, app, env):
    parts = [{"type": "text", "text": "Что тут?"}, image("jpeg")]
    r = await chat(client, app, request(*parts))
    assert r.status_code == 200, r.text
    ((_, payload),) = env.chat_calls
    assert payload["messages"] == [{"role": "user", "content": parts}]


@pytest.mark.docker
async def test_images_to_a_model_without_vision_are_422(client, app, env):
    r = await chat(client, app, request(image(), model="text-m"))
    assert r.status_code == 422
    assert r.json() == {"detail": "Модель не принимает изображения"}
    assert not env.chat_calls


@pytest.mark.docker
async def test_images_to_an_unknown_model_are_422(client, app, env):
    assert (await chat(client, app, request(image(), model="ghost"))).status_code == 422


@pytest.mark.docker
async def test_text_parts_without_images_go_to_any_model(client, app, env):
    r = await chat(client, app, request({"type": "text", "text": "Привет"}, model="text-m"))
    assert r.status_code == 200, r.text


@pytest.mark.docker
async def test_a_plain_string_message_is_unchanged(client, app, env):
    r = await chat(client, app, {"model": "text-m", "messages": [{"role": "user", "content": "Привет"}]})
    assert r.status_code == 200
    assert env.chat_calls[0][1]["messages"] == [{"role": "user", "content": "Привет"}]


@pytest.mark.docker
async def test_a_600_kb_text_message_is_accepted(client, app, env):
    text = "Файл: a.txt" + chr(10) + chr(96) * 3 + chr(10) + "я" * 600_000 + chr(10) + chr(96) * 3
    r = await chat(client, app, {"model": "text-m", "messages": [{"role": "user", "content": text}]})
    assert r.status_code == 200, r.text
    assert env.chat_calls[0][1]["messages"][0]["content"] == text


@pytest.mark.docker
async def test_a_message_over_the_text_limit_is_422_without_echoing_it(client, app, env):
    text = "x" * (playground.MAX_CONTENT_CHARS + 1)
    r = await chat(client, app, {"model": "text-m", "messages": [{"role": "user", "content": text}]})
    assert r.status_code == 422
    assert "x" * 100 not in r.text
    assert not env.chat_calls


@pytest.mark.docker
async def test_a_body_over_20_mb_is_413_by_content_length(client, app, env):
    me = await login(client, app)
    headers = {**csrf_headers(me), "Content-Type": "application/json"}
    body = b'{"model": "vision-m", "messages": [{"role": "user", "content": "' + b"a" * playground.MAX_BODY_BYTES + b'"}]}'
    r = await client.post("/api/playground/chat", content=body, headers=headers)
    assert r.status_code == 413
    assert r.json() == {"detail": playground.TOO_LARGE}
    assert not env.chat_calls


@pytest.mark.docker
async def test_a_body_without_a_declared_length_is_cut_at_the_limit(client, app, env):
    me = await login(client, app)
    headers = {**csrf_headers(me), "Content-Type": "application/json"}
    chunk = b"a" * (1024 * 1024)

    async def stream():
        yield b'{"model": "text-m", "messages": [{"role": "user", "content": "'
        for _ in range(21):
            yield chunk
        yield b'"}]}'

    r = await client.post("/api/playground/chat", content=stream(), headers=headers)  # chunked: no Content-Length
    assert r.status_code == 413
    assert r.json() == {"detail": playground.TOO_LARGE}


@pytest.mark.docker
async def test_nobody_is_signed_in_means_the_body_is_not_read(client, app, env, monkeypatch):
    """The session and CSRF checks come first: a caller without a session costs the portal no buffering."""
    read = []
    original = playground._read_chat_request

    async def spy(*args, **kwargs):
        read.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(playground, "_read_chat_request", spy)
    body = b'{"model": "text-m", "messages": [{"role": "user", "content": "' + b"a" * 2_000_000 + b'"}]}'
    r = await client.post("/api/playground/chat", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code in (401, 403)
    me = await login(client, app)
    r = await client.post("/api/playground/chat", content=body, headers={"Content-Type": "application/json"})  # no CSRF
    assert r.status_code == 403
    assert read == []
    assert me


@pytest.mark.docker
async def test_malformed_json_is_422(client, app, env):
    me = await login(client, app)
    r = await client.post(
        "/api/playground/chat", content=b"{not json", headers={**csrf_headers(me), "Content-Type": "application/json"}
    )
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


@pytest.mark.docker
async def test_an_image_with_the_wrong_signature_is_422(client, app, env):
    r = await chat(client, app, request(image("png", encoded("jpeg"))))
    assert r.status_code == 422
    assert "не совпадает с его типом" in r.text
    assert not env.chat_calls
