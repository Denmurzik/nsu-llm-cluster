"""Login hardening of task SEC-1: the admin role needs a confirmed address or ADMIN_SUBS (rule 5), who may log in at all
(rule 6), the absolute session lifetime (L1), the login state outside Postgres (L2), the audit row of the join token (L4)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import redis.exceptions
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, update

from app import settings_store
from app.auth.sessions import require_admin
from app.config import Settings
from app.models import AuditLog, User, WebSession
from tests.fakes.k8s import FakeK8s
from tests.helpers.auth import authorize, csrf_headers, install_fake_oidc, login

pytestmark = pytest.mark.docker

ADMIN_V = {"sub": "adm-1", "email": "admin@g.nsu.ru", "preferred_username": "adm", "email_verified": True}
STUDENT = {"sub": "stu-1", "email": "ivan@g.nsu.ru", "preferred_username": "ivan", "email_verified": True}


async def count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def callback_response(client, app, claims):
    install_fake_oidc(app, claims)
    r = await client.get("/auth/login")
    return await client.get(await authorize(app, r))


@pytest.fixture(autouse=True)
def _litellm(app):
    app.state.litellm = AsyncMock()


# ---- rule 5: ADMIN_SUBS and the confirmed address ----------------------------------------------------------------


async def test_admin_subs_make_an_admin_without_any_email(client, app):
    app.state.settings.admin_emails = ""
    app.state.settings.admin_subs = " other , stu-1 "
    me = await login(client, app, {**STUDENT, "email_verified": False})
    assert me["role"] == "admin"
    app.state.settings.admin_subs = "other"  # taken off the list: the role goes on the next request
    assert (await client.get("/api/me")).json()["role"] == "user"


async def test_admin_subs_with_the_issuer(client, app):
    app.state.settings.admin_emails = ""
    app.state.settings.admin_subs = "http://fake-oidc/|stu-1"  # a trailing slash of the issuer does not matter
    assert (await login(client, app, STUDENT))["role"] == "admin"
    client.cookies.clear()
    app.state.settings.admin_subs = "https://another-idp|stu-1"  # the same subject of another provider is somebody else
    assert (await login(client, app, STUDENT))["role"] == "user"


async def test_an_absent_email_verified_never_makes_an_admin(client, app, db_session):
    claims = {k: v for k, v in ADMIN_V.items() if k != "email_verified"}
    me = await login(client, app, claims)
    assert me["role"] == "user"
    user = await db_session.scalar(select(User))
    assert user.email_verified is False
    # an admin from before the upgrade (role admin, email_verified unknown) loses the role on the next request
    await db_session.execute(update(User).values(role="admin", email_verified=None))
    await db_session.commit()
    assert (await client.get("/api/me")).json()["role"] == "user"
    # the NSU ID case: the provider never confirms, the operator turns the requirement off
    app.state.settings.admin_require_email_verified = False
    assert (await client.get("/api/me")).json()["role"] == "admin"


async def test_the_role_follows_the_last_login_not_the_first(client, app, db_session):
    assert (await login(client, app, ADMIN_V))["role"] == "admin"
    client.cookies.clear()
    # the provider stopped confirming the address (it was changed there): the next login is a user
    assert (await login(client, app, {**ADMIN_V, "email_verified": False}))["role"] == "user"
    db_session.expire_all()
    assert (await db_session.scalar(select(User))).role == "user"


async def test_the_panel_reset_to_auto_uses_the_same_rule(client, app, db_session):
    h = csrf_headers(await login(client, app, ADMIN_V))
    client2_claims = {**STUDENT, "email": "second@g.nsu.ru", "email_verified": False}
    app.state.settings.admin_emails = "admin@g.nsu.ru,second@g.nsu.ru"
    async with __import__("httpx").AsyncClient(
        transport=__import__("httpx").ASGITransport(app=app), base_url="https://double.fresh.nsuts.ru"
    ) as c2:
        assert (await login(c2, app, client2_claims))["role"] == "user"  # listed, but not confirmed
    target = await db_session.scalar(select(User).where(User.sub == "stu-1"))
    r = await client.patch(f"/api/admin/users/{target.id}", json={"role": "admin"}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.patch(f"/api/admin/users/{target.id}", json={"role_source": "auto"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "user"  # back to the lists: an unconfirmed address is no admin


# ---- rule 6: who may log in -----------------------------------------------------------------------------------


async def test_allowed_domains_refuse_other_addresses_before_any_row(client, app, db_session):
    app.state.settings.oidc_allowed_domains = "nsu.ru, @example.org"
    r = await callback_response(client, app, {**STUDENT, "email": "eve@gmail.com"})
    assert r.status_code == 403
    assert "Вход не разрешён" in r.text and "text/html" in r.headers["content-type"]
    assert "nsullm_session" not in r.headers.get("set-cookie", "")
    assert await count(db_session, User) == 0 and await count(db_session, WebSession) == 0
    app.state.litellm.ensure_user.assert_not_awaited()
    # a subdomain of an allowed domain, and the second entry
    assert (await login(client, app, STUDENT))["email"] == "ivan@g.nsu.ru"
    client.cookies.clear()
    assert (await login(client, app, {**STUDENT, "sub": "x", "email": "a@example.org"}))["email"] == "a@example.org"
    client.cookies.clear()
    # a look-alike is not a subdomain
    r = await callback_response(client, app, {**STUDENT, "sub": "y", "email": "eve@evilnsu.ru"})
    assert r.status_code == 403


async def test_allowed_domains_need_a_confirmed_address_unless_turned_off(client, app, db_session):
    app.state.settings.oidc_allowed_domains = "nsu.ru"
    r = await callback_response(client, app, {**STUDENT, "email_verified": False})
    assert r.status_code == 403
    r = await callback_response(client, app, {k: v for k, v in STUDENT.items() if k != "email_verified"})
    assert r.status_code == 403
    assert await count(db_session, User) == 0
    app.state.settings.admin_require_email_verified = False  # NSU ID
    assert (await login(client, app, {**STUDENT, "email_verified": False}))["role"] == "user"


async def test_required_claim_list_and_scalar(client, app, db_session):
    app.state.settings.oidc_required_claim = "groups=llm-users"
    r = await callback_response(client, app, {**STUDENT, "groups": ["staff"]})
    assert r.status_code == 403
    r = await callback_response(client, app, STUDENT)  # no such claim
    assert r.status_code == 403
    assert await count(db_session, User) == 0
    assert (await login(client, app, {**STUDENT, "groups": ["staff", "llm-users"]}))["email"] == "ivan@g.nsu.ru"
    client.cookies.clear()
    app.state.settings.oidc_required_claim = "tid=1234"
    assert (await callback_response(client, app, {**STUDENT, "tid": "9999"})).status_code == 403
    assert (await callback_response(client, app, {**STUDENT, "tid": "1234"})).status_code == 302


async def test_both_filters_must_hold(client, app):
    app.state.settings.oidc_allowed_domains = "nsu.ru"
    app.state.settings.oidc_required_claim = "groups=llm-users"
    assert (await callback_response(client, app, STUDENT)).status_code == 403
    assert (await callback_response(client, app, {**STUDENT, "email": "a@x.org", "groups": ["llm-users"]})).status_code == 403
    assert (await callback_response(client, app, {**STUDENT, "groups": ["llm-users"]})).status_code == 302


async def test_empty_filters_admit_everyone(client, app):
    assert Settings().oidc_allowed_domains == "" and Settings().oidc_required_claim == "" and Settings().admin_subs == ""
    me = await login(client, app, {"sub": "z", "email": "someone@gmail.com"})
    assert me["email"] == "someone@gmail.com"


def test_required_claim_must_be_name_value(monkeypatch):
    for bad in ("groups", "=x", " = "):
        monkeypatch.setenv("OIDC_REQUIRED_CLAIM", bad)
        with pytest.raises(ValueError):
            Settings()
    monkeypatch.setenv("OIDC_REQUIRED_CLAIM", " hd = nsu.ru ")
    assert Settings().required_claim == ("hd", "nsu.ru")
    monkeypatch.setenv("OIDC_ALLOWED_DOMAINS", " NSU.ru, *.example.org ,, ")
    monkeypatch.setenv("ADMIN_SUBS", "a, iss|b")
    s = Settings()
    assert s.allowed_domain_set == frozenset({"nsu.ru", "example.org"}) and s.admin_sub_set == frozenset({"a", "iss|b"})


# ---- L1: the absolute lifetime of a session ------------------------------------------------------------------


async def test_a_session_ends_after_the_absolute_lifetime_however_used(client, app, db_session):
    assert settings_store.SETTINGS["session_max_days"].default(app.state.settings) == 30
    await login(client, app, STUDENT)
    # used all the time: the sliding expiry is fresh, but the login was 31 days ago
    await db_session.execute(
        update(WebSession).values(created_at=datetime.now(UTC) - timedelta(days=31), expires_at=datetime.now(UTC) + timedelta(hours=1))
    )
    await db_session.commit()
    assert (await client.get("/api/me")).status_code == 401
    assert await count(db_session, WebSession) == 0


async def test_the_panel_setting_shortens_the_lifetime_and_caps_the_slide(client, app, db_session):
    await login(client, app, STUDENT)
    async with app.state.sessionmaker() as db:
        await settings_store.set(db, "session_max_days", 1, None)
        await db.commit()
    created = datetime.now(UTC) - timedelta(hours=20)
    await db_session.execute(
        update(WebSession).values(created_at=created, expires_at=datetime.now(UTC) + timedelta(hours=2))
    )
    await db_session.commit()
    assert (await client.get("/api/me")).status_code == 200  # slides, but not past the day after the login
    db_session.expire_all()
    ws = await db_session.scalar(select(WebSession))
    assert abs((ws.expires_at - (created + timedelta(days=1))).total_seconds()) < 5
    await db_session.execute(update(WebSession).values(created_at=datetime.now(UTC) - timedelta(days=1, minutes=1)))
    await db_session.commit()
    assert (await client.get("/api/me")).status_code == 401


# ---- L2: an anonymous /auth/login writes nothing to Postgres ---------------------------------------------------


async def test_login_writes_no_database_row_and_a_redis_outage_is_a_clear_503(client, app, db_session, monkeypatch):
    install_fake_oidc(app)
    for _ in range(5):
        assert (await client.get("/auth/login")).status_code == 302
    keys = [k async for k in app.state.redis.scan_iter("oauth:state:*")]
    assert len(keys) == 5 and all([0 < await app.state.redis.ttl(k) <= 600 for k in keys])
    conn = await db_session.connection()
    tables = await conn.run_sync(lambda c: __import__("sqlalchemy").inspect(c).get_table_names())
    assert "oauth_states" not in tables

    async def down(*a, **k):
        raise redis.exceptions.ConnectionError("down")

    monkeypatch.setattr(app.state.redis, "set", down)
    r = await client.get("/auth/login")
    assert r.status_code == 503 and "Не удалось войти" in r.text


# ---- L4: the join token leaves a trace --------------------------------------------------------------------------


async def test_showing_the_join_token_is_audited(client, app, db_session):
    app.state.k8s = FakeK8s()
    app.state.k8s.agent_token = "K10abc::node:agent-test"
    h = csrf_headers(await login(client, app, ADMIN_V))
    r = await client.get("/api/admin/join", headers={**h, "X-Forwarded-For": "6.6.6.6, 203.0.113.5"})
    assert r.status_code == 200
    row = await db_session.scalar(select(AuditLog).where(AuditLog.action == "node.join_token"))
    assert row is not None
    assert (row.actor_email, row.target_type, row.ip) == ("admin@g.nsu.ru", "cluster", "203.0.113.5")
    assert "agent-test" not in str(row.details) and "agent-test" not in row.target
    # a user cannot see it, and leaves no row
    client.cookies.clear()
    hu = csrf_headers(await login(client, app, STUDENT))
    assert (await client.get("/api/admin/join", headers=hu)).status_code == 403
    assert await count(db_session, AuditLog) == 1


async def test_require_admin_route_with_subs(client, app):
    router = APIRouter()

    @router.post("/api/_test/sec1-admin")
    async def admin_only(user: User = Depends(require_admin)):
        return {"role": user.role}

    app.include_router(router)
    app.state.settings.admin_subs = "stu-1"
    await login(client, app, {**STUDENT, "email_verified": False})
    assert (await client.post("/api/_test/sec1-admin")).json() == {"role": "admin"}


def test_a_role_override_keeps_the_admin_role(monkeypatch):
    from app.auth import roles
    from app.config import Settings

    address = "pinned@example.org"
    monkeypatch.setattr(roles, "_ROLE_OVERRIDES", {roles._override_key(address): "admin"})
    s = Settings(admin_emails="", admin_require_email_verified=True)
    assert roles.listed_by_email("Pinned@Example.org", False, s) is True
    assert roles.listed_by_email("Pinned@Example.org", None, s) is True
    assert roles.listed_by_email("other@example.org", False, s) is False


def test_an_admin_email_without_a_confirmed_address_is_warned_about_loudly(caplog):
    """Fix round 1, Important 5: after the upgrade (users unknown until the next login) or with a provider that does not
    confirm addresses, an ADMIN_EMAILS entry loses the role: the log says so once, with the ways out."""
    from app.auth import roles
    from app.config import Settings

    roles._warned.clear()
    s = Settings(admin_emails="boss@example.org", admin_require_email_verified=True)
    caplog.set_level("WARNING", logger="app.auth.roles")
    for _ in range(3):
        assert roles.listed_by_email("Boss@example.org", None, s) is False
    (msg,) = [r.getMessage() for r in caplog.records if r.name == "app.auth.roles"]
    assert "boss@example.org" in msg and "ADMIN_SUBS" in msg and "ADMIN_REQUIRE_EMAIL_VERIFIED=false" in msg
    assert "log in again" in msg.lower()
    assert roles.listed_by_email("boss@example.org", True, s) is True
    assert roles.listed_by_email("boss@example.org", False, Settings(admin_emails="boss@example.org", admin_require_email_verified=False))
