"""Admin API of users and the audit log: list and search, role, block, limits, a user's keys, the log and its CSV.

Every change goes through `audit.record` in the transaction of the change. What reaches LiteLLM (blocking and
unblocking the keys, the limits) is done before the commit, so a LiteLLM error leaves the database as it was.
Blocking disables the keys (`/key/block`) instead of deleting them: unblocking brings the very same key values back,
a user who was blocked by mistake loses nothing, and a key an admin revoked meanwhile stays gone (it is deleted).
"""

import asyncio
import csv
import io
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, key_access, user_limits
from app.api.keys import gateway_down, gateway_error
from app.api.schemas_admin_ops import AuditEntryOut, AuditPageOut
from app.auth import roles
from app.auth.sessions import on_user_blocked, require_admin, require_csrf
from app.config import Settings
from app.db import get_session
from app.deps import get_litellm, get_settings_dep
from app.litellm_client import LiteLLMError, LiteLLMUnavailable
from app.models import ApiKeyRef, AuditLog, PortalFlag, User
from app.user_limits import key_limits

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

USAGE_DAYS = 30
USAGE_CONCURRENCY = 8
USAGE_TTL_S = 300.0  # the 30-day tokens of a user are recounted at most every five minutes
EXPORT_MAX = 50_000  # rows of the CSV export; more is cut and the header X-Truncated says so
MAX_RPM, MAX_TPM, MAX_KEYS = 1_000_000, 1_000_000_000, 1000  # the bounds of the matching settings (settings_store)

NOT_FOUND = "Пользователь не найден"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ---- usage of the last 30 days, cached ---------------------------------------------------------------------------


async def _tokens_30d(request: Request, litellm, user_ids: list[str]) -> dict[str, int | None]:
    """Prompt plus completion tokens of the last 30 days per user, None when LiteLLM cannot tell.

    A number is kept for USAGE_TTL_S in the process, so paging and searching do not call LiteLLM again; only the users
    of the page that are not cached are asked (never the whole table), eight at a time.
    """
    cache: dict[str, tuple[float, int]] = request.app.state.__dict__.setdefault("user_usage_cache", {})
    now = time.monotonic()
    out: dict[str, int | None] = {}
    missing: list[str] = []
    for uid in user_ids:
        hit = cache.get(uid)
        if hit is not None and now - hit[0] < USAGE_TTL_S:
            out[uid] = hit[1]
        else:
            missing.append(uid)
    if not missing:
        return out
    if not await litellm.health():
        return {**out, **dict.fromkeys(missing)}
    end = datetime.now(UTC).date()
    start = end - timedelta(days=USAGE_DAYS - 1)
    gate = asyncio.Semaphore(USAGE_CONCURRENCY)

    async def one(uid: str) -> int | None:
        async with gate:
            try:
                activity = await litellm.user_activity(uid, start, end)
            except (LiteLLMUnavailable, LiteLLMError) as exc:
                log.warning("admin users: no usage for %s: %s", uid, exc)
                return None
            return activity.prompt_tokens + activity.completion_tokens

    values = await asyncio.gather(*(one(u) for u in missing))
    for uid, value in zip(missing, values, strict=True):
        out[uid] = value
        if value is not None:
            cache[uid] = (now, value)
    return out


# ---- users --------------------------------------------------------------------------------------------------------


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    role_source: str
    blocked: bool
    blocked_reason: str | None
    rpm_limit: int | None
    tpm_limit: int | None
    max_keys: int | None
    created_at: str | None
    last_login_at: str | None
    keys: int
    tokens_30d: int | None


class UsersPage(BaseModel):
    items: list[UserOut]
    total: int


def _user_out(u: User, keys: int, tokens: int | None) -> UserOut:
    return UserOut(
        id=str(u.id),
        email=u.email,
        full_name=u.full_name,
        role=u.role,
        role_source=u.role_source,
        blocked=u.blocked,
        blocked_reason=u.blocked_reason,
        rpm_limit=u.rpm_limit,
        tpm_limit=u.tpm_limit,
        max_keys=u.max_keys,
        created_at=_iso(u.created_at),
        last_login_at=_iso(u.last_login_at),
        keys=keys,
        tokens_30d=tokens,
    )


def _like(text: str) -> str:
    """A LIKE pattern that finds `text` anywhere; % and _ of the text are literal."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _key_counts(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = await db.execute(
        select(ApiKeyRef.user_id, func.count())
        .where(ApiKeyRef.user_id.in_(ids), ApiKeyRef.is_playground.is_(False))
        .group_by(ApiKeyRef.user_id)
    )
    return dict(rows.all())


@router.get("/users", response_model=UsersPage)
async def list_users(
    request: Request,
    q: Annotated[str, Query(max_length=200)] = "",
    role: Annotated[str | None, Query(pattern="^(user|admin)$")] = None,
    blocked: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    db: AsyncSession = Depends(get_session),
    litellm=Depends(get_litellm),
) -> UsersPage:
    conditions = []
    text = q.strip()
    if text:
        pattern = _like(text)
        conditions.append(
            or_(
                User.email.ilike(pattern, escape="\\"),
                User.full_name.ilike(pattern, escape="\\"),
                User.username.ilike(pattern, escape="\\"),
            )
        )
    if role:
        conditions.append(User.role == role)
    if blocked is not None:
        conditions.append(User.blocked.is_(blocked))
    total = await db.scalar(select(func.count()).select_from(User).where(*conditions))
    rows = (
        await db.scalars(
            select(User).where(*conditions).order_by(User.created_at, User.email, User.id).limit(limit).offset(offset)
        )
    ).all()
    counts = await _key_counts(db, [u.id for u in rows])
    await db.commit()  # release the connection before the LiteLLM calls
    tokens = await _tokens_30d(request, litellm, [str(u.id) for u in rows])
    return UsersPage(items=[_user_out(u, counts.get(u.id, 0), tokens[str(u.id)]) for u in rows], total=total or 0)


class UserPatch(BaseModel):
    """Only the fields that are sent change. `rpm_limit`, `tpm_limit` and `max_keys` set to null go back to the
    global setting; `role_source: "auto"` gives the role back to ADMIN_EMAILS."""

    model_config = ConfigDict(extra="forbid")

    role: str | None = Field(default=None, pattern="^(user|admin)$")
    role_source: str | None = Field(default=None, pattern="^auto$")
    blocked: bool | None = None
    blocked_reason: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=500)] = None
    rpm_limit: int | None = Field(default=None, ge=1, le=MAX_RPM)
    tpm_limit: int | None = Field(default=None, ge=1, le=MAX_TPM)
    max_keys: int | None = Field(default=None, ge=1, le=MAX_KEYS)


def _conflict(text: str) -> HTTPException:
    return HTTPException(status_code=409, detail=text)


async def _lock_admins(db: AsyncSession, also: uuid.UUID) -> list[User]:
    """Locks the administrators and the row `also` in ONE pass in id order, and refreshes them. Every handler that
    takes several user rows takes them in this order, so two admins who demote or block each other at once wait for
    one another instead of deadlocking: the second one then sees the first one's change."""
    rows = (
        await db.scalars(
            select(User)
            .where(or_(User.role == "admin", User.id == also))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    return [u for u in rows if u.role == "admin"]


async def guard_last_admin(db: AsyncSession, target: User) -> None:
    """409 when `target` is the only administrator who can still sign in (not blocked).

    Locks the administrators in id order first, so two admins who demote or block each other at the same moment
    cannot both succeed (the second one sees the first one's change)."""
    admins = await _lock_admins(db, target.id)
    if not any(a.id != target.id and not a.blocked for a in admins):
        raise _conflict("Это последний администратор: сначала назначьте другого")


async def _follow_role(db: AsyncSession, litellm, user: User, role: str) -> None:
    """The keys of the user follow the new role at once (the controllers' sweep does it too, within one round, if this
    fails): admins reach the experimental models, users do not."""
    try:
        if await key_access.sync_user_keys(db, litellm, user, role):
            key = key_access.FLAG_PREFIX + str(user.id)
            flag = await db.get(PortalFlag, key)
            if flag is None:
                db.add(PortalFlag(key=key, value=role))
            else:
                flag.value = role
            await db.commit()
    except (LiteLLMUnavailable, LiteLLMError) as exc:
        log.warning("admin users: the keys of %s follow the new role later: %s", user.id, exc)


async def _tokens_of(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    return list((await db.scalars(select(ApiKeyRef.litellm_token).where(ApiKeyRef.user_id == user_id))).all())


@router.patch("/users/{user_id}", response_model=UserOut, dependencies=[Depends(require_csrf)])
async def patch_user(
    user_id: uuid.UUID,
    body: UserPatch,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    litellm=Depends(get_litellm),
    settings: Settings = Depends(get_settings_dep),
) -> UserOut:
    sent = body.model_fields_set
    if not sent:
        raise HTTPException(status_code=422, detail="Нечего менять")
    if "role" in sent and "role_source" in sent:
        raise HTTPException(status_code=422, detail="Роль и «по списку ADMIN_EMAILS» вместе задавать нельзя")
    if "role" in sent and body.role is None or "role_source" in sent and body.role_source is None:
        raise HTTPException(status_code=422, detail="Роль нельзя очистить")
    if "blocked" in sent and body.blocked is None:
        raise HTTPException(status_code=422, detail="Не указано, блокировать или разблокировать")

    await _lock_admins(db, user_id)  # the target and all admins in one ordered pass, see _lock_admins
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    ip = audit.client_ip(request)
    myself = target.id == admin.id

    # -- what the change would make of the user
    old_role, old_source = target.role, target.role_source
    new_role, new_source = old_role, old_source
    if "role" in sent:
        new_role, new_source = body.role, "manual"
    elif "role_source" in sent:
        # ADMIN_SUBS / ADMIN_EMAILS decide again, with the email_verified of the user's last login (app.auth.roles)
        new_role, new_source = roles.auto_role(target, settings), "auto"
    new_blocked = target.blocked if "blocked" not in sent else bool(body.blocked)
    if body.blocked_reason and not new_blocked:
        raise HTTPException(status_code=422, detail="Причина нужна только у заблокированного пользователя")

    # -- the rules
    if myself and new_blocked and not target.blocked:
        raise _conflict("Нельзя заблокировать самого себя")
    if myself and old_role == "admin" and new_role != "admin":
        raise _conflict("Нельзя снять роль администратора с самого себя")
    if old_role == "admin" and not target.blocked and (new_role != "admin" or new_blocked):
        await guard_last_admin(db, target)

    tokens = await _tokens_of(db, target.id)
    effects: list[str] = []  # what was done in LiteLLM, to undo when the commit fails
    try:
        # -- role
        if (new_role, new_source) != (old_role, old_source):
            target.role, target.role_source = new_role, new_source
            await audit.record(
                db, admin, "user.role", "user", str(target.id),
                {"email": target.email, "old": old_role, "new": new_role, "role_source": new_source}, ip,
            )

        # -- block, unblock, the reason
        if new_blocked and not target.blocked:
            target.blocked, target.blocked_reason = True, body.blocked_reason or None
            await on_user_blocked(db, target, litellm)
            effects.append("block")
            await audit.record(
                db, admin, "user.block", "user", str(target.id),
                {"email": target.email, "reason": target.blocked_reason, "key_count": len(tokens)}, ip,
            )
        elif new_blocked and "blocked_reason" in sent and (body.blocked_reason or None) != target.blocked_reason:
            target.blocked_reason = body.blocked_reason or None
            await audit.record(
                db, admin, "user.block", "user", str(target.id),
                {"email": target.email, "reason": target.blocked_reason, "reason_changed": True}, ip,
            )
        elif "blocked" in sent and not new_blocked and target.blocked:
            # The effect is recorded BEFORE the call: if key 3 of 5 fails, keys 1-2 are already enabled and the undo
            # must block them again (blocking is idempotent and skips deleted keys). The user stays blocked.
            effects.append("unblock")
            await litellm.unblock_keys(tokens)
            target.blocked, target.blocked_reason = False, None
            await audit.record(
                db, admin, "user.unblock", "user", str(target.id), {"email": target.email, "key_count": len(tokens)}, ip
            )

        # -- limits
        changed: dict[str, dict[str, Any]] = {}
        for field in ("rpm_limit", "tpm_limit", "max_keys"):
            if field in sent and getattr(body, field) != getattr(target, field):
                changed[field] = {"old": getattr(target, field), "new": getattr(body, field)}
                setattr(target, field, getattr(body, field))
        if changed:
            await audit.record(
                db, admin, "user.limits", "user", str(target.id), {"email": target.email, "changed": changed}, ip
            )
            if "rpm_limit" in changed or "tpm_limit" in changed:
                rpm, tpm = await key_limits(db, target, settings)  # the user's own value or the global setting
                for token in tokens:  # a key the owner deleted meanwhile answers False and is skipped
                    await litellm.update_key_limits(token, rpm, tpm)
                # the user as a whole (all its keys and the Playground together, app.user_limits); the next sweep
                # sends them again, so a change undone below by a failure does not stay in LiteLLM
                await litellm.update_user_limits(str(target.id), rpm, tpm)
                await user_limits.forget(request.app.state.redis, str(target.id))
    except LiteLLMUnavailable:
        await db.rollback()
        await _undo(litellm, effects, tokens)
        raise gateway_down() from None
    except LiteLLMError as exc:
        await db.rollback()
        await _undo(litellm, effects, tokens)
        log.warning("LiteLLM refused a user change: %s", exc)
        raise gateway_error() from None

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        await _undo(litellm, effects, tokens)
        raise

    if new_role != old_role and not target.blocked:
        await _follow_role(db, litellm, target, new_role)
    counts = await _key_counts(db, [target.id])
    await db.commit()
    usage = await _tokens_30d(request, litellm, [str(target.id)])
    return _user_out(target, counts.get(target.id, 0), usage[str(target.id)])


async def _undo(litellm, effects: list[str], tokens: list[str]) -> None:
    """Best effort after a failed change: put the keys back the way they were."""
    for effect in effects:
        try:
            if effect == "block":
                await litellm.unblock_keys(tokens)
            elif effect == "unblock":
                await litellm.block_keys(tokens)
        except Exception:
            log.warning("admin users: could not undo %s of the keys", effect, exc_info=True)


# ---- the keys of a user -------------------------------------------------------------------------------------------


@router.get("/users/{user_id}/keys")
async def user_keys(
    user_id: uuid.UUID,
    response: Response,
    db: AsyncSession = Depends(get_session),
    litellm=Depends(get_litellm),
) -> list[dict]:
    """The keys of a user as the user sees them (the masked value only, never the key)."""
    if await db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    refs = (
        await db.scalars(
            select(ApiKeyRef)
            .where(ApiKeyRef.user_id == user_id, ApiKeyRef.is_playground.is_(False))
            .order_by(ApiKeyRef.created_at.desc())
        )
    ).all()
    await db.commit()  # release the connection before the LiteLLM call
    by_key = {}
    if refs:
        today = datetime.now(UTC).date()
        try:
            by_key = (await litellm.user_activity(str(user_id), today - timedelta(days=USAGE_DAYS), today)).by_key
        except (LiteLLMUnavailable, LiteLLMError):
            log.warning("admin users: usage unavailable while listing keys", exc_info=True)
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


@router.delete("/users/{user_id}/keys/{key_id}", status_code=204, dependencies=[Depends(require_csrf)])
async def revoke_user_key(
    user_id: uuid.UUID,
    key_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    litellm=Depends(get_litellm),
) -> Response:
    ref = await db.scalar(
        select(ApiKeyRef).where(
            ApiKeyRef.id == key_id, ApiKeyRef.user_id == user_id, ApiKeyRef.is_playground.is_(False)
        )
    )
    if ref is None:
        raise HTTPException(status_code=404, detail="Ключ не найден")
    owner = await db.get(User, user_id)
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
        db, admin, "key.revoke", "key", str(ref.id),
        {"owner": owner.email if owner else None, "alias": ref.alias, "masked": ref.masked, "by": "admin"},
        audit.client_ip(request),
    )
    await db.commit()
    return Response(status_code=204)


# ---- the audit log ------------------------------------------------------------------------------------------------


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _audit_conditions(
    action: str, actor: str, target: str, since: datetime | None, until: datetime | None
) -> list:
    conditions = []
    if action.strip():
        conditions.append(AuditLog.action.startswith(action.strip(), autoescape=True))
    if actor.strip():
        text = actor.strip()
        match = [AuditLog.actor_email.ilike(_like(text), escape="\\")]
        try:
            match.append(AuditLog.actor_id == uuid.UUID(text))
        except ValueError:
            pass
        conditions.append(or_(*match))
    if target.strip():
        conditions.append(AuditLog.target.ilike(_like(target.strip()), escape="\\"))
    if since is not None and until is not None and _aware(since) > _aware(until):
        raise HTTPException(status_code=422, detail="Начало периода позже его конца")
    if since is not None:
        conditions.append(AuditLog.at >= (since if since.tzinfo else since.replace(tzinfo=UTC)))
    if until is not None:
        conditions.append(AuditLog.at <= (until if until.tzinfo else until.replace(tzinfo=UTC)))
    return conditions


@router.get("/audit", response_model=AuditPageOut)
async def audit_log(
    action: Annotated[str, Query(max_length=64, description="prefix: «user.» finds user.role, user.block, ...")] = "",
    actor: Annotated[str, Query(max_length=320, description="part of the email of the actor, or the actor's id")] = "",
    target: Annotated[str, Query(max_length=255, description="part of the target")] = "",
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    db: AsyncSession = Depends(get_session),
) -> AuditPageOut:
    conditions = _audit_conditions(action, actor, target, since, until)
    total = await db.scalar(select(func.count()).select_from(AuditLog).where(*conditions))
    rows = (
        await db.scalars(
            select(AuditLog).where(*conditions).order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)
        )
    ).all()
    return AuditPageOut(items=[AuditEntryOut.of(r) for r in rows], total=total or 0)


def _csv_cell(value: Any) -> str:
    """A cell that a spreadsheet will not take for a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/audit/export")
async def audit_export(
    action: Annotated[str, Query(max_length=64)] = "",
    actor: Annotated[str, Query(max_length=320)] = "",
    target: Annotated[str, Query(max_length=255)] = "",
    since: datetime | None = None,
    until: datetime | None = None,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """The filtered log as CSV (UTF-8 with a BOM, so Excel reads the Russian text), newest first, at most EXPORT_MAX rows."""
    conditions = _audit_conditions(action, actor, target, since, until)
    rows = (
        await db.scalars(
            select(AuditLog).where(*conditions).order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(EXPORT_MAX + 1)
        )
    ).all()
    truncated = len(rows) > EXPORT_MAX
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "time", "actor", "action", "target_type", "target", "ip", "details"])
    for row in rows[:EXPORT_MAX]:
        details = json.dumps(row.details, ensure_ascii=False, sort_keys=True) if row.details else ""
        writer.writerow(
            [row.id, row.at.isoformat(), *map(_csv_cell, (row.actor_email, row.action, row.target_type, row.target, row.ip, details))]
        )
    name = f"audit-{datetime.now(UTC):%Y%m%d-%H%M%S}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"}
    if truncated:
        headers["X-Truncated"] = "1"
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)
