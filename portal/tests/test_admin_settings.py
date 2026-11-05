"""Admin «Настройки» (task 2-34): the API, the audit, and that every wired setting changes behaviour without a restart."""

from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app import key_limits, settings_store
from app.config import Settings
from app.models import ApiKeyRef, AuditLog, ControllerTimer, NodeWake, PortalFlag, SystemSetting, User
from app.settings_store import RuntimeSettings
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}
OTHER = {"sub": "usr2", "email": "petr@g.nsu.ru", "name": "Пётр"}
API = "/api/admin/settings"
CHAT = {"model": "gemma-4-e4b", "messages": [{"role": "user", "content": "Привет"}]}


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()
    app.state.backend_status = {"at": "t", "backends": []}


async def as_admin(client, app) -> dict:
    return csrf_headers(await login(client, app, ADMIN))


def another_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


async def audit_rows(db, action="setting.update") -> list[AuditLog]:
    db.expire_all()
    return list((await db.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))).all())


# ---- the list and the access rules ------------------------------------------------------------------------


async def test_list_has_every_setting_by_section_with_value_default_and_source(client, app):
    h = await as_admin(client, app)
    r = await client.get(API, headers=h)
    assert r.status_code == 200, r.text
    items = r.json()
    assert {i["key"] for i in items} == set(settings_store.SETTINGS)
    # the sections come in the order of the page, the registry order inside each
    groups = [i["group"] for i in items]
    assert list(dict.fromkeys(groups)) == list(settings_store.GROUPS)
    assert groups == sorted(groups, key=settings_store.GROUPS.index)
    rpm = next(i for i in items if i["key"] == "key_rpm_limit")
    assert rpm["value"] == rpm["default"] == app.state.settings.key_rpm_limit
    assert (rpm["source"], rpm["overridden"], rpm["type"], rpm["min"], rpm["max"]) == ("env", False, "int", 1, 1_000_000)
    assert rpm["description"] and rpm["restart_required"] is False and rpm["pending_restart"] is False
    mirror = next(i for i in items if i["key"] == "hf_mirror_max_gib")
    assert mirror["restart_required"] is True and mirror["pending_restart"] is False


async def test_only_an_admin_sees_or_changes_settings(client, app):
    assert (await client.get(API)).status_code == 401
    me = await login(client, app, USER)
    h = csrf_headers(me)
    calls = [
        ("GET", API, None),
        ("PUT", f"{API}/key_rpm_limit", {"value": 10}),
        ("DELETE", f"{API}/key_rpm_limit", None),
        ("POST", "/api/admin/key-limits/apply", None),
        ("GET", "/api/admin/key-limits/job", None),
        ("POST", "/api/admin/hf-mirror/restart", None),
    ]
    for method, url, body in calls:
        r = await client.request(method, url, json=body, headers=h)
        assert r.status_code == 403, (method, url)
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(SystemSetting))).all() == []


async def test_changes_need_the_csrf_header(client, app):
    await login(client, app, ADMIN)
    for method, url, body in (
        ("PUT", f"{API}/key_rpm_limit", {"value": 10}),
        ("DELETE", f"{API}/key_rpm_limit", None),
        ("POST", "/api/admin/key-limits/apply", None),
        ("POST", "/api/admin/hf-mirror/restart", None),
    ):
        assert (await client.request(method, url, json=body)).status_code == 403, (method, url)
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(SystemSetting))).all() == []
        assert await audit_rows(db) == []


# ---- PUT / DELETE: validation, storing, audit ---------------------------------------------------------------


async def test_put_stores_the_value_and_audits_old_and_new(client, app, db_session):
    h = await as_admin(client, app)
    r = await client.put(f"{API}/key_rpm_limit", json={"value": 120}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["value"], body["source"], body["overridden"], body["updated_by"]) == (120, "db", True, "admin@g.nsu.ru")
    assert body["default"] == app.state.settings.key_rpm_limit
    listed = {i["key"]: i for i in (await client.get(API, headers=h)).json()}
    assert listed["key_rpm_limit"]["value"] == 120 and listed["key_rpm_limit"]["source"] == "db"
    assert listed["key_tpm_limit"]["source"] == "env"
    (row,) = await audit_rows(db_session)
    assert (row.actor_email, row.target_type, row.target) == ("admin@g.nsu.ru", "setting", "key_rpm_limit")
    assert row.details == {"old": app.state.settings.key_rpm_limit, "new": 120}
    assert row.ip


async def test_delete_goes_back_to_the_env_value_and_audits_the_reset(client, app, db_session):
    h = await as_admin(client, app)
    await client.put(f"{API}/wol_delay_s", json={"value": 5}, headers=h)
    r = await client.delete(f"{API}/wol_delay_s", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["value"], body["source"], body["overridden"]) == (60.0, "env", False)
    assert (await db_session.scalars(select(SystemSetting))).all() == []
    first, second = await audit_rows(db_session)
    assert first.details == {"old": 60.0, "new": 5.0}
    assert second.details == {"old": 5.0, "new": 60.0, "reset": True}
    # a reset of a setting that was never changed is not an error, and not a journal row either
    assert (await client.delete(f"{API}/wol_delay_s", headers=h)).status_code == 200
    assert len(await audit_rows(db_session)) == 2


async def test_saving_the_same_value_again_writes_no_audit_row(client, app, db_session):
    h = await as_admin(client, app)
    assert (await client.put(f"{API}/wol_delay_s", json={"value": 5}, headers=h)).status_code == 200
    again = await client.put(f"{API}/wol_delay_s", json={"value": 5}, headers=h)
    assert again.status_code == 200 and again.json()["value"] == 5
    assert len(await audit_rows(db_session)) == 1
    # a value equal to the env default is still a change when nothing was stored before: it becomes an override
    await client.put(f"{API}/key_rpm_limit", json={"value": app.state.settings.key_rpm_limit}, headers=h)
    assert len(await audit_rows(db_session)) == 2


@pytest.mark.parametrize(
    ("key", "value", "text"),
    [
        ("key_rpm_limit", 0, "не меньше 1"),
        ("key_rpm_limit", 1_000_001, "не больше 1000000"),
        ("key_rpm_limit", 2.5, "целое число"),
        ("key_rpm_limit", "10", "нужно число"),
        ("key_rpm_limit", True, "нужно число"),
        ("key_rpm_limit", None, "нужно число"),
        ("max_keys_per_user", 0, "не меньше 1"),
        ("wol_enabled", 1, "да/нет"),
        ("wol_enabled", "yes", "да/нет"),
        ("wol_delay_s", -1, "не меньше 0"),
        ("wol_max_tries", 101, "не больше 100"),
        ("model_cache_idle_s", 59, "не меньше 60"),
        ("metrics_retention_days", 366, "не больше 365"),
        ("hf_mirror_max_gib", 0, "не меньше 1"),
    ],
)
async def test_put_refuses_a_wrong_value_in_russian_and_changes_nothing(client, app, db_session, key, value, text):
    h = await as_admin(client, app)
    r = await client.put(f"{API}/{key}", json={"value": value}, headers=h)
    assert r.status_code == 422, r.text
    assert text in r.json()["detail"] and key in r.json()["detail"]
    assert (await db_session.scalars(select(SystemSetting))).all() == []
    assert await audit_rows(db_session) == []


async def test_an_unknown_key_is_404_and_a_body_without_value_is_422(client, app, db_session):
    h = await as_admin(client, app)
    r = await client.put(f"{API}/litellm_master_key", json={"value": "x"}, headers=h)
    assert (r.status_code, r.json()["detail"]) == (404, "Неизвестная настройка")
    assert (await client.delete(f"{API}/nope", headers=h)).status_code == 404
    assert (await client.put(f"{API}/key_rpm_limit", json={}, headers=h)).status_code == 422
    assert (await db_session.scalars(select(SystemSetting))).all() == []
    assert await audit_rows(db_session) == []


async def test_a_float_setting_takes_a_whole_number_and_a_bool_takes_a_bool(client, app):
    h = await as_admin(client, app)
    assert (await client.put(f"{API}/wol_delay_s", json={"value": 30}, headers=h)).json()["value"] == 30.0
    assert (await client.put(f"{API}/wol_enabled", json={"value": False}, headers=h)).json()["value"] is False


# ---- the cache -----------------------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


async def test_runtime_settings_cache_for_the_ttl_and_invalidate(app, db_session):
    clock = Clock()
    rt = RuntimeSettings(app.state, ttl=5.0, clock=clock)
    default = app.state.settings.key_rpm_limit
    assert await rt.get("key_rpm_limit") == default
    await settings_store.set(db_session, "key_rpm_limit", 7, None)
    await db_session.commit()
    clock.now += 4.9
    assert await rt.get("key_rpm_limit") == default  # still the cached value: a hot path does not query
    clock.now += 0.2
    assert await rt.get("key_rpm_limit") == 7
    await settings_store.set(db_session, "key_rpm_limit", 8, None)
    await db_session.commit()
    rt.invalidate()
    assert await rt.get_many("key_rpm_limit", "wol_enabled") == {"key_rpm_limit": 8, "wol_enabled": True}
    with pytest.raises(settings_store.UnknownSetting):
        await rt.get("nope")


async def test_runtime_settings_keep_the_last_values_when_the_database_fails(app):
    clock = Clock()
    broken = {"on": False}

    def sessionmaker():
        if broken["on"]:
            raise OSError("db down")
        return app.state.sessionmaker()

    state = type("S", (), {"sessionmaker": staticmethod(sessionmaker), "settings": app.state.settings})()
    rt = RuntimeSettings(state, ttl=1.0, clock=clock)
    broken["on"] = True
    assert await rt.get("max_keys_per_user") == app.state.settings.max_keys_per_user  # the env default before a read
    broken["on"] = False
    clock.now += 2
    assert await rt.get("max_keys_per_user") == app.state.settings.max_keys_per_user
    broken["on"] = True
    clock.now += 2
    assert await rt.get("wol_max_tries") == app.state.settings.wol_max_tries  # the last known values stay in use


async def test_live_falls_back_to_the_env_without_a_runtime():
    state = type("S", (), {"settings": Settings(key_tpm_limit=777)})()
    assert await settings_store.live(state, "key_tpm_limit") == 777
    assert await settings_store.live_many(state, ["key_tpm_limit", "wol_enabled"]) == {
        "key_tpm_limit": 777, "wol_enabled": True,
    }


# ---- behaviour without a restart ------------------------------------------------------------------------------


async def make_key(client, headers, alias="ключ"):
    return await client.post("/api/keys", json={"alias": alias, "expires_days": None}, headers=headers)


async def test_max_keys_per_user_is_read_at_every_create(client, app):
    admin = await as_admin(client, app)
    user_client = another_client(app)
    async with user_client:
        me = csrf_headers(await login(user_client, app, USER))
        for i in range(app.state.settings.max_keys_per_user):
            assert (await make_key(user_client, me, f"k{i}")).status_code == 201
        r = await make_key(user_client, me, "лишний")
        assert (r.status_code, r.json()["detail"]) == (409, f"Достигнут лимит ключей: {app.state.settings.max_keys_per_user}")
        await client.put(f"{API}/max_keys_per_user", json={"value": 6}, headers=admin)
        assert (await make_key(user_client, me, "шестой")).status_code == 201
        await client.put(f"{API}/max_keys_per_user", json={"value": 1}, headers=admin)
        r = await make_key(user_client, me, "седьмой")
        assert (r.status_code, r.json()["detail"]) == (409, "Достигнут лимит ключей: 1")
        await client.delete(f"{API}/max_keys_per_user", headers=admin)  # back to the .env value
        limit = app.state.settings.max_keys_per_user  # the user has six keys now: over the .env limit
        r = await make_key(user_client, me, "после сброса")
        assert (r.status_code, r.json()["detail"]) == (409, f"Достигнут лимит ключей: {limit}")


async def test_new_keys_get_the_limits_of_the_panel(client, app):
    admin = await as_admin(client, app)
    fake = app.state.litellm
    await client.put(f"{API}/key_rpm_limit", json={"value": 11}, headers=admin)
    await client.put(f"{API}/key_tpm_limit", json={"value": 2222}, headers=admin)
    assert (await make_key(client, admin, "a")).status_code == 201
    (rec,) = fake.keys.values()
    assert (rec["rpm_limit"], rec["tpm_limit"]) == (11, 2222)
    await client.delete(f"{API}/key_rpm_limit", headers=admin)
    assert (await make_key(client, admin, "b")).status_code == 201
    assert [r["rpm_limit"] for r in fake.keys.values()] == [11, app.state.settings.key_rpm_limit]


async def test_the_playground_key_gets_the_limits_of_the_panel(client, app):
    admin = await as_admin(client, app)
    await client.put(f"{API}/key_rpm_limit", json={"value": 13}, headers=admin)
    await client.put(f"{API}/key_tpm_limit", json={"value": 3333}, headers=admin)
    r = await client.post("/api/playground/chat", json=CHAT, headers=admin)
    assert r.status_code == 200, r.text
    (rec,) = app.state.litellm.keys.values()
    assert (rec["rpm_limit"], rec["tpm_limit"]) == (13, 3333)


async def test_the_nodes_page_shows_the_wake_attempt_limit_of_the_panel(client, app):
    k8s = app.state.k8s
    k8s.add_node("llm-main", accel="cpu", internal_ip="192.168.3.21", is_main=True)
    k8s.add_node("pc01", accel="cpu", internal_ip="192.168.0.193", ready=False)
    async with app.state.sessionmaker() as db:
        db.add_all(
            [
                NodeWake(node_name="pc01", mac="00:1a:2b:3c:4d:5e", ip="192.168.0.193", prefix=24),
                ControllerTimer(key="wake:pc01", since=datetime(2026, 11, 4, 12, 0, tzinfo=UTC).timestamp(), members="2"),
            ]
        )
        await db.commit()
    h = await as_admin(client, app)
    node = lambda body: next(n for n in body["nodes"] if n["name"] == "pc01")  # noqa: E731
    assert node((await client.get("/api/admin/nodes", headers=h)).json())["wake"]["max_tries"] == 6
    await client.put(f"{API}/wol_max_tries", json={"value": 3}, headers=h)
    assert node((await client.get("/api/admin/nodes", headers=h)).json())["wake"]["max_tries"] == 3


# ---- «Применить к существующим ключам» -------------------------------------------------------------------------


async def seed_keys(app, fake):
    """Ivan has two keys, Petr one (with an own request limit of 5); each also a playground key for Ivan."""
    async with app.state.sessionmaker() as db:
        ivan = User(email="ivan@g.nsu.ru", sub="u1", role="user")
        petr = User(email="petr@g.nsu.ru", sub="u2", role="user", rpm_limit=5)
        db.add_all([ivan, petr])
        await db.flush()
        for user, alias, playground in ((ivan, "a", False), (ivan, "__playground", True), (petr, "b", False)):
            gen = await fake.generate_key(str(user.id), f"{user.email}-{alias}", None, 1, 1, {})
            db.add(ApiKeyRef(user_id=user.id, litellm_token=gen.token, alias=alias, masked="m", is_playground=playground))
        await db.commit()
        return {u.email: str(u.id) for u in (ivan, petr)}


async def wait_job(app):
    task = getattr(app.state, "key_limits_task", None)
    if task is not None:
        await task


async def test_apply_updates_the_existing_keys_but_keeps_a_users_own_limit(client, app, db_session):
    fake = app.state.litellm
    await seed_keys(app, fake)
    h = await as_admin(client, app)
    assert (await client.get("/api/admin/key-limits/job", headers=h)).json() == {"state": "none"}
    await client.put(f"{API}/key_rpm_limit", json={"value": 40}, headers=h)
    await client.put(f"{API}/key_tpm_limit", json={"value": 9000}, headers=h)
    r = await client.post("/api/admin/key-limits/apply", headers=h)
    assert r.status_code == 202, r.text
    assert (r.json()["rpm_limit"], r.json()["tpm_limit"]) == (40, 9000)
    await wait_job(app)
    job = (await client.get("/api/admin/key-limits/job", headers=h)).json()
    assert (job["state"], job["total"], job["done"], job["failed"]) == ("done", 3, 3, 0)
    assert job["started_at"] and job["finished_at"] and job["error"] is None
    limits = sorted((rec["alias"], rec["rpm_limit"], rec["tpm_limit"]) for rec in fake.keys.values())
    assert limits == [
        ("ivan@g.nsu.ru-__playground", 40, 9000),
        ("ivan@g.nsu.ru-a", 40, 9000),
        ("petr@g.nsu.ru-b", 1, 9000),  # Petr's own request limit is not touched, his token limit follows the panel
    ]
    (row,) = await audit_rows(db_session, "setting.apply_key_limits")
    assert row.details == {"rpm_limit": 40, "tpm_limit": 9000}


async def test_apply_skips_a_key_that_has_both_limits_of_its_own_and_does_not_fail_on_a_key_gone_from_the_gateway(client, app):
    fake = app.state.litellm
    await seed_keys(app, fake)
    async with app.state.sessionmaker() as db:
        petr = await db.scalar(select(User).where(User.email == "petr@g.nsu.ru"))
        petr.tpm_limit = 7  # Petr owns both limits now: nothing to send for his key
        ivan_key = await db.scalar(select(ApiKeyRef).join(User).where(User.email == "ivan@g.nsu.ru", ApiKeyRef.alias == "a"))
        del fake.keys[ivan_key.litellm_token]  # gone from LiteLLM, still a row here
        await db.commit()
    h = await as_admin(client, app)
    assert (await client.post("/api/admin/key-limits/apply", headers=h)).status_code == 202
    await wait_job(app)
    job = (await client.get("/api/admin/key-limits/job", headers=h)).json()
    assert (job["state"], job["total"], job["done"], job["failed"]) == ("done", 2, 2, 0)
    assert len(fake.key_limit_updates) == 2


async def test_apply_counts_a_refused_key_and_goes_on(client, app):
    fake = app.state.litellm
    await seed_keys(app, fake)
    async with app.state.sessionmaker() as db:
        row = await db.scalar(select(ApiKeyRef).where(ApiKeyRef.alias == "a"))
        fake.key_limit_failures.add(row.litellm_token)
    h = await as_admin(client, app)
    await client.post("/api/admin/key-limits/apply", headers=h)
    await wait_job(app)
    job = (await client.get("/api/admin/key-limits/job", headers=h)).json()
    assert (job["state"], job["total"], job["done"], job["failed"]) == ("done", 3, 2, 1)


async def test_apply_stops_when_the_gateway_is_down(client, app):
    fake = app.state.litellm
    await seed_keys(app, fake)
    fake.unavailable = True
    h = await as_admin(client, app)
    assert (await client.post("/api/admin/key-limits/apply", headers=h)).status_code == 202
    await wait_job(app)
    job = (await client.get("/api/admin/key-limits/job", headers=h)).json()
    assert job["state"] == "failed" and job["error"] == "Шлюз моделей недоступен" and job["finished_at"]
    fake.unavailable = False
    assert (await client.post("/api/admin/key-limits/apply", headers=h)).status_code == 202  # the lock was released
    await wait_job(app)
    assert (await client.get("/api/admin/key-limits/job", headers=h)).json()["state"] == "done"


async def test_a_second_apply_while_one_runs_is_409(client, app):
    h = await as_admin(client, app)
    assert await app.state.redis.set(key_limits.LOCK_KEY, "1", ex=60)  # a job of another worker holds the lock
    r = await client.post("/api/admin/key-limits/apply", headers=h)
    assert (r.status_code, r.json()["detail"]) == (409, "Обновление ключей уже идёт")
    async with app.state.sessionmaker() as db:
        assert await audit_rows(db, "setting.apply_key_limits") == []


async def test_no_key_is_touched_when_the_audit_row_cannot_be_written(client, app, monkeypatch):
    fake = app.state.litellm
    await seed_keys(app, fake)
    h = await as_admin(client, app)

    async def broken(*args, **kwargs):
        raise RuntimeError("the journal is down")

    monkeypatch.setattr("app.api.admin_settings.audit.record", broken)
    with pytest.raises(RuntimeError):
        await client.post("/api/admin/key-limits/apply", headers=h)
    await wait_job(app)
    assert fake.key_limit_updates == []  # the job starts only after the audit row is committed
    assert await app.state.redis.get(key_limits.LOCK_KEY) is None  # the lock is free again
    assert (await client.get("/api/admin/key-limits/job", headers=h)).json() == {"state": "none"}


# ---- the mirror limit: applied after a restart of the mirror --------------------------------------------------


async def test_the_mirror_limit_waits_for_a_restart_of_the_mirror(client, app, db_session):
    h = await as_admin(client, app)
    k8s = app.state.k8s
    r = await client.put(f"{API}/hf_mirror_max_gib", json={"value": 800}, headers=h)
    body = r.json()
    assert (body["value"], body["restart_required"], body["pending_restart"]) == (800, True, True)
    assert k8s.config_maps["hf-mirror-settings"] == {"max_gib": "800"}  # what the mirror reads at its next start
    assert k8s.restarted == []  # saving never restarts it
    listed = {i["key"]: i for i in (await client.get(API, headers=h)).json()}
    assert listed["hf_mirror_max_gib"]["pending_restart"] is True

    r = await client.post("/api/admin/hf-mirror/restart", headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["value"], r.json()["pending_restart"]) == (800, False)
    assert k8s.restarted == ["hf-mirror"]
    assert (await db_session.get(PortalFlag, "hf_mirror_applied_gib")).value == "800"
    (row,) = await audit_rows(db_session, "hf_mirror.restart")
    assert (row.actor_email, row.details) == ("admin@g.nsu.ru", {"max_gib": 800})

    # back to the .env value: the override is CLEARED in the ConfigMap (an empty value, so the mirror takes the .env);
    # the running mirror still has 800, so it is pending again
    r = await client.delete(f"{API}/hf_mirror_max_gib", headers=h)
    assert (r.json()["value"], r.json()["pending_restart"]) == (app.state.settings.hf_mirror_max_gib, True)
    assert k8s.config_maps["hf-mirror-settings"] == {"max_gib": ""}

    # the restart applies the cleared override: the pod is on the .env value, nothing is pending, the flag is gone
    r = await client.post("/api/admin/hf-mirror/restart", headers=h)
    assert (r.json()["value"], r.json()["pending_restart"]) == (app.state.settings.hf_mirror_max_gib, False)
    assert await db_session.get(PortalFlag, "hf_mirror_applied_gib") is None
    assert k8s.config_maps["hf-mirror-settings"] == {"max_gib": ""}
    # the admin edits the .env and re-runs the installer: the portal follows the .env, still nothing pending
    app.state.settings.hf_mirror_max_gib = 700
    listed = {i["key"]: i for i in (await client.get(API, headers=h)).json()}
    assert (listed["hf_mirror_max_gib"]["value"], listed["hf_mirror_max_gib"]["pending_restart"]) == (700, False)


async def test_the_mirror_config_is_empty_while_the_limit_was_never_changed(client, app):
    h = await as_admin(client, app)
    await client.put(f"{API}/hf_mirror_max_gib", json={"value": 800}, headers=h)
    await client.put(f"{API}/hf_mirror_max_gib", json={"value": 800}, headers=h)  # no change
    assert app.state.k8s.config_maps["hf-mirror-settings"] == {"max_gib": "800"}
    await client.delete(f"{API}/hf_mirror_max_gib", headers=h)
    assert app.state.k8s.config_maps["hf-mirror-settings"] == {"max_gib": ""}


async def test_saving_the_mirror_limit_works_while_the_cluster_is_down(client, app):
    h = await as_admin(client, app)
    app.state.k8s.unavailable = True
    r = await client.put(f"{API}/hf_mirror_max_gib", json={"value": 900}, headers=h)
    assert r.status_code == 200 and r.json()["value"] == 900 and r.json()["pending_restart"] is True
    r = await client.post("/api/admin/hf-mirror/restart", headers=h)
    assert (r.status_code, r.json()["detail"]) == (503, "Кластер недоступен")
    app.state.k8s.unavailable = False
    listed = {i["key"]: i for i in (await client.get(API, headers=h)).json()}
    assert listed["hf_mirror_max_gib"]["pending_restart"] is True  # nothing was restarted
    r = await client.post("/api/admin/hf-mirror/restart", headers=h)
    assert r.status_code == 200 and app.state.k8s.config_maps["hf-mirror-settings"] == {"max_gib": "900"}


async def test_restart_of_a_mirror_that_is_not_in_the_cluster_is_404(client, app, db_session):
    h = await as_admin(client, app)
    app.state.k8s.missing_deployments.add("hf-mirror")
    r = await client.post("/api/admin/hf-mirror/restart", headers=h)
    assert r.status_code == 404 and "Зеркало" in r.json()["detail"]
    assert await db_session.get(PortalFlag, "hf_mirror_applied_gib") is None
    assert await audit_rows(db_session, "hf_mirror.restart") == []
