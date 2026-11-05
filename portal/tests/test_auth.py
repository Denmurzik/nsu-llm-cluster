import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Depends
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey
from sqlalchemy import func, select, update

from app.auth.oidc import OidcClient, OidcError
from app.auth.sessions import create_session, current_user, delete_session, load_session, require_admin, require_csrf
from app.models import User, WebSession
from tests.helpers.auth import authorize, csrf_headers, install_fake_oidc, login

pytestmark = pytest.mark.docker

ERROR_TEXT = "Не удалось войти"


async def count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def oauth_keys(app) -> list[str]:
    return [k async for k in app.state.redis.scan_iter("oauth:state:*")]


async def start_login(client, app, **params):
    install_fake_oidc(app)
    r = await client.get("/auth/login", params=params)
    assert r.status_code == 302
    return r


def assert_login_failed(r):
    assert r.status_code == 400
    assert ERROR_TEXT in r.text
    assert 'href="/auth/login"' in r.text
    assert "text/html" in r.headers["content-type"]


async def test_login_redirects_to_provider_with_pkce(client, app):
    r = await start_login(client, app)
    url = urlparse(r.headers["location"])
    assert url.path.endswith("/authorize")
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["client_id"] == "nsu-llm-test"
    assert q["response_type"] == "code"
    assert q["response_mode"] == "query"
    assert q["scope"] == "openid profile email"
    assert q["redirect_uri"] == "https://double.fresh.nsuts.ru/auth/callback"
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"] and q["state"] and q["nonce"]


async def test_login_without_a_configured_provider_says_what_is_missing(client, app):
    install_fake_oidc(app)
    for field, name in (("oidc_issuer", "OIDC_ISSUER"), ("oidc_client_id", "OIDC_CLIENT_ID")):
        saved = getattr(app.state.settings, field)
        setattr(app.state.settings, field, "")
        try:
            r = await client.get("/auth/login")
        finally:
            setattr(app.state.settings, field, saved)
        assert r.status_code == 503
        assert "Вход не настроен" in r.text and name in r.text
        assert "location" not in r.headers
        assert "nsu.ru" not in r.text


def test_the_provider_has_no_default():
    from app.config import Settings

    s = Settings()
    assert s.oidc_issuer == "" and s.oidc_client_id == "" and "nsu" not in s.login_provider_name.lower()
    assert s.login_provider_name == "SSO"


async def test_public_status_names_the_login_provider(client, app):
    app.state.settings.login_provider_name = "NSU ID"
    assert (await client.get("/api/public/status")).json()["login_provider"] == "NSU ID"
    app.state.settings.login_provider_name = "  "
    assert (await client.get("/api/public/status")).json()["login_provider"] == "SSO"


async def test_full_flow_creates_user_session_and_cookie(client, app, db_session):
    app.state.litellm = AsyncMock()
    r = await start_login(client, app, return_to="/keys")
    callback = await authorize(app, r)
    r = await client.get(callback)
    assert r.status_code == 302
    assert r.headers["location"] == "/keys"
    set_cookie = r.headers["set-cookie"]
    assert set_cookie.startswith("nsullm_session=")
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Max-Age=43200" in set_cookie

    assert await count(db_session, User) == 1
    assert await count(db_session, WebSession) == 1
    assert await oauth_keys(app) == []  # the state was used up
    user = (await db_session.execute(select(User))).scalar_one()
    assert user.sub == "fake-user-1"
    assert user.email == "user@g.nsu.ru"
    assert user.username == "user"
    assert user.full_name == "Иван Петров"
    assert user.role == "user"
    app.state.litellm.ensure_user.assert_awaited_once_with(str(user.id), "user@g.nsu.ru")

    me = (await client.get("/api/me")).json()
    assert me["id"] == str(user.id)
    assert me["email"] == "user@g.nsu.ru"
    assert me["username"] == "user"
    assert me["full_name"] == "Иван Петров"
    assert me["role"] == "user"
    assert len(me["csrf_token"]) >= 32


async def test_secure_cookie_by_default(client, app):
    app.state.settings.cookie_secure = True
    r = await start_login(client, app)
    r = await client.get(await authorize(app, r))
    assert "Secure" in r.headers["set-cookie"]


async def test_repeated_login_reuses_user(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app)
    await login(client, app, claims={"sub": "fake-user-1", "email": "new@g.nsu.ru", "preferred_username": "user"})
    assert await count(db_session, User) == 1
    assert await count(db_session, WebSession) == 2
    user = (await db_session.execute(select(User))).scalar_one()
    assert user.email == "new@g.nsu.ru"


async def test_ensure_user_failure_is_not_fatal(client, app):
    app.state.litellm = AsyncMock()
    app.state.litellm.ensure_user.side_effect = RuntimeError("litellm down")
    me = await login(client, app)
    assert me["email"] == "user@g.nsu.ru"


async def test_admin_role_from_admin_emails(client, app):
    app.state.litellm = AsyncMock()
    claims = {"sub": "adm-1", "email": "ADMIN@g.nsu.ru", "preferred_username": "adm", "email_verified": True}
    me = await login(client, app, claims=claims)
    assert me["role"] == "admin"


ADMIN_CLAIMS = {"sub": "adm-1", "email": "admin@g.nsu.ru", "preferred_username": "adm", "email_verified": True}


async def test_admin_role_needs_a_verified_email_when_the_claim_says_so(client, app):
    app.state.litellm = AsyncMock()
    me = await login(client, app, claims={**ADMIN_CLAIMS, "email_verified": False})
    assert me["role"] == "user"
    client.cookies.clear()
    me = await login(client, app, claims={**ADMIN_CLAIMS, "email_verified": True})
    assert me["role"] == "admin"
    client.cookies.clear()
    client.cookies.clear()
    # no claim at all (finding H2: Entra multi-tenant and small IdPs let anyone set the address): logged in, as a user
    me = await login(client, app, claims={k: v for k, v in ADMIN_CLAIMS.items() if k != "email_verified"})
    assert me["role"] == "user"
    assert (await client.get("/api/me")).json()["role"] == "user"
    client.cookies.clear()
    me = await login(client, app, claims={**ADMIN_CLAIMS, "email_verified": "true"})  # some providers send a string
    assert me["role"] == "admin"


async def test_role_follows_the_current_admin_list_not_the_one_at_login(client, app, db_session):
    app.state.litellm = AsyncMock()
    router = APIRouter()

    @router.post("/api/_test/admin2")
    async def admin_only(user: User = Depends(require_admin)):
        return {"role": user.role}

    app.include_router(router)
    me = await login(client, app, claims=ADMIN_CLAIMS)
    assert me["role"] == "admin"
    assert (await client.post("/api/_test/admin2")).status_code == 200

    app.state.settings.admin_emails = "someone-else@g.nsu.ru"  # the admin was removed from ADMIN_EMAILS
    assert (await client.post("/api/_test/admin2")).status_code == 403
    assert (await client.get("/api/me")).json()["role"] == "user"
    db_session.expire_all()
    assert (await db_session.scalar(select(User))).role == "user"  # persisted, not only masked

    app.state.settings.admin_emails = "admin@g.nsu.ru"  # listed again: the verified login still counts
    assert (await client.post("/api/_test/admin2")).status_code == 200


async def test_callback_rejects_unknown_state(client, app, db_session):
    install_fake_oidc(app)
    assert_login_failed(await client.get("/auth/callback", params={"code": "x", "state": "nope"}))
    assert await count(db_session, WebSession) == 0


async def test_callback_rejects_replayed_state(client, app, db_session):
    app.state.litellm = AsyncMock()
    r = await start_login(client, app)
    callback = await authorize(app, r)
    assert (await client.get(callback)).status_code == 302
    client.cookies.clear()
    assert_login_failed(await client.get(callback))
    assert await count(db_session, WebSession) == 1
    assert (await client.get("/api/me")).status_code == 401


async def test_callback_rejects_expired_state(client, app, db_session):
    r = await start_login(client, app)
    callback = await authorize(app, r)
    keys = await oauth_keys(app)
    assert len(keys) == 1 and 0 < await app.state.redis.ttl(keys[0]) <= 600  # Redis forgets it after 10 minutes
    await app.state.redis.delete(*keys)  # what the TTL does
    assert_login_failed(await client.get(callback))
    assert await count(db_session, WebSession) == 0
    assert await count(db_session, User) == 0


async def test_callback_rejects_nonce_mismatch(client, app, db_session):
    r = await start_login(client, app)
    callback = await authorize(app, r)
    (key,) = await oauth_keys(app)
    flow = json.loads(await app.state.redis.get(key))
    await app.state.redis.set(key, json.dumps({**flow, "nonce": "another-nonce"}))
    assert_login_failed(await client.get(callback))
    assert await count(db_session, WebSession) == 0
    assert await count(db_session, User) == 0


async def test_callback_requires_state_cookie_of_same_login(client, app, db_session):
    app.state.litellm = AsyncMock()
    # attacker starts a login, captures the callback link and does not open it
    r = await start_login(client, app)
    attacker_callback = await authorize(app, r)
    # victim browser: no state cookie at all
    client.cookies.clear()
    assert_login_failed(await client.get(attacker_callback))
    assert await count(db_session, WebSession) == 0
    assert await count(db_session, User) == 0
    # victim browser has its own login in progress: another state in the cookie
    await start_login(client, app)
    assert_login_failed(await client.get(attacker_callback))
    assert await count(db_session, WebSession) == 0
    assert await count(db_session, User) == 0
    assert "nsullm_session" not in client.cookies


async def test_state_cookie_attributes_and_cleanup(client, app):
    app.state.litellm = AsyncMock()
    app.state.settings.cookie_secure = True
    r = await start_login(client, app)
    c = r.headers["set-cookie"]
    assert c.startswith("nsullm_oauth_state=")
    for part in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=600", "Path=/auth/callback"):
        assert part in c
    r = await client.get(await authorize(app, r))
    assert r.status_code == 302
    cookies = r.headers.get_list("set-cookie")
    assert any(h.startswith("nsullm_oauth_state=") and "Max-Age=0" in h for h in cookies)


async def test_callback_provider_error_param(client, app, db_session):
    install_fake_oidc(app)
    assert_login_failed(await client.get("/auth/callback", params={"error": "access_denied", "state": "x"}))
    assert await count(db_session, WebSession) == 0


async def test_callback_without_params(client, app):
    install_fake_oidc(app)
    assert_login_failed(await client.get("/auth/callback"))


@pytest.mark.parametrize(
    "target",
    ["https://evil.example/x", "//evil.example", "/\\evil.example", "evil", "javascript:alert(1)", "/ok\r\nX: y"],
)
async def test_return_to_open_redirect_blocked(client, app, target):
    app.state.litellm = AsyncMock()
    r = await start_login(client, app, return_to=target)
    r = await client.get(await authorize(app, r))
    assert r.status_code == 302
    assert r.headers["location"] == "/"


async def test_return_to_local_path_kept(client, app):
    app.state.litellm = AsyncMock()
    r = await start_login(client, app, return_to="/admin/nodes?tab=1")
    r = await client.get(await authorize(app, r))
    assert r.headers["location"] == "/admin/nodes?tab=1"


async def test_me_requires_login(client):
    r = await client.get("/api/me")
    assert r.status_code == 401
    assert r.json() == {"detail": "Требуется вход"}


async def test_logout_clears_session_and_redirects_to_end_session(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app)
    r = await client.get("/auth/logout")
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert loc.path.endswith("/logout")
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert q["post_logout_redirect_uri"] == "https://double.fresh.nsuts.ru/"
    assert q["id_token_hint"].count(".") == 2
    assert "nsullm_session=" in r.headers["set-cookie"]
    assert "Max-Age=0" in r.headers["set-cookie"]
    assert await count(db_session, WebSession) == 0
    client.cookies.clear()
    assert (await client.get("/api/me")).status_code == 401


async def test_logout_post_needs_csrf_and_ends_the_session(client, app, db_session):
    app.state.litellm = AsyncMock()
    me = await login(client, app)
    assert (await client.post("/auth/logout")).status_code == 403
    assert await count(db_session, WebSession) == 1  # a forged cross-site POST does not log the user out
    r = await client.post("/auth/logout", headers=csrf_headers(me))
    assert r.status_code == 303
    assert urlparse(r.headers["location"]).path.endswith("/logout")
    assert "Max-Age=0" in r.headers["set-cookie"]
    assert await count(db_session, WebSession) == 0


async def test_logout_without_session_redirects_home(client, app):
    install_fake_oidc(app)
    r = await client.get("/auth/logout")
    assert r.status_code == 302


async def test_logout_survives_provider_failure(client, app):
    app.state.litellm = AsyncMock()
    await login(client, app)

    def boom(request):
        raise httpx.ConnectError("down")

    app.state.oidc._metadata = None
    app.state.oidc.transport = httpx.MockTransport(boom)
    r = await client.get("/auth/logout")
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    assert (await client.get("/api/me")).status_code == 401


async def test_session_expiry_and_sliding(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app)
    session = (await db_session.execute(select(WebSession))).scalar_one()
    now = datetime.now(UTC)

    # more than half of the 12 h TTL left: not extended
    far = now + timedelta(hours=10)
    await db_session.execute(update(WebSession).values(expires_at=far))
    await db_session.commit()
    r = await client.get("/api/me")
    assert r.status_code == 200
    assert "set-cookie" not in r.headers
    await db_session.refresh(session)
    assert abs((session.expires_at - far).total_seconds()) < 1

    # less than half left: slides to a full TTL and the cookie is refreshed
    await db_session.execute(update(WebSession).values(expires_at=now + timedelta(hours=2)))
    await db_session.commit()
    r = await client.get("/api/me")
    assert r.status_code == 200
    assert "Max-Age=43200" in r.headers["set-cookie"]
    await db_session.refresh(session)
    assert session.expires_at > now + timedelta(hours=11, minutes=55)

    # expired: rejected and removed
    await db_session.execute(update(WebSession).values(expires_at=now - timedelta(seconds=1)))
    await db_session.commit()
    r = await client.get("/api/me")
    assert r.status_code == 401
    assert await count(db_session, WebSession) == 0


async def test_sessions_api_stores_only_a_hash(db_session):
    user = User(sub="s1", email="a@b.c")
    db_session.add(user)
    await db_session.commit()
    raw, ws = await create_session(db_session, user, "id-token", 12)
    assert raw != ws.id_hash and len(ws.id_hash) == 64
    assert (await load_session(db_session, raw)).csrf_token == ws.csrf_token
    assert await load_session(db_session, "garbage") is None
    await delete_session(db_session, raw)
    assert await load_session(db_session, raw) is None


async def test_csrf_required_for_mutation(client, app):
    router = APIRouter()

    @router.post("/api/_test/mutate", dependencies=[Depends(require_csrf)])
    async def mutate(user: User = Depends(current_user)):
        return {"ok": True, "email": user.email}

    app.include_router(router)
    app.state.litellm = AsyncMock()

    r = await client.post("/api/_test/mutate")
    assert r.status_code == 401
    me = await login(client, app)
    r = await client.post("/api/_test/mutate")
    assert r.status_code == 403
    assert r.json() == {"detail": "CSRF"}
    r = await client.post("/api/_test/mutate", headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    r = await client.post("/api/_test/mutate", headers=csrf_headers(me))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "email": "user@g.nsu.ru"}


async def test_require_admin(client, app):
    router = APIRouter()

    # POST: the SPA catch-all registered by create_app would shadow a GET route added afterwards
    @router.post("/api/_test/admin")
    async def admin_only(user: User = Depends(require_admin)):
        return {"role": user.role}

    app.include_router(router)
    app.state.litellm = AsyncMock()
    assert (await client.post("/api/_test/admin")).status_code == 401
    await login(client, app)
    r = await client.post("/api/_test/admin")
    assert r.status_code == 403
    assert r.json() == {"detail": "Недостаточно прав"}
    await login(client, app, claims={"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True})
    assert (await client.post("/api/_test/admin")).json() == {"role": "admin"}


ISSUER = "http://fake-oidc"


def _oidc_with_key(key: RSAKey) -> tuple[OidcClient, dict]:
    """Returns the client and a mutable dict: "key" is served as JWKS, "id_token" by /token."""
    provider = {"key": key, "id_token": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if path.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [provider["key"].as_dict(private=False)]})
        if path.endswith("/token"):
            return httpx.Response(200, json={"id_token": provider["id_token"], "access_token": "a"})
        return httpx.Response(404)

    return OidcClient(ISSUER, "cid", "secret", "openid", transport=httpx.MockTransport(handler)), provider


def _new_key(kid="k1") -> RSAKey:
    return RSAKey.import_key(rsa.generate_private_key(public_exponent=65537, key_size=2048), {"kid": kid})


def _claims(**over) -> dict:
    now = int(time.time())
    return {"iss": ISSUER, "aud": "cid", "sub": "u1", "exp": now + 300, "iat": now, **over}


@pytest.mark.parametrize(
    "over",
    [
        {"aud": "someone-else"},
        {"iss": "http://evil"},
        {"exp_offset": -120},  # seconds relative to "now", resolved in the test body
        {"sub": None},
    ],
)
async def test_oidc_rejects_bad_id_token_claims(over):
    key = _new_key()
    oidc, provider = _oidc_with_key(key)
    over = dict(over)
    if "exp_offset" in over:
        over["exp"] = int(time.time()) + over.pop("exp_offset")
    claims = {k: v for k, v in _claims(**over).items() if v is not None}
    provider["id_token"] = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)
    with pytest.raises(OidcError):
        await oidc.exchange_code("c", "https://x/cb", "v")


async def test_oidc_rejects_token_signed_by_another_key_and_wrong_alg():
    oidc, provider = _oidc_with_key(_new_key())
    provider["id_token"] = jwt.encode({"alg": "RS256", "kid": "k1"}, _claims(), _new_key())
    with pytest.raises(OidcError):
        await oidc.exchange_code("c", "https://x/cb", "v")

    provider["id_token"] = jwt.encode({"alg": "HS256", "kid": "k1"}, _claims(), OctKey.import_key("s" * 32))
    with pytest.raises(OidcError):
        await oidc.exchange_code("c", "https://x/cb", "v")


@pytest.mark.parametrize(
    "over",
    [{"iat_offset": 30}, {"exp_offset": -30}],  # seconds relative to "now", resolved in the test body
)
async def test_oidc_tolerates_small_clock_skew(over):
    key = _new_key()
    oidc, provider = _oidc_with_key(key)
    now = int(time.time())
    claims = {k.removesuffix("_offset"): now + v for k, v in over.items()}
    provider["id_token"] = jwt.encode({"alg": "RS256", "kid": "k1"}, _claims(**claims), key)
    assert (await oidc.exchange_code("c", "https://x/cb", "v")).claims["sub"] == "u1"


async def test_oidc_rejects_token_expired_beyond_leeway():
    key = _new_key()
    oidc, provider = _oidc_with_key(key)
    provider["id_token"] = jwt.encode({"alg": "RS256", "kid": "k1"}, _claims(exp=int(time.time()) - 120), key)
    with pytest.raises(OidcError):
        await oidc.exchange_code("c", "https://x/cb", "v")


async def test_oidc_accepts_valid_token_and_refetches_jwks_on_unknown_kid():
    key = _new_key()
    oidc, provider = _oidc_with_key(key)
    provider["id_token"] = jwt.encode({"alg": "RS256", "kid": "k1"}, _claims(), key)
    assert (await oidc.exchange_code("c", "https://x/cb", "v")).claims["sub"] == "u1"
    # the provider rotates its key: the cached key set has no such kid, so the JWKS is fetched again
    rotated = _new_key("k2")
    provider["key"] = rotated
    provider["id_token"] = jwt.encode({"alg": "RS256", "kid": "k2"}, _claims(), rotated)
    assert (await oidc.exchange_code("c", "https://x/cb", "v")).claims["sub"] == "u1"


async def test_without_the_verified_requirement_an_unverified_listed_email_is_admin(client, app):
    app.state.litellm = AsyncMock()
    app.state.settings.admin_require_email_verified = False  # NSU ID: email_verified is never true
    me = await login(client, app, claims={**ADMIN_CLAIMS, "email_verified": False})
    assert me["role"] == "admin"
    client.cookies.clear()
    other = {"sub": "u-2", "email": "user@g.nsu.ru", "preferred_username": "u", "email_verified": True}
    assert (await login(client, app, claims=other))["role"] == "user"  # the list alone decides


async def test_with_the_verified_requirement_an_unverified_admin_stays_user_on_requests(client, app):
    app.state.litellm = AsyncMock()
    assert app.state.settings.admin_require_email_verified is True  # the code default
    me = await login(client, app, claims={**ADMIN_CLAIMS, "email_verified": False})
    assert me["role"] == "user"
    assert (await client.get("/api/me")).json()["role"] == "user"  # no promotion without a verified login


async def test_without_the_verified_requirement_the_role_is_re_evaluated_on_each_request(client, app, db_session):
    app.state.litellm = AsyncMock()
    me = await login(client, app, claims={**ADMIN_CLAIMS, "email_verified": False})  # flag on: logged in as user
    assert me["role"] == "user"
    app.state.settings.admin_require_email_verified = False  # main restarted with the flag off
    assert (await client.get("/api/me")).json()["role"] == "admin"  # promoted without a new login
    db_session.expire_all()
    assert (await db_session.scalar(select(User))).role == "admin"
    app.state.settings.admin_emails = "someone-else@g.nsu.ru"
    assert (await client.get("/api/me")).json()["role"] == "user"  # and demoted when the list changes


def test_admin_require_email_verified_defaults_to_true_and_reads_the_env(monkeypatch):
    from app.config import Settings

    assert Settings().admin_require_email_verified is True
    monkeypatch.setenv("ADMIN_REQUIRE_EMAIL_VERIFIED", "false")
    assert Settings().admin_require_email_verified is False


async def test_login_through_a_bridge_domain_uses_the_registered_redirect_uri(client, app, db_session):
    """NSU ID only knows double.fresh.nsuts.ru: its front answers 302 to this portal's /auth/callback with the query."""
    app.state.litellm = AsyncMock()
    bridge = "https://double.fresh.nsuts.ru/llm-auth/callback"
    app.state.settings.oidc_redirect_uri = bridge
    app.state.settings.public_hostname = "n8n.nsumonkey.top"
    r = await start_login(client, app)
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    assert q["redirect_uri"] == bridge
    provider = app.state.fake_oidc_client
    back = urlparse((await provider.get(r.headers["location"])).headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == bridge
    # the front's 302 keeps the query; the code exchange must send the same redirect_uri, or the provider refuses it
    r = await client.get(f"/auth/callback?{back.query}")
    assert r.status_code == 302, r.text
    assert (await client.get("/api/me")).status_code == 200


async def test_logout_uses_the_configured_post_logout_uri(client, app, db_session):
    app.state.litellm = AsyncMock()
    await login(client, app)
    app.state.settings.oidc_post_logout_redirect_uri = "https://double.fresh.nsuts.ru/llm-auth/logged-out"
    r = await client.get("/auth/logout")
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    assert q["post_logout_redirect_uri"] == "https://double.fresh.nsuts.ru/llm-auth/logged-out"
