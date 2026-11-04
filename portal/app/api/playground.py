"""SSE chat playground.

Each user gets one hidden LiteLLM key (`is_playground`). Its raw value lives only in Redis
(`pg:<user id>`, 12 h), shared by all API workers; the database keeps the token hash and the mask.
On a cache miss (expired, Redis restarted) or when LiteLLM answers 401, the old playground key is
deleted and a new one is issued, under a per-user Redis lock so two workers never issue at once.
"""

import asyncio
import base64
import binascii
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.keys import gateway_down, gateway_error, litellm_alias, mask_key
from app.auth.sessions import current_user, require_csrf
from app.config import Settings
from app.db import get_session
from app.deps import REDIS_ERRORS, get_litellm, get_settings_dep
from app.litellm_client import LiteLLMBusy, LiteLLMClient, LiteLLMError, LiteLLMUnavailable, models_for_role
from app.models import ApiKeyRef, ModelDeployment, User

log = logging.getLogger(__name__)

router = APIRouter()

PLAYGROUND_ALIAS = "__playground"
MAX_TOKENS_LIMIT = 4096
KEY_TTL_S = 12 * 3600
LOCK_TTL_MS = 30_000  # a worker that dies holding the lock frees it after this
LOCK_WAIT_S = 30.0
BUSY_RETRY_AFTER_S = 5
ADMINS_ONLY = "Модель доступна только администраторам"
MODEL_DENIED = "Эта модель вам недоступна"
BUSY = "Слишком много одновременных запросов к шлюзу. Повторите запрос через 5 с."
# deletes the lock only while it still holds this holder's token (one atomic step)
_RELEASE_LOCK = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""
_CYRILLIC = re.compile("[А-Яа-яЁё]")


MAX_MESSAGES = 100
# One message: five text files of 512 KB (what the web client allows) and the typed text. The whole request is bounded
# by MAX_BODY_BYTES below (and by Caddy at 24 MB on this route), not by this.
MAX_CONTENT_CHARS = 2_700_000


MAX_IMAGES = 4  # per message
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # each image once decoded
MAX_BODY_BYTES = 20 * 1024 * 1024  # the whole request; Caddy lets this route through up to 24 MB (main/Caddyfile.tmpl)
NO_VISION = "Модель не принимает изображения"
TOO_LARGE = "Запрос слишком большой: изображения и файлы вместе не больше 20 МБ"
_DATA_URL = re.compile(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]*={0,2})")


class TextPart(BaseModel):
    type: Literal["text"]
    text: str = Field(max_length=MAX_CONTENT_CHARS)


_PNG_SIGNATURE = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
_JPEG_SIGNATURE = bytes([0xFF, 0xD8, 0xFF])


def _signature_matches(kind: str, head: bytes) -> bool:
    """The first bytes of the file agree with the declared type: a label alone is not trusted."""
    if kind == "png":
        return head.startswith(_PNG_SIGNATURE)
    if kind == "jpeg":
        return head.startswith(_JPEG_SIGNATURE)
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


class ImageUrl(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def _data_url(cls, value: str) -> str:
        match = _DATA_URL.fullmatch(value)
        if match is None:
            raise ValueError("Изображение должно быть data-URL в формате png, jpeg или webp")
        try:
            decoded = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("Изображение повреждено: base64 не читается") from None
        if len(decoded) > MAX_IMAGE_BYTES:
            raise ValueError("Изображение больше 5 МБ")
        if not _signature_matches(match.group(1), decoded[:12]):
            raise ValueError("Содержимое изображения не совпадает с его типом")
        return value


class ImagePart(BaseModel):
    type: Literal["image_url"]
    image_url: ImageUrl


ContentPart = Annotated[TextPart | ImagePart, Field(discriminator="type")]


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, Field(max_length=MAX_CONTENT_CHARS)] | list[ContentPart]

    @model_validator(mode="after")
    def _limits(self) -> "ChatMessage":
        if isinstance(self.content, str):
            return self
        if not self.content:
            raise ValueError("В сообщении нет содержимого")
        if self.role != "user" and any(p.type == "image_url" for p in self.content):
            raise ValueError("Изображения допустимы только в сообщении пользователя")
        if sum(p.type == "image_url" for p in self.content) > MAX_IMAGES:
            raise ValueError(f"В сообщении не больше {MAX_IMAGES} изображений")
        return self

    @property
    def has_images(self) -> bool:
        return not isinstance(self.content, str) and any(p.type == "image_url" for p in self.content)


class ChatRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    max_tokens: int | None = Field(default=None, ge=1, le=MAX_TOKENS_LIMIT)

    @model_validator(mode="after")
    def _total_size(self) -> "ChatRequest":
        """The request as a whole: the earlier turns carry their images again, so the sum is bounded too."""
        total = 0
        for message in self.messages:
            if isinstance(message.content, str):
                total += len(message.content)
                continue
            for part in message.content:
                total += len(part.text) if part.type == "text" else len(part.image_url.url)
        if total > MAX_BODY_BYTES:
            raise ValueError(TOO_LARGE)
        return self


async def _read_chat_request(request: Request) -> ChatRequest:
    """Reads and validates the body. Called after the session and CSRF dependencies, so a caller who is not signed in
    never makes the portal buffer 24 MB; the read stops at the limit instead of taking what the client sends."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=TOO_LARGE)
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail=TOO_LARGE)
    try:
        return ChatRequest.model_validate_json(bytes(raw))
    except ValidationError as exc:
        errors = [{**e, "loc": ("body", *e["loc"])} for e in exc.errors(include_url=False)]
        raise RequestValidationError(errors) from None


async def _check_vision(db: AsyncSession, body: ChatRequest) -> None:
    """Images only for a model whose deployment has a vision projector (`mmproj_file`)."""
    if not any(m.has_images for m in body.messages):
        return
    mmproj = await db.scalar(select(ModelDeployment.mmproj_file).where(ModelDeployment.name == body.model))
    if not mmproj:
        raise HTTPException(status_code=422, detail=NO_VISION)


def _cache_key(user_id: str) -> str:
    return f"pg:{user_id}"


@asynccontextmanager
async def _user_lock(redis: Any, user_id: str) -> AsyncIterator[None]:
    """Cross-process lock: only one API worker issues a user's playground key at a time."""
    name, token = f"pg-lock:{user_id}", secrets.token_hex(16)
    deadline = time.monotonic() + LOCK_WAIT_S
    while not await redis.set(name, token, nx=True, px=LOCK_TTL_MS):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"playground key lock of {user_id} is busy")
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        await redis.eval(_RELEASE_LOCK, 1, name, token)


async def _playground_key(
    request: Request,
    user: User,
    db: AsyncSession,
    litellm: LiteLLMClient,
    settings: Settings,
    stale: str | None = None,
) -> str:
    """Cached playground key; issues a new one on a miss or when `stale` is the cached (rejected) key."""
    redis = request.app.state.redis
    uid = str(user.id)
    cached = await redis.get(_cache_key(uid))
    if cached is not None and cached != stale:
        return cached
    async with _user_lock(redis, uid):
        cached = await redis.get(_cache_key(uid))  # another worker may have issued it meanwhile
        if cached is not None and cached != stale:
            return cached
        old = (
            await db.scalars(select(ApiKeyRef).where(ApiKeyRef.user_id == user.id, ApiKeyRef.is_playground.is_(True)))
        ).all()
        # ends the SELECT's transaction: none stays open ("idle in transaction") across the LiteLLM calls below
        await db.commit()
        if old:
            await litellm.delete_keys([ref.litellm_token for ref in old])
            for ref in old:
                await db.delete(ref)
            await db.commit()
        await litellm.ensure_user(uid, user.email)
        generated = await litellm.generate_key(
            user_id=uid,
            alias=litellm_alias(user, PLAYGROUND_ALIAS),
            duration=None,
            rpm_limit=settings.key_rpm_limit,
            tpm_limit=settings.key_tpm_limit,
            metadata={"portal": True, "user_email": user.email, "playground": True},
            models=models_for_role(user.role),
        )
        db.add(
            ApiKeyRef(
                user_id=user.id,
                litellm_token=generated.token,
                alias=PLAYGROUND_ALIAS,
                masked=mask_key(generated.key),
                created_at=datetime.now(UTC),
                expires_at=None,
                is_playground=True,
            )
        )
        await db.commit()
        await redis.set(_cache_key(uid), generated.key, ex=KEY_TTL_S)
        return generated.key


def _model_denied(exc: LiteLLMError) -> bool:
    """LiteLLM refuses a key the model's access group is not in (v1.106.6: 403 of type `key_model_access_denied`)."""
    body = exc.body.lower()
    return exc.status in (401, 403) and ("key_model_access_denied" in body or "not allowed to access model" in body)


def _upstream_error(exc: LiteLLMError) -> HTTPException:
    if _model_denied(exc):
        return HTTPException(status_code=403, detail=MODEL_DENIED)
    try:
        message = json.loads(exc.body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = exc.body[:300]
    if exc.status == 429:
        # an overloaded model (LiteLLM's 429 already in Russian, see main/litellm/overload_hook.py) or a key limit
        original = str(message).strip()
        if _CYRILLIC.search(original):
            detail = original
        else:
            detail = "Слишком много запросов, повторите позже" + (f" ({original})" if original else "")
        headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
        return HTTPException(status_code=429, detail=detail, headers=headers)
    if 400 <= exc.status < 500 and exc.status != 401:
        original = str(message).strip()
        # the model's own wording is usually English: a Russian sentence first, the original in parentheses
        detail = f"Модель вернула ошибку ({original})" if original else "Модель вернула ошибку"
        return HTTPException(status_code=exc.status, detail=detail)
    log.warning("LiteLLM refused a playground request: %s", exc)
    return gateway_error()


async def _open_stream(
    key: str, payload: dict[str, Any], litellm: LiteLLMClient
) -> tuple[AsyncIterator[bytes], bytes]:
    """Start the upstream stream and read its first chunk, so errors surface before the response starts."""
    stream = litellm.stream_chat(key, payload)
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        first = b""
    except BaseException:
        await stream.aclose()
        raise
    return stream, first


async def _relay(stream: AsyncIterator[bytes], first: bytes) -> AsyncIterator[bytes]:
    try:
        if first:
            yield first
        async for chunk in stream:
            yield chunk
    except LiteLLMUnavailable:
        log.warning("LiteLLM stream interrupted", exc_info=True)
        error = {"error": {"message": "Шлюз моделей недоступен"}}
        yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n".encode()
    finally:
        await stream.aclose()


@router.post("/api/playground/chat", dependencies=[Depends(require_csrf)])
async def chat(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
    litellm: LiteLLMClient = Depends(get_litellm),
    settings: Settings = Depends(get_settings_dep),
) -> StreamingResponse:
    body = await _read_chat_request(request)
    if user.role != "admin" and await db.scalar(
        select(ModelDeployment.experimental).where(ModelDeployment.name == body.model)
    ):
        raise HTTPException(status_code=403, detail=ADMINS_ONLY)
    await _check_vision(db, body)
    payload: dict[str, Any] = {
        "model": body.model,
        "messages": [m.model_dump() for m in body.messages],
        "stream": True,
    }
    if body.max_tokens is not None:
        payload["max_tokens"] = body.max_tokens

    stale: str | None = None
    try:
        for attempt in (1, 2):
            key = await _playground_key(request, user, db, litellm, settings, stale)
            try:
                stream, first = await _open_stream(key, payload, litellm)
                break
            except LiteLLMError as exc:
                if exc.status == 401 and attempt == 1 and not _model_denied(exc):
                    stale = key
                    continue
                raise
    except LiteLLMUnavailable:
        raise gateway_down() from None
    except LiteLLMBusy:
        log.warning("playground: every connection to LiteLLM is in use")
        raise HTTPException(
            status_code=429, detail=BUSY, headers={"Retry-After": str(BUSY_RETRY_AFTER_S)}
        ) from None
    except REDIS_ERRORS as exc:
        log.warning("playground: Redis unavailable: %s", exc)
        raise gateway_down() from None
    except LiteLLMError as exc:
        raise _upstream_error(exc) from None
    finally:
        # free the pooled connection before the (long) stream starts
        await db.rollback()

    return StreamingResponse(
        _relay(stream, first),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
