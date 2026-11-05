"""Server-side web sessions, the session cookie and the auth/CSRF dependencies."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store
from app.auth import roles
from app.config import Settings
from app.db import get_session
from app.deps import get_settings_dep
from app.models import ApiKeyRef, User, WebSession

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_TTL_HOURS = 12


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


async def create_session(
    db: AsyncSession, user: User, id_token: str, ttl_hours: int = DEFAULT_TTL_HOURS
) -> tuple[str, WebSession]:
    raw = secrets.token_urlsafe(32)
    # expired sessions are only removed when someone presents them; sweep them here as well
    await db.execute(delete(WebSession).where(WebSession.expires_at <= _now()))
    ws = WebSession(
        id_hash=_hash(raw),
        user_id=user.id,
        id_token=id_token,
        csrf_token=secrets.token_urlsafe(32),
        expires_at=_now() + timedelta(hours=ttl_hours),
    )
    db.add(ws)
    await db.commit()
    return raw, ws


async def _load(
    db: AsyncSession, raw: str, ttl_hours: int, max_age: timedelta | None = None
) -> tuple[WebSession | None, bool]:
    """Returns (session or None, whether the expiry was extended). `max_age`: the absolute lifetime counted from the
    login (finding L1): the sliding expiry never goes past it, and a session older than that is removed."""
    if not raw:
        return None, False
    ws = await db.get(WebSession, _hash(raw))
    if ws is None:
        return None, False
    now = _now()
    ttl = timedelta(hours=ttl_hours)
    limit = ws.created_at + max_age if max_age is not None else None
    if ws.expires_at <= now or (limit is not None and limit <= now):
        await db.delete(ws)
        await db.commit()
        return None, False
    if ws.expires_at - now < ttl / 2:
        new_expiry = now + ttl if limit is None else min(now + ttl, limit)
        if new_expiry > ws.expires_at:
            ws.expires_at = new_expiry
            await db.commit()
            return ws, True
    return ws, False


async def session_max_age(state) -> timedelta:
    """The absolute lifetime of a web session: the runtime setting `session_max_days` (panel), else SESSION_MAX_DAYS."""
    return timedelta(days=int(await settings_store.live(state, "session_max_days")))


async def load_session(
    db: AsyncSession, raw: str, ttl_hours: int = DEFAULT_TTL_HOURS, max_age: timedelta | None = None
) -> WebSession | None:
    """Expired sessions are removed and yield None; a session with less than half of the TTL left slides (never past
    `max_age` after its login)."""
    ws, _ = await _load(db, raw, ttl_hours, max_age)
    return ws


async def delete_session(db: AsyncSession, raw: str) -> None:
    await db.execute(delete(WebSession).where(WebSession.id_hash == _hash(raw)))
    await db.commit()


BLOCKED_TEXT = "Доступ заблокирован администратором"


def blocked_message(user: User) -> str:
    """«Доступ заблокирован администратором: <причина>» (without the reason, no colon)."""
    return f"{BLOCKED_TEXT}: {user.blocked_reason}" if user.blocked_reason else BLOCKED_TEXT


async def end_user_sessions(db: AsyncSession, user_id) -> None:
    """Drops every web session of the user; the caller commits."""
    await db.execute(delete(WebSession).where(WebSession.user_id == user_id))


async def on_user_blocked(db: AsyncSession, user: User, litellm=None) -> None:
    """Called by the panel (api/admin_users.py) when it blocks a user, before its commit.

    The sessions end at once, and with a LiteLLM client every key of the user (the Playground one too) is disabled
    (`/key/block`: the key values survive, `unblock_keys` restores them). A LiteLLM error propagates, the caller rolls
    the whole change back and nobody is left half-blocked (the client releases the keys it had already blocked).
    """
    await end_user_sessions(db, user.id)
    if litellm is not None:
        tokens = (await db.scalars(select(ApiKeyRef.litellm_token).where(ApiKeyRef.user_id == user.id))).all()
        await litellm.block_keys(list(tokens))


def set_session_cookie(response: Response, raw: str, settings: Settings) -> None:
    response.set_cookie(
        settings.cookie_name,
        raw,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


SLIDE_SCOPE_KEY = "nsullm.session_cookie"


class SlidingCookieMiddleware:
    """Sets the refreshed session cookie on whatever response the request ends with.

    A dependency cannot do it: headers of the injected `Response` are dropped when the handler returns a
    response of its own (streaming, 204) or raises HTTPException. The dependency leaves the cookie value in
    the ASGI scope instead and this middleware adds the header when the response starts.
    """

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message):
            raw = scope.get(SLIDE_SCOPE_KEY)
            if message["type"] == "http.response.start" and raw:
                cookie = Response()
                set_session_cookie(cookie, raw, self.settings)
                headers = list(message.get("headers", []))
                headers += [(k, v) for k, v in cookie.raw_headers if k == b"set-cookie"]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cookie)


async def current_web_session(
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> WebSession:
    raw = request.cookies.get(settings.cookie_name, "")
    ws, slid = await _load(db, raw, settings.session_ttl_hours, await session_max_age(request.app.state))
    if ws is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    if slid:
        request.scope[SLIDE_SCOPE_KEY] = raw
    return ws


async def current_user(
    ws: WebSession = Depends(current_web_session),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> User:
    user = await db.scalar(select(User).where(User.id == ws.user_id))
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход")
    if user.blocked:
        # blocked after the login: the session ends now (the next request is a plain 401)
        await end_user_sessions(db, user.id)
        await db.commit()
        raise HTTPException(status_code=403, detail=blocked_message(user))
    if user.role_source != "manual":  # a role set in the panel: ADMIN_SUBS/ADMIN_EMAILS do not touch it
        # the lists (and ADMIN_REQUIRE_EMAIL_VERIFIED) may have changed since the login: the role follows them now, with
        # the email_verified the provider sent at that login (app.auth.roles)
        user.role = roles.auto_role(user, settings)
    # end the transaction now: the handler may talk to LiteLLM for a long time and must not hold a pooled
    # connection meanwhile (the sessionmaker does not expire objects on commit, so `user` stays usable)
    await db.commit()
    return user


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


async def require_csrf(request: Request, ws: WebSession = Depends(current_web_session)) -> None:
    if request.method not in _MUTATING:
        return
    header = request.headers.get("X-CSRF-Token", "")
    if not header or not secrets.compare_digest(header, ws.csrf_token):
        raise HTTPException(status_code=403, detail="CSRF")
