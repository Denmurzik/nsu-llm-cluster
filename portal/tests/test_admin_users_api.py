"""Users management and the audit log API (task 2-32): list and filters, role, block, limits, keys, audit and CSV."""

import csv
import io
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.api import admin_users
from app.litellm_client import Activity, KeyUsage, LiteLLMError
from app.models import ApiKeyRef, AuditLog, PortalFlag, User, WebSession
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
ADMIN2 = {"sub": "adm2", "email": "second@g.nsu.ru", "email_verified": True, "name": "Второй"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}
BLOCKED = "Доступ заблокирован администратором"


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    app.state.litellm = FakeLiteLLM()


@pytest.fixture
def settings(settings):
    settings.admin_emails = "admin@g.nsu.ru,second@g.nsu.ru"
    return settings


async def second_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


async def as_admin(client, app) -> dict:
    return csrf_headers(await login(client, app, ADMIN))


async def row(app, email: str) -> User:
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(User).where(User.email == email))


async def audit_rows(app, prefix: str = "") -> list[AuditLog]:
    async with app.state.sessionmaker() as db:
        rows = (await db.scalars(select(AuditLog).order_by(AuditLog.id))).all()
    return [r for r in rows if r.action.startswith(prefix)]


async def make_user(client, app, claims=USER, keys: tuple[str, ...] = ()) -> tuple[User, dict, list[dict]]:
    """Logs `claims` in on `client`, creates keys through the API; returns (user row, csrf headers, created keys)."""
    h = csrf_headers(await login(client, app, claims))
    made = []
    for alias in keys:
        r = await client.post("/api/keys", json={"alias": alias}, headers=h)
        assert r.status_code == 201, r.text
        made.append(r.json())
    return await row(app, claims["email"]), h, made


async def patch(client, user: User, h: dict, **body) -> httpx.Response:
    return await client.patch(f"/api/admin/users/{user.id}", json=body, headers=h)


# ---- list ---------------------------------------------------------------------------------------------------------


async def test_the_new_endpoints_are_for_admins_only(client, app):
    h = csrf_headers(await login(client, app, USER))
    me = await row(app, USER["email"])
    for method, url in (
        ("GET", "/api/admin/users"),
        ("PATCH", f"/api/admin/users/{me.id}"),
        ("GET", f"/api/admin/users/{me.id}/keys"),
        ("DELETE", f"/api/admin/users/{me.id}/keys/{me.id}"),
        ("GET", "/api/admin/audit"),
        ("GET", "/api/admin/audit/export"),
    ):
        r = await client.request(method, url, headers=h, json={"role": "admin"} if method == "PATCH" else None)
        assert r.status_code == 403, (method, url)
        assert r.json()["detail"] == "Недостаточно прав"
    assert (await row(app, USER["email"])).role == "user"


async def test_list_has_the_fields_the_panel_needs_and_counts_keys_and_tokens(client, app):
    other = await second_client(app)
    user, _, made = await make_user(other, app, keys=("a", "b"))
    app.state.litellm.activity[str(user.id)] = Activity(prompt_tokens=10, completion_tokens=5, requests=3)
    h = await as_admin(client, app)

    r = await client.get("/api/admin/users", headers=h)
    assert r.status_code == 200, r.text
    page = r.json()
    assert page["total"] == 2
    by_email = {u["email"]: u for u in page["items"]}
    ivan = by_email["ivan@g.nsu.ru"]
    assert set(ivan) == {
        "id", "email", "full_name", "role", "role_source", "blocked", "blocked_reason", "rpm_limit", "tpm_limit",
        "max_keys", "created_at", "last_login_at", "keys", "tokens_30d",
    }
    assert ivan["keys"] == 2 and ivan["tokens_30d"] == 15 and ivan["full_name"] == "Иван Иванов"
    assert (ivan["role"], ivan["role_source"], ivan["blocked"], ivan["rpm_limit"]) == ("user", "auto", False, None)
    assert by_email["admin@g.nsu.ru"]["role"] == "admin"
    assert made[0]["key"] not in r.text  # never a key value


async def test_list_searches_filters_and_pages(client, app):
    for claims in (
        USER,
        {"sub": "p", "email": "petrova@g.nsu.ru", "name": "Анна Петрова"},
        {"sub": "u", "email": "under_score@g.nsu.ru", "name": "Подчёркивание"},
        {"sub": "b", "email": "banned@g.nsu.ru", "name": "Нарушитель"},
    ):
        other = await second_client(app)
        await login(other, app, claims)
    h = await as_admin(client, app)
    banned = await row(app, "banned@g.nsu.ru")
    assert (await patch(client, banned, h, blocked=True, blocked_reason="спам")).status_code == 200

    def emails(r):
        assert r.status_code == 200, r.text
        return [u["email"] for u in r.json()["items"]]

    assert emails(await client.get("/api/admin/users?q=петрова", headers=h)) == ["petrova@g.nsu.ru"]  # by name
    assert emails(await client.get("/api/admin/users?q=IVAN@", headers=h)) == ["ivan@g.nsu.ru"]  # by email, any case
    assert emails(await client.get("/api/admin/users?q=_", headers=h)) == ["under_score@g.nsu.ru"]  # _ is literal
    assert emails(await client.get("/api/admin/users?q=%25", headers=h)) == []  # so is %
    assert emails(await client.get("/api/admin/users?role=admin", headers=h)) == ["admin@g.nsu.ru"]
    assert emails(await client.get("/api/admin/users?blocked=true", headers=h)) == ["banned@g.nsu.ru"]
    assert "banned@g.nsu.ru" not in emails(await client.get("/api/admin/users?blocked=false", headers=h))
    assert (await client.get("/api/admin/users?role=root", headers=h)).status_code == 422

    first = await client.get("/api/admin/users?limit=2", headers=h)
    rest = await client.get("/api/admin/users?limit=2&offset=2", headers=h)
    assert first.json()["total"] == rest.json()["total"] == 5
    assert len(emails(first)) == 2 and len(emails(rest)) == 2
    assert not set(emails(first)) & set(emails(rest))
    tail = await client.get("/api/admin/users?limit=2&offset=4", headers=h)
    assert len(emails(tail)) == 1


async def test_tokens_are_cached_and_only_the_page_is_asked(client, app):
    for i in range(4):
        other = await second_client(app)
        await login(other, app, {"sub": f"s{i}", "email": f"u{i}@g.nsu.ru"})
    calls: list[str] = []
    litellm = app.state.litellm
    original = litellm.user_activity

    async def counting(user_id, start, end):
        calls.append(user_id)
        return await original(user_id, start, end)

    litellm.user_activity = counting
    h = await as_admin(client, app)
    await client.get("/api/admin/users?limit=2", headers=h)
    assert len(calls) == 2  # the two users of the page, not the five of the table
    await client.get("/api/admin/users?limit=2", headers=h)
    assert len(calls) == 2  # cached
    await client.get("/api/admin/users?limit=2&offset=2", headers=h)
    assert len(calls) == 4


async def test_tokens_are_null_when_litellm_is_down_and_that_is_not_cached(client, app):
    other = await second_client(app)
    user, _, _ = await make_user(other, app)
    app.state.litellm.activity[str(user.id)] = Activity(prompt_tokens=7, completion_tokens=1)
    h = await as_admin(client, app)
    app.state.litellm.unavailable = True
    r = await client.get("/api/admin/users", headers=h)
    assert r.status_code == 200 and all(u["tokens_30d"] is None for u in r.json()["items"])
    app.state.litellm.unavailable = False
    r = await client.get("/api/admin/users", headers=h)
    assert {u["email"]: u["tokens_30d"] for u in r.json()["items"]}["ivan@g.nsu.ru"] == 8


# ---- role ---------------------------------------------------------------------------------------------------------


async def test_promoting_sets_a_manual_role_audits_it_and_moves_the_keys(client, app):
    other = await second_client(app)
    user, _, made = await make_user(other, app, keys=("k",))
    token = (await _tokens(app, user))[0]
    assert app.state.litellm.keys[token]["models"] == ["public"]
    h = await as_admin(client, app)

    r = await patch(client, user, h, role="admin")
    assert r.status_code == 200, r.text
    assert (r.json()["role"], r.json()["role_source"]) == ("admin", "manual")
    after = await row(app, USER["email"])
    assert (after.role, after.role_source) == ("admin", "manual")
    assert app.state.litellm.keys[token]["models"] == ["public", "experimental"]  # at once, not at the next sweep
    async with app.state.sessionmaker() as db:
        flag = await db.get(PortalFlag, f"keyaccess:{user.id}")
    assert flag is not None and flag.value == "admin"

    (entry,) = await audit_rows(app, "user.role")
    assert entry.actor_email == "admin@g.nsu.ru" and entry.target_type == "user" and entry.target == str(user.id)
    assert entry.details == {"email": "ivan@g.nsu.ru", "old": "user", "new": "admin", "role_source": "manual"}
    assert entry.ip

    me = await other.get("/api/me")
    assert me.json()["role"] == "admin"  # ADMIN_EMAILS does not take a manual role back

    assert (await patch(client, user, h, role="user")).status_code == 200
    assert app.state.litellm.keys[token]["models"] == ["public"]
    assert (await row(app, USER["email"])).role_source == "manual"  # still pinned: the list does not decide


async def test_back_to_the_admin_emails_list(client, app):
    other = await second_client(app)
    listed, _, _ = await make_user(other, app, ADMIN2)  # in ADMIN_EMAILS
    h = await as_admin(client, app)
    assert (await patch(client, listed, h, role="user")).status_code == 200  # demoted by hand: manual user
    assert (await row(app, ADMIN2["email"])).role == "user"

    r = await patch(client, listed, h, role_source="auto")
    assert r.status_code == 200, r.text
    assert (r.json()["role"], r.json()["role_source"]) == ("admin", "auto")  # the list decides again
    entries = await audit_rows(app, "user.role")
    assert entries[-1].details["role_source"] == "auto" and entries[-1].details["new"] == "admin"

    unlisted, _, _ = await make_user(await second_client(app), app)
    assert (await patch(client, unlisted, h, role="admin")).status_code == 200
    r = await patch(client, unlisted, h, role_source="auto")
    assert (r.json()["role"], r.json()["role_source"]) == ("user", "auto")  # not in the list: back to a user


async def test_a_patch_must_say_one_clear_thing(client, app):
    other = await second_client(app)
    user, _, _ = await make_user(other, app)
    h = await as_admin(client, app)
    for body in (
        {},
        {"role": "admin", "role_source": "auto"},
        {"role": None},
        {"role": "root"},
        {"role_source": "manual"},
        {"blocked": None},
        {"rpm_limit": 0},
        {"tpm_limit": -5},
        {"max_keys": 0},
        {"max_keys": 100000},
        {"blocked_reason": "x" * 501, "blocked": True},
        {"surprise": 1},
    ):
        r = await patch(client, user, h, **body)
        assert r.status_code == 422, (body, r.text)
    assert (await patch(client, user, h, blocked_reason="причина")).status_code == 422  # not blocked: no reason
    assert await audit_rows(app, "user.") == []
    r = await client.patch(f"/api/admin/users/{user.id}", json={"role": "admin"})  # no CSRF header
    assert r.status_code == 403
    r = await client.patch("/api/admin/users/00000000-0000-0000-0000-000000000000", json={"role": "admin"}, headers=h)
    assert r.status_code == 404
    assert (await client.patch("/api/admin/users/not-a-uuid", json={"role": "admin"}, headers=h)).status_code == 422


# ---- the rules ----------------------------------------------------------------------------------------------------


async def test_an_admin_cannot_block_or_demote_themselves(client, app):
    h = await as_admin(client, app)
    me = await row(app, ADMIN["email"])
    r = await patch(client, me, h, blocked=True, blocked_reason="oops")
    assert r.status_code == 409 and "самого себя" in r.json()["detail"]
    r = await patch(client, me, h, role="user")
    assert r.status_code == 409 and "с самого себя" in r.json()["detail"]
    r = await patch(client, me, h, role_source="auto")  # fine while ADMIN_EMAILS lists them: stays an admin
    assert r.status_code == 200 and r.json()["role"] == "admin"

    other = await second_client(app)
    await login(other, app, ADMIN2)  # a second admin exists: still not oneself
    assert (await patch(client, me, h, role="user")).status_code == 409
    assert (await row(app, ADMIN["email"])).role == "admin"

    me_now = await row(app, ADMIN["email"])
    assert (await patch(client, me_now, h, rpm_limit=30)).status_code == 200  # other changes of oneself are fine


async def test_self_demotion_through_the_list_is_refused_too(client, app, settings):
    h = await as_admin(client, app)
    me = await row(app, ADMIN["email"])
    assert (await patch(client, me, h, role="admin")).status_code == 200  # pin the role by hand
    settings.admin_emails = "second@g.nsu.ru"  # the list no longer has this admin
    r = await patch(client, me, h, role_source="auto")
    assert r.status_code == 409  # would make them a user


async def test_the_last_admin_who_can_sign_in_is_protected(app):
    async with app.state.sessionmaker() as db:
        boss = User(sub="b", email="b@g.nsu.ru", role="admin")
        away = User(sub="a", email="a@g.nsu.ru", role="admin", blocked=True)  # blocked: cannot take over
        db.add_all([boss, away])
        await db.commit()
        with pytest.raises(admin_users.HTTPException) as err:
            await admin_users.guard_last_admin(db, boss)
        assert err.value.status_code == 409 and "последний администратор" in err.value.detail
        db.add(User(sub="c", email="c@g.nsu.ru", role="admin"))
        await db.commit()
        await admin_users.guard_last_admin(db, boss)  # another one is there


async def test_demoting_or_blocking_another_admin_is_fine(client, app):
    other = await second_client(app)
    second, _, _ = await make_user(other, app, ADMIN2)
    h = await as_admin(client, app)
    assert (await patch(client, second, h, role="user")).status_code == 200
    assert (await patch(client, second, h, role="admin")).status_code == 200
    assert (await patch(client, second, h, blocked=True)).status_code == 200


# ---- blocking -----------------------------------------------------------------------------------------------------


async def _tokens(app, user: User) -> list[str]:
    async with app.state.sessionmaker() as db:
        return list((await db.scalars(select(ApiKeyRef.litellm_token).where(ApiKeyRef.user_id == user.id))).all())


async def test_blocking_disables_the_keys_ends_the_sessions_and_audits(client, app):
    other = await second_client(app)
    user, uh, made = await make_user(other, app, keys=("one", "two"))
    tokens = await _tokens(app, user)
    litellm = app.state.litellm
    payload = {"model": "any", "messages": []}
    h = await as_admin(client, app)

    r = await patch(client, user, h, blocked=True, blocked_reason="  нарушение правил  ")
    assert r.status_code == 200, r.text
    assert (r.json()["blocked"], r.json()["blocked_reason"]) == (True, "нарушение правил")
    assert all(litellm.keys[t]["blocked"] for t in tokens) and len(tokens) == 2
    for key in made:  # the key values are refused by the gateway
        with pytest.raises(LiteLLMError) as err:
            async for _ in litellm.stream_chat(key["key"], payload):
                pass
        assert err.value.status == 401
    assert (await other.get("/api/me")).status_code == 401  # the session is gone
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(WebSession).where(WebSession.user_id == user.id))).all() == []
    assert (await row(app, USER["email"])).blocked is True

    (entry,) = await audit_rows(app, "user.block")
    assert entry.details == {"email": "ivan@g.nsu.ru", "reason": "нарушение правил", "key_count": 2}
    assert entry.target == str(user.id) and entry.actor_email == "admin@g.nsu.ru"
    assert not any(k["key"] in str(entry.details) for k in made)


async def test_unblocking_brings_back_the_same_key_values_but_not_a_revoked_key(client, app):
    other = await second_client(app)
    user, uh, made = await make_user(other, app, keys=("one", "two"))
    h = await as_admin(client, app)
    assert (await patch(client, user, h, blocked=True)).status_code == 200
    gone = made[1]
    r = await client.delete(f"/api/admin/users/{user.id}/keys/{gone['id']}", headers=h)  # revoked while blocked
    assert r.status_code == 204

    r = await patch(client, user, h, blocked=False)
    assert r.status_code == 200 and (r.json()["blocked"], r.json()["blocked_reason"]) == (False, None)
    litellm = app.state.litellm
    keep = next(k for k in litellm.keys.values() if k["key"] == made[0]["key"])
    assert keep["blocked"] is False  # the very same key value works again
    assert not any(k["key"] == gone["key"] for k in litellm.keys.values())  # a deleted key stays deleted
    assert len(await audit_rows(app, "user.unblock")) == 1
    other.cookies.clear()
    assert (await login(other, app, USER))["email"] == USER["email"]  # may sign in again


async def test_changing_the_reason_of_a_blocked_user_does_not_touch_the_keys(client, app):
    other = await second_client(app)
    user, _, _ = await make_user(other, app, keys=("one",))
    h = await as_admin(client, app)
    await patch(client, user, h, blocked=True, blocked_reason="первая")
    app.state.litellm.block_calls.clear()
    r = await patch(client, user, h, blocked_reason="вторая")
    assert r.status_code == 200 and r.json()["blocked_reason"] == "вторая"
    assert app.state.litellm.block_calls == []
    assert [e.details.get("reason_changed") for e in await audit_rows(app, "user.block")] == [None, True]


async def test_a_gateway_failure_while_blocking_changes_nothing(client, app):
    other = await second_client(app)
    user, _, made = await make_user(other, app, keys=("one",))
    h = await as_admin(client, app)
    app.state.litellm.unavailable = True
    r = await patch(client, user, h, blocked=True, blocked_reason="r")
    assert r.status_code == 503 and r.json()["detail"] == "Шлюз моделей недоступен"
    app.state.litellm.unavailable = False
    after = await row(app, USER["email"])
    assert (after.blocked, after.blocked_reason) == (False, None)
    assert await audit_rows(app, "user.") == []
    assert (await other.get("/api/me")).status_code == 200  # the session stays
    assert not any(k.get("blocked") for k in app.state.litellm.keys.values())


async def test_a_gateway_refusal_while_unblocking_keeps_the_user_blocked(client, app):
    other = await second_client(app)
    user, _, _ = await make_user(other, app, keys=("one",))
    h = await as_admin(client, app)
    await patch(client, user, h, blocked=True, blocked_reason="r")

    async def refuse(tokens):
        raise LiteLLMError(400, "nope")

    app.state.litellm.unblock_keys = refuse
    r = await patch(client, user, h, blocked=False)
    assert r.status_code == 502
    after = await row(app, USER["email"])
    assert after.blocked is True and after.blocked_reason == "r"
    assert len(await audit_rows(app, "user.unblock")) == 0


async def test_a_failure_on_the_second_key_while_unblocking_blocks_the_enabled_ones_again(client, app):
    other = await second_client(app)
    user, _, _ = await make_user(other, app, keys=("one", "two", "three"))
    tokens = await _tokens(app, user)
    litellm = app.state.litellm
    h = await as_admin(client, app)
    assert (await patch(client, user, h, blocked=True, blocked_reason="r")).status_code == 200
    assert all(litellm.keys[t]["blocked"] for t in tokens)

    real_unblock = litellm.unblock_keys

    async def fail_on_second(toks):
        await real_unblock(toks[:1])  # the first key is enabled, then the gateway refuses the second
        raise LiteLLMError(500, "boom")

    litellm.unblock_keys = fail_on_second
    r = await patch(client, user, h, blocked=False)
    litellm.unblock_keys = real_unblock
    assert r.status_code == 502
    after = await row(app, USER["email"])
    assert after.blocked is True and after.blocked_reason == "r"
    assert all(litellm.keys[t]["blocked"] for t in tokens)  # no key of a blocked user stays enabled
    assert len(await audit_rows(app, "user.unblock")) == 0


# ---- limits -------------------------------------------------------------------------------------------------------


async def test_limits_reach_the_existing_keys_and_the_new_ones(client, app):
    other = await second_client(app)
    user, uh, _ = await make_user(other, app, keys=("one",))
    tokens = await _tokens(app, user)
    litellm = app.state.litellm
    assert (litellm.keys[tokens[0]]["rpm_limit"], litellm.keys[tokens[0]]["tpm_limit"]) == (60, 100000)
    h = await as_admin(client, app)

    r = await patch(client, user, h, rpm_limit=10, tpm_limit=5000, max_keys=2)
    assert r.status_code == 200, r.text
    assert (r.json()["rpm_limit"], r.json()["tpm_limit"], r.json()["max_keys"]) == (10, 5000, 2)
    assert (litellm.keys[tokens[0]]["rpm_limit"], litellm.keys[tokens[0]]["tpm_limit"]) == (10, 5000)

    r = await other.post("/api/keys", json={"alias": "two"}, headers=uh)  # a new key gets them as well
    assert r.status_code == 201
    new_token = next(t for t in await _tokens(app, user) if t not in tokens)
    assert (litellm.keys[new_token]["rpm_limit"], litellm.keys[new_token]["tpm_limit"]) == (10, 5000)
    r = await other.post("/api/keys", json={"alias": "three"}, headers=uh)  # max_keys=2
    assert r.status_code == 409 and r.json()["detail"] == "Достигнут лимит ключей: 2"

    (entry,) = await audit_rows(app, "user.limits")
    assert entry.details == {
        "email": "ivan@g.nsu.ru",
        "changed": {
            "rpm_limit": {"old": None, "new": 10},
            "tpm_limit": {"old": None, "new": 5000},
            "max_keys": {"old": None, "new": 2},
        },
    }


async def test_clearing_a_limit_goes_back_to_the_global_setting(client, app):
    other = await second_client(app)
    user, _, _ = await make_user(other, app, keys=("one",))
    token = (await _tokens(app, user))[0]
    h = await as_admin(client, app)
    await patch(client, user, h, rpm_limit=5, tpm_limit=500)
    r = await patch(client, user, h, rpm_limit=None, tpm_limit=None)
    assert r.status_code == 200 and (r.json()["rpm_limit"], r.json()["tpm_limit"]) == (None, None)
    assert (app.state.litellm.keys[token]["rpm_limit"], app.state.litellm.keys[token]["tpm_limit"]) == (60, 100000)
    r = await patch(client, user, h, rpm_limit=None)  # nothing changes: no new audit row
    assert r.status_code == 200
    assert len(await audit_rows(app, "user.limits")) == 2


async def test_the_limits_of_a_user_follow_the_global_setting_that_an_admin_changed(client, app):
    from app import settings_store

    async with app.state.sessionmaker() as db:
        await settings_store.set(db, "key_rpm_limit", 7, None)
        await db.commit()
    other = await second_client(app)
    user, uh, _ = await make_user(other, app, keys=("one",))
    token = (await _tokens(app, user))[0]
    assert app.state.litellm.keys[token]["rpm_limit"] == 7


async def test_a_gateway_failure_while_setting_limits_changes_nothing(client, app):
    other = await second_client(app)
    user, _, _ = await make_user(other, app, keys=("one",))
    h = await as_admin(client, app)
    app.state.litellm.unavailable = True
    assert (await patch(client, user, h, rpm_limit=3)).status_code == 503
    app.state.litellm.unavailable = False
    assert (await row(app, USER["email"])).rpm_limit is None
    assert await audit_rows(app, "user.limits") == []


# ---- keys of a user -----------------------------------------------------------------------------------------------


async def test_an_admin_sees_the_masked_keys_of_a_user_and_their_usage(client, app):
    other = await second_client(app)
    user, _, made = await make_user(other, app, keys=("one", "two"))
    token = (await _tokens(app, user))[0]
    app.state.litellm.activity[str(user.id)] = Activity(by_key={token: KeyUsage(tokens=40, requests=4)})
    h = await as_admin(client, app)
    r = await client.get(f"/api/admin/users/{user.id}/keys", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()
    assert {k["alias"] for k in items} == {"one", "two"}
    assert set(items[0]) == {"id", "alias", "masked", "created_at", "expires_at", "tokens_30d", "requests_30d"}
    for key in made:
        assert key["key"] not in r.text and key["masked"] in r.text
    used = next(k for k in items if k["tokens_30d"])
    assert (used["tokens_30d"], used["requests_30d"]) == (40, 4)

    app.state.litellm.unavailable = True
    r = await client.get(f"/api/admin/users/{user.id}/keys", headers=h)
    assert r.status_code == 200 and r.headers.get("x-usage-unavailable") == "1"
    assert (await client.get("/api/admin/users/00000000-0000-0000-0000-000000000000/keys", headers=h)).status_code == 404


async def test_an_admin_revokes_a_key_of_another_user_and_it_is_audited(client, app):
    other = await second_client(app)
    user, _, made = await make_user(other, app, keys=("one", "two"))
    h = await as_admin(client, app)
    r = await client.delete(f"/api/admin/users/{user.id}/keys/{made[0]['id']}", headers=h)
    assert r.status_code == 204
    assert len(await _tokens(app, user)) == 1
    assert not any(k["key"] == made[0]["key"] for k in app.state.litellm.keys.values())
    assert any(k["key"] == made[1]["key"] for k in app.state.litellm.keys.values())

    (entry,) = await audit_rows(app, "key.revoke")
    assert (entry.target_type, entry.target, entry.actor_email) == ("key", made[0]["id"], "admin@g.nsu.ru")
    assert entry.details == {"owner": "ivan@g.nsu.ru", "alias": "one", "masked": made[0]["masked"], "by": "admin"}
    assert made[0]["key"] not in str(entry.details)

    again = await client.delete(f"/api/admin/users/{user.id}/keys/{made[0]['id']}", headers=h)
    assert again.status_code == 404
    admin_row = await row(app, ADMIN["email"])
    wrong = await client.delete(f"/api/admin/users/{admin_row.id}/keys/{made[1]['id']}", headers=h)
    assert wrong.status_code == 404  # the key belongs to somebody else
    assert len(await _tokens(app, user)) == 1


async def test_a_gateway_failure_keeps_the_key_and_writes_no_audit(client, app):
    other = await second_client(app)
    user, _, made = await make_user(other, app, keys=("one",))
    h = await as_admin(client, app)
    app.state.litellm.unavailable = True
    r = await client.delete(f"/api/admin/users/{user.id}/keys/{made[0]['id']}", headers=h)
    assert r.status_code == 503
    assert len(await _tokens(app, user)) == 1 and await audit_rows(app, "key.revoke") == []


async def test_the_playground_key_is_neither_listed_nor_revocable_here(client, app):
    other = await second_client(app)
    user, _, _ = await make_user(other, app, keys=("one",))
    async with app.state.sessionmaker() as db:
        pg = ApiKeyRef(user_id=user.id, litellm_token="pg-token", alias="__playground", masked="sk-…pg", is_playground=True)
        db.add(pg)
        await db.commit()
        pg_id = pg.id
    h = await as_admin(client, app)
    items = (await client.get(f"/api/admin/users/{user.id}/keys", headers=h)).json()
    assert [k["alias"] for k in items] == ["one"]
    assert (await client.delete(f"/api/admin/users/{user.id}/keys/{pg_id}", headers=h)).status_code == 404


async def test_the_owner_revoking_their_own_key_is_audited_without_the_secret(client, app):
    user, uh, made = await make_user(client, app, keys=("mine",))
    r = await client.delete(f"/api/keys/{made[0]['id']}", headers=uh)
    assert r.status_code == 204
    (entry,) = await audit_rows(app, "key.revoke")
    assert (entry.actor_email, entry.target_type, entry.target) == ("ivan@g.nsu.ru", "key", made[0]["id"])
    assert entry.details == {"owner": "ivan@g.nsu.ru", "alias": "mine", "masked": made[0]["masked"], "by": "owner"}
    assert made[0]["key"] not in str(entry.details) and entry.ip


async def test_a_refused_own_revoke_writes_no_audit(client, app):
    _, uh, made = await make_user(client, app, keys=("mine",))
    app.state.litellm.unavailable = True
    assert (await client.delete(f"/api/keys/{made[0]['id']}", headers=uh)).status_code == 503
    assert await audit_rows(app, "key.revoke") == []


# ---- the log ------------------------------------------------------------------------------------------------------


async def seed_log(app) -> None:
    now = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        for i, (actor, action, target_type, target, ago) in enumerate(
            (
                ("a@g.nsu.ru", "deployment.create", "deployment", "gemma", 50),
                ("a@g.nsu.ru", "deployment.update", "deployment", "gemma", 40),
                ("b@g.nsu.ru", "user.block", "user", "u-100", 30),
                ("b@g.nsu.ru", "node.wake", "node", "pc01", 20),
                (None, "setting.update", "setting", "wol_enabled", 10),
                ("a@g.nsu.ru", "key.revoke", "key", "k1", 5),
            )
        ):
            db.add(
                AuditLog(
                    at=now - timedelta(minutes=ago), actor_email=actor, action=action, target_type=target_type,
                    target=target, details={"n": i, "text": "=1+1"} if i == 0 else None, ip="10.0.0.1",
                )
            )
        await db.commit()


async def test_audit_is_newest_first_paged_and_filtered(client, app):
    await seed_log(app)
    h = await as_admin(client, app)

    def actions(r):
        assert r.status_code == 200, r.text
        return [e["action"] for e in r.json()["items"]]

    r = await client.get("/api/admin/audit", headers=h)
    assert r.json()["total"] == 6
    assert actions(r) == [
        "key.revoke", "setting.update", "node.wake", "user.block", "deployment.update", "deployment.create",
    ]
    assert set(r.json()["items"][0]) == {"id", "at", "actor_id", "actor_email", "action", "target_type", "target", "details", "ip"}
    assert actions(await client.get("/api/admin/audit?action=deployment.", headers=h)) == [
        "deployment.update", "deployment.create",
    ]
    assert actions(await client.get("/api/admin/audit?action=node", headers=h)) == ["node.wake"]
    assert actions(await client.get("/api/admin/audit?actor=B@G", headers=h)) == ["node.wake", "user.block"]
    assert actions(await client.get("/api/admin/audit?target=GEMMA", headers=h)) == [
        "deployment.update", "deployment.create",
    ]
    assert actions(await client.get("/api/admin/audit?action=%25", headers=h)) == []  # % is not a wildcard
    page = await client.get("/api/admin/audit?limit=2&offset=2", headers=h)
    assert actions(page) == ["node.wake", "user.block"] and page.json()["total"] == 6
    assert (await client.get("/api/admin/audit?limit=0", headers=h)).status_code == 422

    now = datetime.now(UTC)
    since = (now - timedelta(minutes=35)).isoformat().replace("+00:00", "Z")
    until = (now - timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
    assert actions(await client.get("/api/admin/audit", params={"since": since, "until": until}, headers=h)) == [
        "node.wake", "user.block",
    ]
    assert actions(await client.get("/api/admin/audit", params={"since": since}, headers=h))[-1] == "user.block"


async def test_an_inverted_time_range_is_a_422(client, app):
    h = await as_admin(client, app)
    params = {"since": "2026-11-02T10:00:00Z", "until": "2026-11-01T10:00:00Z"}
    for path in ("/api/admin/audit", "/api/admin/audit/export"):
        r = await client.get(path, params=params, headers=h)
        assert r.status_code == 422 and r.json()["detail"] == "Начало периода позже его конца"
    r = await client.get("/api/admin/audit", params={"since": "2026-11-01T10:00:00Z", "until": "2026-11-01T10:00:00Z"}, headers=h)
    assert r.status_code == 200  # equal is a range of one moment


async def test_audit_filters_by_the_id_of_the_actor(client, app):
    h = await as_admin(client, app)
    me = await row(app, ADMIN["email"])
    other = await second_client(app)
    user, _, _ = await make_user(other, app)
    await patch(client, user, h, rpm_limit=9)
    r = await client.get(f"/api/admin/audit?actor={me.id}", headers=h)
    assert [e["action"] for e in r.json()["items"]] == ["user.limits"]
    assert r.json()["items"][0]["actor_id"] == str(me.id)


async def test_audit_csv_has_a_bom_the_filters_and_no_formulas(client, app):
    await seed_log(app)
    h = await as_admin(client, app)
    r = await client.get("/api/admin/audit/export?action=deployment.", headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="audit-' in r.headers["content-disposition"]
    assert r.content.startswith("﻿".encode())
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == ["id", "time", "actor", "action", "target_type", "target", "ip", "details"]
    assert [x[3] for x in rows[1:]] == ["deployment.update", "deployment.create"]  # the filter applies, newest first
    assert rows[2][2] == "a@g.nsu.ru" and rows[2][6] == "10.0.0.1"
    assert "'=1+1" in rows[2][7] or '"text": "=1+1"' in rows[2][7]  # inside a JSON text: not a leading =
    assert not any(cell.startswith(("=", "+", "@")) for line in rows for cell in line)
    assert "x-truncated" not in r.headers


async def test_audit_csv_neutralizes_a_formula_and_marks_a_cut(client, app, monkeypatch):
    async with app.state.sessionmaker() as db:
        for target in ("=HYPERLINK(1)", "@cmd", "ok"):
            db.add(AuditLog(action="x.y", target_type="t", target=target))
        await db.commit()
    h = await as_admin(client, app)
    r = await client.get("/api/admin/audit/export", headers=h)
    cells = {x[5] for x in csv.reader(io.StringIO(r.content.decode("utf-8-sig")))}
    assert {"'=HYPERLINK(1)", "'@cmd", "ok"} <= cells
    monkeypatch.setattr(admin_users, "EXPORT_MAX", 2)
    r = await client.get("/api/admin/audit/export", headers=h)
    assert r.headers["x-truncated"] == "1"
    assert len(list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))) == 3  # header and two rows


async def test_every_admin_change_leaves_exactly_the_rows_it_made(client, app):
    other = await second_client(app)
    user, _, _ = await make_user(other, app, keys=("one",))
    h = await as_admin(client, app)
    await patch(client, user, h, role="admin", rpm_limit=4)
    await patch(client, user, h, blocked=True, blocked_reason="r")
    await patch(client, user, h, blocked=False)
    r = await client.get("/api/admin/audit?action=user.", headers=h)
    assert [e["action"] for e in r.json()["items"]] == ["user.unblock", "user.block", "user.limits", "user.role"]
