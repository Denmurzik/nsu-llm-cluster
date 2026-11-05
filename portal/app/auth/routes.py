"""NSU ID login (BFF): /auth/login, /auth/callback, /auth/logout and /api/me."""

import hashlib
import html
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import roles
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
    session_max_age,
    set_session_cookie,
)
from app.config import Settings
from app.db import get_session
from app.deps import REDIS_ERRORS, get_settings_dep
from app.models import User, WebSession

log = logging.getLogger(__name__)

router = APIRouter()

STATE_TTL = timedelta(minutes=10)
STATE_COOKIE = "nsullm_oauth_state"
RETURN_TO_MAX = 2048
# Redis: the login in progress (nonce, PKCE verifier, where to go back), for STATE_TTL; the key is the hash of the state,
# so a dump of Redis gives no state to replay. An anonymous /auth/login writes nothing to Postgres (finding L2).
STATE_KEY = "oauth:state:"

_ERROR_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход не выполнен</title>
</head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem;">
<h1>Не удалось войти</h1>
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


_NOT_CONFIGURED_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход не настроен</title>
</head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem;">
<h1>Вход не настроен</h1>
<p>Не задано: {missing}. Администратору главного сервера нужно прописать их в <code>.env</code> и перезапустить главный
(установщик: <code>sudo ./main/install-main.sh</code>).</p>
</body>
</html>
"""


def missing_oidc(settings: Settings) -> list[str]:
    """The OIDC settings without a value: there is no default provider, so an empty one cannot log anyone in."""
    return [
        name
        for name, value in (
            ("OIDC_ISSUER", settings.oidc_issuer),
            ("OIDC_CLIENT_ID", settings.oidc_client_id),
            ("OIDC_CLIENT_SECRET", settings.oidc_client_secret.get_secret_value()),
        )
        if not value.strip()
    ]


_REFUSED_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход не разрешён</title>
</head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem;">
<h1>Вход не разрешён</h1>
<p>Этот портал открыт не для всех учётных записей провайдера входа, и ваша в их число не входит (адрес почты не из
разрешённого домена, не подтверждён или у учётной записи нет нужной группы).</p>
<p>Если вам нужен доступ, обратитесь к администратору портала. Чтобы войти под другой учётной записью, выйдите из неё
у провайдера входа и <a href="/auth/login">войдите снова</a>.</p>
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
    missing = missing_oidc(settings)
    if missing:
        log.error("login is not configured: %s is empty (set it in .env and restart main)", ", ".join(missing))
        return HTMLResponse(_NOT_CONFIGURED_PAGE.format(missing=html.escape(", ".join(missing))), status_code=503)
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
    flow = {"nonce": nonce, "verifier": verifier, "return_to": safe_return_to(return_to)}
    try:
        await request.app.state.redis.set(_state_key(state), json.dumps(flow), ex=int(STATE_TTL.total_seconds()))
    except REDIS_ERRORS:
        log.warning("login: the state cannot be stored, Redis is unavailable", exc_info=True)
        return _error_page(503)
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


def _state_key(state: str) -> str:
    return STATE_KEY + hashlib.sha256(state.encode()).hexdigest()


async def _take_state(request: Request, state: str) -> dict | None:
    """The login in progress for `state`, removed at once (single use: GETDEL); None when unknown or expired."""
    raw = await request.app.state.redis.getdel(_state_key(state))
    if not raw:
        return None
    flow = json.loads(raw)
    return flow if isinstance(flow, dict) else None


def _full_name(claims: dict) -> str:
    name = claims.get("name")
    if name:
        return str(name)
    return " ".join(str(claims[k]) for k in ("given_name", "family_name") if claims.get(k))


async def _upsert_user(db: AsyncSession, claims: dict, settings: Settings) -> User:
    user = await db.scalar(select(User).where(User.sub == claims["sub"]))
    if user is None:
        user = User(sub=claims["sub"])
        db.add(user)
    user.email = str(claims.get("email") or "")
    # only a confirmation counts: an absent claim is "not verified" (app.auth.roles)
    user.email_verified = roles.verified(claims)
    user.username = str(claims.get("preferred_username") or "")
    user.full_name = _full_name(claims)
    if user.role_source != "manual":  # a role set in the panel is kept; a new user is `auto` (the column default)
        user.role = roles.auto_role(user, settings)
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
        # single use: the state is removed atomically before anything else happens; Redis expires it after STATE_TTL
        stored = await _take_state(request, state)
        if stored is None:
            return _error_page()
        tokens = await oidc.exchange_code(code, _redirect_uri(settings), str(stored["verifier"]))
        if not secrets.compare_digest(str(tokens.claims.get("nonce", "")), str(stored["nonce"])):
            raise OidcError("nonce mismatch")
        refusal = roles.login_refusal(tokens.claims, settings)
        if refusal is not None:
            # before the user row and the session: an account the portal is not for leaves nothing behind
            log.warning("login refused for subject %s: %s", tokens.claims.get("sub"), refusal)
            return HTMLResponse(_REFUSED_PAGE, status_code=403)
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

    response = RedirectResponse(safe_return_to(str(stored.get("return_to") or "/")), status_code=302)
    set_session_cookie(response, raw, settings)
    return response


async def _logout(
    request: Request, db: AsyncSession, settings: Settings, oidc: OidcClient, status_code: int
) -> RedirectResponse:
    raw = request.cookies.get(settings.cookie_name, "")
    target = "/"
    if raw:
        ws = await load_session(db, raw, settings.session_ttl_hours, await session_max_age(request.app.state))
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
