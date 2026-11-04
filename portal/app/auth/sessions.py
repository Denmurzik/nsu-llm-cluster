"""Server-side web sessions, the session cookie and the auth/CSRF dependencies."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_session
from app.deps import get_settings_dep
from app.models import User, WebSession

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


async def _load(db: AsyncSession, raw: str, ttl_hours: int) -> tuple[WebSession | None, bool]:
    """Returns (session or None, whether the expiry was extended)."""
    if not raw:
        return None, False
    ws = await db.get(WebSession, _hash(raw))
    if ws is None:
        return None, False
    now = _now()
    ttl = timedelta(hours=ttl_hours)
    if ws.expires_at <= now:
        await db.delete(ws)
        await db.commit()
        return None, False
    if ws.expires_at - now < ttl / 2:
        ws.expires_at = now + ttl
        await db.commit()
        return ws, True
    return ws, False


async def load_session(db: AsyncSession, raw: str, ttl_hours: int = DEFAULT_TTL_HOURS) -> WebSession | None:
    """Expired sessions are removed and yield None; a session with less than half of the TTL left slides."""
    ws, _ = await _load(db, raw, ttl_hours)
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


async def on_user_blocked(db: AsyncSession, user: User) -> None:
    """Hook for the panel (task 2-32) to call when it blocks a user, before its commit: the sessions end at once.

    TODO(2-32): revoke the user's LiteLLM keys here (a blocked user's API keys must stop working too).
    """
    await end_user_sessions(db, user.id)


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
    ws, slid = await _load(db, raw, settings.session_ttl_hours)
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
    listed = user.email.lower() in settings.admin_email_set
    if user.role_source == "manual":
        pass  # the role was set in the panel: ADMIN_EMAILS does not touch it
    elif user.role == "admin" and not listed:
        # ADMIN_EMAILS changed since the login: the role follows the current list, not the one at login time
        user.role = "user"
    elif user.role == "user" and listed and not settings.admin_require_email_verified:
        # without the email_verified requirement the list alone decides, also between logins
        user.role = "admin"
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
