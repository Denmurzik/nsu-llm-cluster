import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, StringConstraints, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.sessions import current_user, require_csrf
from app.config import Settings
from app.db import get_session
from app.deps import REDIS_ERRORS, get_litellm, get_settings_dep
from app.litellm_client import LiteLLMClient, LiteLLMError, LiteLLMUnavailable, models_for_role
from app.models import ApiKeyRef, User
from app.user_limits import key_limits, max_keys

log = logging.getLogger(__name__)

router = APIRouter()

GATEWAY_DOWN = "Шлюз моделей недоступен"
GATEWAY_ERROR = "Шлюз моделей вернул ошибку"
DUPLICATE_ALIAS = "Ключ с таким названием уже есть"
CREATE_RATE_PREFIX = "key-create:"  # Redis: keys a user created in the current hour (INCR, expires with the hour)
CREATE_WINDOW_S = 3600


def gateway_down() -> HTTPException:
    return HTTPException(status_code=503, detail=GATEWAY_DOWN)


def gateway_error() -> HTTPException:
    return HTTPException(status_code=502, detail=GATEWAY_ERROR)


async def _creation_allowed(request: Request, user: User, limit: int) -> int | None:
    """Reserves one creation in the user's hourly window (INCR, so parallel requests cannot all pass): None when allowed,
    else the seconds until the window ends. A creation that then fails gives its place back (`_uncount_creation`), so a
    refusal of LiteLLM spends nothing. A Redis that does not answer lets it through: the key count and the per-user limits
    still hold."""
    redis = request.app.state.redis
    name = CREATE_RATE_PREFIX + str(user.id)
    try:
        count = await redis.incr(name)
        if count == 1:
            await redis.expire(name, CREATE_WINDOW_S)
        if count <= limit:
            return None
        await redis.decr(name)  # refused: this attempt takes no place
        ttl = await redis.ttl(name)
    except REDIS_ERRORS as exc:
        log.warning("key creation rate of user %s not checked, Redis is unavailable: %s", user.id, exc)
        return None
    if ttl is None or ttl < 0:  # the expiry was lost (a crash between the two calls): the window starts again
        try:
            await redis.expire(name, CREATE_WINDOW_S)
        except REDIS_ERRORS:
            pass
        ttl = CREATE_WINDOW_S
    return max(int(ttl), 1)


async def _uncount_creation(request: Request, user: User) -> None:
    """A reserved creation failed: its place in the hourly window is given back."""
    try:
        await request.app.state.redis.decr(CREATE_RATE_PREFIX + str(user.id))
    except REDIS_ERRORS as exc:
        log.warning("key creation of user %s not given back, Redis is unavailable: %s", user.id, exc)


def mask_key(key: str) -> str:
    return key[:3] + "…" + key[-4:]


def litellm_alias(user: User, alias: str) -> str:
    return f"{str(user.id)[:8]}-{alias}"


Alias = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40, pattern=r"^[\w \-]+$")]


class KeyCreate(BaseModel):
    alias: Alias
    expires_days: Literal[30, 90] | None = None

    @field_validator("alias")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value.startswith("__"):
            raise ValueError("Название не может начинаться с «__»")
        return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@router.post("/api/keys", status_code=201, dependencies=[Depends(require_csrf)])
async def create_key(
    body: KeyCreate,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
    litellm: LiteLLMClient = Depends(get_litellm),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    # serialize concurrent creates of one user so the limit cannot be overshot
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    regular = ApiKeyRef.is_playground.is_(False)
    count = await db.scalar(select(func.count()).select_from(ApiKeyRef).where(ApiKeyRef.user_id == user.id, regular))
    limit = await max_keys(db, user, settings)
    if count >= limit:
        raise HTTPException(status_code=409, detail=f"Достигнут лимит ключей: {limit}")
    taken = await db.scalar(
        select(ApiKeyRef.id).where(ApiKeyRef.user_id == user.id, regular, ApiKeyRef.alias == body.alias)
    )
    if taken is not None:
        raise HTTPException(status_code=409, detail=DUPLICATE_ALIAS)

    wait = await _creation_allowed(request, user, settings.key_create_per_hour)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много новых ключей: не больше {settings.key_create_per_hour} в час. Попробуйте позже.",
            headers={"Retry-After": str(wait)},
        )

    try:
        return await _create(request, user, body, db, litellm, settings)
    except BaseException:
        await _uncount_creation(request, user)
        raise


async def _create(request: Request, user: User, body, db: AsyncSession, litellm, settings) -> dict:
    duration = f"{body.expires_days}d" if body.expires_days else None
    rpm_limit, tpm_limit = await key_limits(db, user, settings)
    try:
        # the same limits for the user as a whole: all its keys and the Playground share them (app.user_limits)
        await litellm.ensure_user(str(user.id), user.email, rpm_limit, tpm_limit)
        generated = await litellm.generate_key(
            user_id=str(user.id),
            alias=litellm_alias(user, body.alias),
            duration=duration,
            rpm_limit=rpm_limit,
            tpm_limit=tpm_limit,
            metadata={"portal": True, "user_email": user.email},
            models=models_for_role(user.role),
        )
    except LiteLLMUnavailable:
        raise gateway_down() from None
    except LiteLLMError as exc:
        if exc.status == 400 and "already exists" in exc.body.lower():
            raise HTTPException(status_code=409, detail=DUPLICATE_ALIAS) from None
        log.warning("LiteLLM refused key generation: %s", exc)
        raise gateway_error() from None

    ref = ApiKeyRef(
        user_id=user.id,
        litellm_token=generated.token,
        alias=body.alias,
        masked=mask_key(generated.key),
        created_at=datetime.now(UTC),
        expires_at=generated.expires,
        is_playground=False,
    )
    try:
        db.add(ref)
        await db.commit()
    except Exception:
        await db.rollback()
        try:  # do not leave a live key that the user can never see or revoke
            await litellm.delete_keys([generated.token])
        except Exception:
            log.warning("could not roll back LiteLLM key after a failed insert", exc_info=True)
        raise
    return {
        "id": str(ref.id),
        "alias": ref.alias,
        "key": generated.key,
        "masked": ref.masked,
        "created_at": _iso(ref.created_at),
        "expires_at": _iso(ref.expires_at),
    }


@router.get("/api/keys")
async def list_keys(
    response: Response,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
    litellm: LiteLLMClient = Depends(get_litellm),
) -> list[dict]:
    refs = (
        await db.scalars(
            select(ApiKeyRef)
            .where(ApiKeyRef.user_id == user.id, ApiKeyRef.is_playground.is_(False))
            .order_by(ApiKeyRef.created_at.desc())
        )
    ).all()
    await db.commit()  # release the connection before the LiteLLM call
    by_key = {}
    if refs:
        today = datetime.now(UTC).date()
        try:
            by_key = (await litellm.user_activity(str(user.id), today - timedelta(days=30), today)).by_key
        except (LiteLLMUnavailable, LiteLLMError):
            log.warning("LiteLLM usage unavailable while listing keys", exc_info=True)
            response.headers["X-Usage-Unavailable"] = "1"
    items = []
    for ref in refs:
        usage = by_key.get(ref.litellm_token)
        items.append(
            {
                "id": str(ref.id),
                "alias": ref.alias,
                "masked": ref.masked,
                "created_at": _iso(ref.created_at),
                "expires_at": _iso(ref.expires_at),
                "tokens_30d": usage.tokens if usage else 0,
                "requests_30d": usage.requests if usage else 0,
            }
        )
    return items


@router.delete("/api/keys/{key_id}", status_code=204, dependencies=[Depends(require_csrf)])
async def delete_key(
    key_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
    litellm: LiteLLMClient = Depends(get_litellm),
) -> Response:
    not_found = HTTPException(status_code=404, detail="Ключ не найден")
    try:
        parsed = uuid.UUID(key_id)
    except ValueError:
        raise not_found from None
    ref = await db.scalar(
        select(ApiKeyRef).where(
            ApiKeyRef.id == parsed, ApiKeyRef.user_id == user.id, ApiKeyRef.is_playground.is_(False)
        )
    )
    if ref is None:
        raise not_found
    await db.commit()  # release the connection before the LiteLLM call
    try:
        await litellm.delete_keys([ref.litellm_token])
    except LiteLLMUnavailable:
        raise gateway_down() from None
    except LiteLLMError as exc:
        log.warning("LiteLLM refused key deletion: %s", exc)
        raise gateway_error() from None
    await db.delete(ref)
    await audit.record(
        db, user, "key.revoke", "key", str(ref.id),
        {"owner": user.email, "alias": ref.alias, "masked": ref.masked, "by": "owner"}, audit.client_ip(request),
    )
    await db.commit()
    return Response(status_code=204)
