import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, StringConstraints, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import current_user, require_csrf
from app.config import Settings
from app.db import get_session
from app.deps import get_litellm, get_settings_dep
from app.litellm_client import LiteLLMClient, LiteLLMError, LiteLLMUnavailable, models_for_role
from app.models import ApiKeyRef, User

log = logging.getLogger(__name__)

router = APIRouter()

GATEWAY_DOWN = "Шлюз моделей недоступен"
GATEWAY_ERROR = "Шлюз моделей вернул ошибку"
DUPLICATE_ALIAS = "Ключ с таким названием уже есть"


def gateway_down() -> HTTPException:
    return HTTPException(status_code=503, detail=GATEWAY_DOWN)


def gateway_error() -> HTTPException:
    return HTTPException(status_code=502, detail=GATEWAY_ERROR)


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
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
    litellm: LiteLLMClient = Depends(get_litellm),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    # serialize concurrent creates of one user so the limit cannot be overshot
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    regular = ApiKeyRef.is_playground.is_(False)
    count = await db.scalar(select(func.count()).select_from(ApiKeyRef).where(ApiKeyRef.user_id == user.id, regular))
    if count >= settings.max_keys_per_user:
        raise HTTPException(status_code=409, detail=f"Достигнут лимит ключей: {settings.max_keys_per_user}")
    taken = await db.scalar(
        select(ApiKeyRef.id).where(ApiKeyRef.user_id == user.id, regular, ApiKeyRef.alias == body.alias)
    )
    if taken is not None:
        raise HTTPException(status_code=409, detail=DUPLICATE_ALIAS)

    duration = f"{body.expires_days}d" if body.expires_days else None
    try:
        await litellm.ensure_user(str(user.id), user.email)
        generated = await litellm.generate_key(
            user_id=str(user.id),
            alias=litellm_alias(user, body.alias),
            duration=duration,
            rpm_limit=settings.key_rpm_limit,
            tpm_limit=settings.key_tpm_limit,
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
    await db.commit()
    return Response(status_code=204)
