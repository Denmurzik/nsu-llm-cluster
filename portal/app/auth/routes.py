"""NSU ID login (BFF): /auth/login, /auth/callback, /auth/logout and /api/me."""

import html
import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.oidc import OidcClient, OidcError, get_oidc
from app.auth.sessions import (
    blocked_message,
    create_session,
    current_user,
    current_web_session,
    delete_session,
    end_user_sessions,
    load_session,
    require_csrf,
    set_session_cookie,
)
from app.config import Settings
from app.db import get_session
from app.deps import get_settings_dep
from app.models import OAuthState, User, WebSession

log = logging.getLogger(__name__)

router = APIRouter()

STATE_TTL = timedelta(minutes=10)
STATE_COOKIE = "nsullm_oauth_state"
RETURN_TO_MAX = 2048  # the length of the OAuthState.return_to column

_ERROR_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход не выполнен</title>
</head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem;">
<h1>Не удалось войти через NSU ID</h1>
<p>Попробуйте войти ещё раз. Если ошибка повторяется, обратитесь к администратору.</p>
<p><a href="/auth/login">Войти снова</a></p>
</body>
</html>
"""


_BLOCKED_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Доступ заблокирован</title>
</head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem;">
<h1>Вход невозможен</h1>
<p>{message}</p>
<p>Если это ошибка, обратитесь к администратору.</p>
</body>
</html>
"""


def _error_page(status_code: int = 400) -> HTMLResponse:
    return HTMLResponse(_ERROR_PAGE, status_code=status_code)


def _blocked_page(user: User) -> HTMLResponse:
    return HTMLResponse(_BLOCKED_PAGE.format(message=html.escape(blocked_message(user))), status_code=403)


def safe_return_to(value: str | None) -> str:
    """Only local absolute paths that fit the column are allowed; anything else becomes "/"."""
    if not value or len(value) > RETURN_TO_MAX or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    if "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return "/"
    return value


def _redirect_uri(settings: Settings) -> str:
    # The same value goes to the authorization request and to the code exchange, as OIDC requires.
    return settings.oidc_redirect_uri.strip() or f"{settings.public_base_url}/auth/callback"


def _post_logout_uri(settings: Settings) -> str:
    return settings.oidc_post_logout_redirect_uri.strip() or f"{settings.public_base_url}/"


@router.get("/auth/login")
async def login(
    request: Request,
    return_to: str | None = None,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    oidc: OidcClient = Depends(get_oidc),
):
    host = request.headers.get("host", "").rsplit(":", 1)[0].lower()
    if settings.main_advertise_ip and host == settings.main_advertise_ip and host != settings.public_hostname.lower():
        # the state cookie is host-only and the redirect URI names PUBLIC_HOSTNAME: a login begun on the IP
        # address would never find its cookie at the callback, so it starts on the public name instead
        # (unless PUBLIC_HOSTNAME is that IP address: the redirect would land here again, a loop)
        target = f"{settings.public_base_url}/auth/login"
        if return_to:
            target += f"?return_to={quote(safe_return_to(return_to), safe='')}"
        return RedirectResponse(target, status_code=302)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    try:
        url = await oidc.authorization_url(_redirect_uri(settings), state, nonce, verifier)
    except Exception:
        log.warning("OIDC discovery failed", exc_info=True)
        return _error_page(503)
    await db.execute(delete(OAuthState).where(OAuthState.created_at < datetime.now(UTC) - STATE_TTL))
    db.add(OAuthState(state=state, nonce=nonce, code_verifier=verifier, return_to=safe_return_to(return_to)))
    await db.commit()
    response = RedirectResponse(url, status_code=302)
    # binds the flow to this browser: a callback link opened elsewhere (login CSRF) is rejected
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=int(STATE_TTL.total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/auth/callback",
    )
    return response


def _full_name(claims: dict) -> str:
    name = claims.get("name")
    if name:
        return str(name)
    return " ".join(str(claims[k]) for k in ("given_name", "family_name") if claims.get(k))


async def _upsert_user(db: AsyncSession, claims: dict, settings: Settings) -> User:
    email = str(claims.get("email") or "")
    # an absent claim does not block the login; ADMIN_REQUIRE_EMAIL_VERIFIED=false ignores the claim
    unverified = settings.admin_require_email_verified and str(claims.get("email_verified", "")).lower() == "false"
    role = "admin" if email.lower() in settings.admin_email_set and not unverified else "user"
    user = await db.scalar(select(User).where(User.sub == claims["sub"]))
    if user is None:
        user = User(sub=claims["sub"])
        db.add(user)
    user.email = email
    user.username = str(claims.get("preferred_username") or "")
    user.full_name = _full_name(claims)
    if user.role_source != "manual":  # a role set in the panel is kept; a new user is `auto` (the column default)
        user.role = role
    user.last_login_at = datetime.now(UTC)
    await db.commit()
    return user


@router.get("/auth/callback")
async def callback(
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    oidc: OidcClient = Depends(get_oidc),
):
    response = await _handle_callback(request, db, settings, oidc)
    response.delete_cookie(STATE_COOKIE, path="/auth/callback")
    return response


async def _handle_callback(request: Request, db: AsyncSession, settings: Settings, oidc: OidcClient):
    try:
        q = request.query_params
        if q.get("error"):
            log.warning("OIDC provider returned error=%s", q.get("error"))
            return _error_page()
        code, state = q.get("code"), q.get("state")
        if not code or not state:
            return _error_page()
        bound = request.cookies.get(STATE_COOKIE, "")
        if not bound or not secrets.compare_digest(bound.encode(), state.encode()):
            log.warning("OIDC callback state is not bound to this browser")
            return _error_page()
        # single use: the row is removed atomically before anything else happens
        stored = (await db.execute(delete(OAuthState).where(OAuthState.state == state).returning(OAuthState))).scalar()
        await db.commit()
        if stored is None or datetime.now(UTC) - stored.created_at > STATE_TTL:
            return _error_page()
        tokens = await oidc.exchange_code(code, _redirect_uri(settings), stored.code_verifier)
        if not secrets.compare_digest(str(tokens.claims.get("nonce", "")), stored.nonce):
            raise OidcError("nonce mismatch")
        user = await _upsert_user(db, tokens.claims, settings)
        if user.blocked:
            await end_user_sessions(db, user.id)
            await db.commit()
            return _blocked_page(user)
        raw, _ = await create_session(db, user, tokens.id_token, settings.session_ttl_hours)
    except Exception:
        log.warning("OIDC callback failed", exc_info=True)
        return _error_page()

    try:
        await request.app.state.litellm.ensure_user(str(user.id), user.email)
    except Exception:
        log.warning("LiteLLM ensure_user failed for user %s", user.id, exc_info=True)

    response = RedirectResponse(stored.return_to, status_code=302)
    set_session_cookie(response, raw, settings)
    return response


async def _logout(
    request: Request, db: AsyncSession, settings: Settings, oidc: OidcClient, status_code: int
) -> RedirectResponse:
    raw = request.cookies.get(settings.cookie_name, "")
    target = "/"
    if raw:
        ws = await load_session(db, raw, settings.session_ttl_hours)
        await delete_session(db, raw)
        if ws is not None:
            try:
                target = (
                    await oidc.end_session_url(ws.id_token, _post_logout_uri(settings)) or "/"
                )
            except Exception:
                log.warning("OIDC end-session lookup failed", exc_info=True)
    response = RedirectResponse(target, status_code=status_code)
    response.delete_cookie(settings.cookie_name, path="/")
    return response


@router.get("/auth/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    oidc: OidcClient = Depends(get_oidc),
):
    """Kept as GET for plain links. A cross-site page can log a user out this way (a low-risk logout CSRF:
    the user only signs in again); `POST /auth/logout` with the CSRF header is the safe form."""
    return await _logout(request, db, settings, oidc, 302)


@router.post("/auth/logout", dependencies=[Depends(require_csrf)])
async def logout_post(
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    oidc: OidcClient = Depends(get_oidc),
):
    return await _logout(request, db, settings, oidc, 303)


@router.get("/api/me")
async def me(user: User = Depends(current_user), ws: WebSession = Depends(current_web_session)) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "csrf_token": ws.csrf_token,
    }
