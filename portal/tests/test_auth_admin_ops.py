"""Blocked users and roles set in the panel (task 2-31a): the login, the sessions and the role at login."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, update

from app import key_access
from app.config import Settings
from app.auth.sessions import on_user_blocked, require_admin
from app.models import User, WebSession
from tests.helpers.auth import authorize, csrf_headers, install_fake_oidc, login

pytestmark = pytest.mark.docker

USER = {"sub": "usr-1", "email": "ivan@g.nsu.ru", "preferred_username": "ivan"}
ADMIN = {"sub": "adm-1", "email": "admin@g.nsu.ru", "email_verified": True, "preferred_username": "adm"}
BLOCKED = "Доступ заблокирован администратором"


async def sessions(db) -> int:
    return (await db.execute(select(func.count()).select_from(WebSession))).scalar_one()


async def patch_user(db, sub: str, **values) -> User:
    await db.execute(update(User).where(User.sub == sub).values(**values))
    await db.commit()
    db.expire_all()
    return await db.scalar(select(User).where(User.sub == sub))


async def attempt_login(client, app, claims) -> httpx.Response:
    """The login flow up to the callback; returns the callback response."""
    install_fake_oidc(app, claims)
    r = await client.get("/auth/login")
    return await client.get(await authorize(app, r))


async def test_a_new_user_is_auto_and_not_blocked(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app, USER)
    user = await db_session.scalar(select(User))
    assert (user.role_source, user.blocked, user.blocked_reason) == ("auto", False, None)
    assert (user.rpm_limit, user.tpm_limit, user.max_keys) == (None, None, None)


async def test_a_blocked_user_cannot_log_in(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app, USER)
    client.cookies.clear()
    await patch_user(db_session, "usr-1", blocked=True, blocked_reason="нарушение правил")
    app.state.litellm.reset_mock()

    r = await attempt_login(client, app, USER)
    assert r.status_code == 403
    assert "text/html" in r.headers["content-type"]
    assert f"{BLOCKED}: нарушение правил" in r.text
    assert "nsullm_session" not in r.headers.get("set-cookie", "")
    assert (await client.get("/api/me")).status_code == 401
    assert await sessions(db_session) == 0
    app.state.litellm.ensure_user.assert_not_awaited()


async def test_a_blocked_user_without_a_reason_gets_the_plain_message(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app, USER)
    client.cookies.clear()
    await patch_user(db_session, "usr-1", blocked=True)
    r = await attempt_login(client, app, USER)
    assert r.status_code == 403 and BLOCKED in r.text and f"{BLOCKED}:" not in r.text


async def test_the_reason_is_escaped_on_the_page(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app, USER)
    client.cookies.clear()
    await patch_user(db_session, "usr-1", blocked=True, blocked_reason="<script>alert(1)</script>")
    r = await attempt_login(client, app, USER)
    assert r.status_code == 403 and "<script>alert" not in r.text and "&lt;script&gt;" in r.text


async def test_a_blocked_admin_is_refused_too(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app, ADMIN)
    client.cookies.clear()
    await patch_user(db_session, "adm-1", blocked=True, blocked_reason="отпуск")
    assert (await attempt_login(client, app, ADMIN)).status_code == 403


async def test_blocking_ends_the_sessions_at_once(client, app, db_session):
    app.state.litellm = AsyncMock()
    me = await login(client, app, USER)
    await login(client, app, USER)  # a second browser
    assert await sessions(db_session) == 2
    await patch_user(db_session, "usr-1", blocked=True, blocked_reason="спам")

    r = await client.get("/api/me")
    assert r.status_code == 403 and r.json()["detail"] == f"{BLOCKED}: спам"
    assert await sessions(db_session) == 0  # every session of the user is gone, not just this one
    assert (await client.get("/api/me")).status_code == 401
    r = await client.post("/api/keys", json={"alias": "x"}, headers=csrf_headers(me))
    assert r.status_code in (401, 403)


async def test_a_blocked_user_with_a_session_is_refused_by_every_api(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app, ADMIN)
    assert (await client.get("/api/admin/deployments")).status_code == 200
    await patch_user(db_session, "adm-1", blocked=True, blocked_reason="r")
    r = await client.get("/api/admin/deployments")
    assert r.status_code == 403 and r.json()["detail"].startswith(BLOCKED)


async def test_unblocking_lets_the_user_in_again(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app, USER)
    await patch_user(db_session, "usr-1", blocked=True, blocked_reason="r")
    assert (await client.get("/api/me")).status_code == 403
    client.cookies.clear()
    await patch_user(db_session, "usr-1", blocked=False, blocked_reason=None)
    assert (await login(client, app, USER))["email"] == "ivan@g.nsu.ru"


async def test_on_user_blocked_ends_every_session(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app, USER)
    await login(client, app, ADMIN)
    user = await db_session.scalar(select(User).where(User.sub == "usr-1"))
    await on_user_blocked(db_session, user)
    await db_session.commit()
    sessions_left = (await db_session.scalars(select(WebSession.user_id))).all()
    assert len(sessions_left) == 1 and user.id not in sessions_left  # the other user keeps theirs


# ---- the role set in the panel -----------------------------------------------------------------------


async def test_a_manual_role_is_kept_at_login(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app, USER)  # not in ADMIN_EMAILS: auto, user
    client.cookies.clear()
    await patch_user(db_session, "usr-1", role="admin", role_source="manual")
    me = await login(client, app, USER)
    assert me["role"] == "admin"  # ADMIN_EMAILS does not take it back
    assert (await db_session.scalar(select(User))).role_source == "manual"


async def test_a_manual_user_role_beats_admin_emails(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app, ADMIN)
    client.cookies.clear()
    await patch_user(db_session, "adm-1", role="user", role_source="manual")  # listed in ADMIN_EMAILS, demoted in the panel
    me = await login(client, app, ADMIN)
    assert me["role"] == "user"
    assert (await client.get("/api/admin/deployments")).status_code == 403


async def test_an_auto_role_still_follows_admin_emails_at_login(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app, USER)
    client.cookies.clear()
    await patch_user(db_session, "usr-1", role="admin")  # role_source stays auto
    assert (await login(client, app, USER))["role"] == "user"


async def test_a_manual_admin_is_not_demoted_between_logins(client, app, db_session):
    """current_user follows ADMIN_EMAILS for an auto role only."""
    app.state.litellm = AsyncMock()
    router = APIRouter()

    @router.post("/api/_test/admin3")
    async def admin_only(user: User = Depends(require_admin)):
        return {"role": user.role}

    app.include_router(router)
    await login(client, app, USER)
    await patch_user(db_session, "usr-1", role="admin", role_source="manual")
    assert (await client.post("/api/_test/admin3")).status_code == 200
    assert (await client.get("/api/me")).json()["role"] == "admin"
    app.state.settings.admin_emails = "someone-else@g.nsu.ru"
    assert (await client.post("/api/_test/admin3")).status_code == 200


def test_key_access_keeps_a_manual_role():
    settings = Settings(admin_emails="admin@g.nsu.ru")
    manual = User(sub="m", email="not-listed@g.nsu.ru", role="admin", role_source="manual")
    auto = User(sub="a", email="not-listed@g.nsu.ru", role="admin", role_source="auto")
    manual_user = User(sub="u", email="admin@g.nsu.ru", role="user", role_source="manual")
    assert key_access.effective_role(manual, settings) == "admin"
    assert key_access.effective_role(auto, settings) == "user"  # listed no more
    settings.admin_require_email_verified = False
    assert key_access.effective_role(manual_user, settings) == "user"  # listed, but demoted by hand
