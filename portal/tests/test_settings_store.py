"""app.settings_store: the registry, the env default, validation, storing with an audit row."""

import math

import pytest
from sqlalchemy import select

from app import settings_store as ss
from app.api.schemas_admin_ops import AuditEntryOut, SystemSettingOut
from app.config import Settings
from app.models import AuditLog, SystemSetting, User

CONTRACT_KEYS = {
    "key_rpm_limit", "key_tpm_limit", "max_keys_per_user", "wol_enabled", "wol_delay_s", "wol_max_tries",
    "hf_mirror_max_gib", "model_cache_idle_s", "node_auto_update", "metrics_retention_days",
    "session_max_days", "node_approval_required", "node_key_migration_days",
}


# ---- the registry (no database) ------------------------------------------------------------------------


def test_the_registry_has_the_contract_keys():
    assert set(ss.SETTINGS) == CONTRACT_KEYS
    assert ss.SETTINGS["hf_mirror_max_gib"].restart_required
    assert [k for k, s in ss.SETTINGS.items() if s.restart_required] == ["hf_mirror_max_gib"]
    assert all(s.description_ru for s in ss.SETTINGS.values())


@pytest.mark.parametrize("key", sorted(CONTRACT_KEYS))
def test_every_env_default_is_a_valid_value(key):
    spec = ss.SETTINGS[key]
    default = spec.default(Settings())
    assert spec.coerce(default) == default  # a default the panel would refuse could not be put back


def test_defaults_come_from_the_env_settings_or_the_fallback():
    s = Settings(key_rpm_limit=30, wol_enabled=False, wol_delay_s=15, max_keys_per_user=9)
    assert [ss.SETTINGS[k].default(s) for k in ("key_rpm_limit", "wol_enabled", "wol_delay_s", "max_keys_per_user")] == [
        30, False, 15.0, 9,
    ]
    # no env setting of this name yet: the fixed fallback (default retention is a week)
    assert ss.SETTINGS["metrics_retention_days"].default(s) == 7
    assert ss.SETTINGS["hf_mirror_max_gib"].default(s) == 500  # HF_MIRROR_MAX_GIB unset
    assert ss.SETTINGS["node_auto_update"].default(s) is False
    assert ss.SETTINGS["model_cache_idle_s"].default(s) == 600


def test_the_mirror_limit_default_follows_the_env(monkeypatch):
    assert ss.SETTINGS["hf_mirror_max_gib"].default(Settings(hf_mirror_max_gib=2000)) == 2000
    monkeypatch.setenv("HF_MIRROR_MAX_GIB", "2000")
    assert Settings().hf_mirror_max_gib == 2000
    assert ss.SETTINGS["hf_mirror_max_gib"].default(Settings()) == 2000


@pytest.mark.docker
async def test_the_mirror_limit_default_is_the_env_value_in_the_store(app, db_session):
    env = Settings(hf_mirror_max_gib=2000)
    assert await ss.get(db_session, "hf_mirror_max_gib", env) == 2000
    assert (await ss.get_all(db_session, env))["hf_mirror_max_gib"] == 2000
    await ss.set(db_session, "hf_mirror_max_gib", 800, None, settings=env)
    await db_session.commit()
    assert await ss.get(db_session, "hf_mirror_max_gib", env) == 800
    assert await ss.reset(db_session, "hf_mirror_max_gib", None, settings=env) == 2000


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("key_rpm_limit", "60", "нужно число"),
        ("key_rpm_limit", True, "нужно число"),
        ("key_rpm_limit", None, "нужно число"),
        ("key_rpm_limit", 1.5, "нужно целое число"),
        ("key_rpm_limit", math.nan, "нужно конечное число"),
        ("key_rpm_limit", math.inf, "нужно конечное число"),
        ("key_rpm_limit", 0, "не меньше 1"),
        ("key_rpm_limit", 1_000_001, "не больше 1000000"),
        ("wol_enabled", 1, "да/нет"),
        ("wol_enabled", "true", "да/нет"),
        ("wol_delay_s", -1, "не меньше 0"),
        ("wol_max_tries", 101, "не больше 100"),
        ("metrics_retention_days", 0, "не меньше 1"),
        ("model_cache_idle_s", 5, "не меньше 60"),
    ],
)
def test_a_wrong_value_is_refused_in_russian(key, value, message):
    with pytest.raises(ss.InvalidSetting, match=message):
        ss.SETTINGS[key].coerce(value)


def test_accepted_values_take_the_type_of_the_setting():
    assert ss.SETTINGS["wol_delay_s"].coerce(30) == 30.0 and isinstance(ss.SETTINGS["wol_delay_s"].coerce(30), float)
    assert ss.SETTINGS["key_rpm_limit"].coerce(120.0) == 120 and isinstance(ss.SETTINGS["key_rpm_limit"].coerce(120.0), int)
    assert ss.SETTINGS["wol_enabled"].coerce(False) is False
    assert ss.SETTINGS["wol_max_tries"].coerce(0) == 0  # 0 = never wake automatically
    assert ss.SETTINGS["key_rpm_limit"].coerce(1) == 1 and ss.SETTINGS["key_rpm_limit"].coerce(1_000_000) == 1_000_000


def test_an_unknown_key_is_refused():
    with pytest.raises(ss.UnknownSetting):
        ss.spec_of("admin_emails")


# ---- the database --------------------------------------------------------------------------------------


@pytest.fixture
def env():
    return Settings(key_rpm_limit=42, wol_delay_s=20)


async def admin(db) -> User:
    user = User(sub="adm", email="admin@g.nsu.ru", role="admin")
    db.add(user)
    await db.commit()
    return user


async def audit_rows(db) -> list[AuditLog]:
    return list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())


@pytest.mark.docker
async def test_get_returns_the_env_default_without_an_override(app, db_session, env):
    assert await ss.get(db_session, "key_rpm_limit", env) == 42
    assert await ss.get(db_session, "wol_delay_s", env) == 20.0
    assert await ss.get(db_session, "metrics_retention_days", env) == 7
    with pytest.raises(ss.UnknownSetting):
        await ss.get(db_session, "nope", env)


@pytest.mark.docker
async def test_get_without_settings_reads_the_process_env(app, db_session, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("KEY_TPM_LIMIT", "777")
    get_settings.cache_clear()
    try:
        assert await ss.get(db_session, "key_tpm_limit") == 777
    finally:
        get_settings.cache_clear()


@pytest.mark.docker
async def test_set_stores_audits_and_get_returns_the_value(app, db_session, env):
    who = await admin(db_session)
    assert await ss.set(db_session, "key_rpm_limit", 120, who, ip="10.0.0.9", settings=env) == 120
    await db_session.commit()

    assert await ss.get(db_session, "key_rpm_limit", env) == 120  # the override beats the env default
    row = await db_session.get(SystemSetting, "key_rpm_limit")
    assert (row.value, row.updated_by) == (120, "admin@g.nsu.ru") and row.updated_at is not None
    (entry,) = await audit_rows(db_session)
    assert (entry.action, entry.target_type, entry.target) == ("setting.update", "setting", "key_rpm_limit")
    assert (entry.actor_id, entry.actor_email, entry.ip) == (who.id, "admin@g.nsu.ru", "10.0.0.9")
    assert entry.details == {"old": 42, "new": 120}  # the old value is the env default it replaced

    await ss.set(db_session, "key_rpm_limit", 90, who, settings=env)  # a second change replaces the row
    await db_session.commit()
    assert await ss.get(db_session, "key_rpm_limit", env) == 90
    assert len((await db_session.scalars(select(SystemSetting))).all()) == 1
    assert (await audit_rows(db_session))[-1].details == {"old": 120, "new": 90}


@pytest.mark.docker
async def test_set_does_not_commit(app, db_session, env):
    who = await admin(db_session)
    await ss.set(db_session, "wol_enabled", False, who, settings=env)
    async with app.state.sessionmaker() as other:
        assert await ss.get(other, "wol_enabled", env) is True
        assert (await audit_rows(other)) == []
    await db_session.commit()
    async with app.state.sessionmaker() as other:
        assert await ss.get(other, "wol_enabled", env) is False


@pytest.mark.docker
async def test_a_refused_value_stores_and_audits_nothing(app, db_session, env):
    who = await admin(db_session)
    with pytest.raises(ss.InvalidSetting):
        await ss.set(db_session, "key_rpm_limit", 0, who, settings=env)
    with pytest.raises(ss.UnknownSetting):
        await ss.set(db_session, "database_url", "x", who, settings=env)
    await db_session.commit()
    assert (await db_session.scalars(select(SystemSetting))).all() == []
    assert await audit_rows(db_session) == []


@pytest.mark.docker
async def test_set_by_the_system_has_no_actor(app, db_session, env):
    await ss.set(db_session, "node_auto_update", True, None, settings=env)
    await db_session.commit()
    row = await db_session.get(SystemSetting, "node_auto_update")
    assert row.value is True and row.updated_by is None
    assert (await audit_rows(db_session))[0].actor_email is None


@pytest.mark.docker
async def test_reset_goes_back_to_the_env_default(app, db_session, env):
    who = await admin(db_session)
    await ss.set(db_session, "key_rpm_limit", 120, who, settings=env)
    await db_session.commit()
    assert await ss.reset(db_session, "key_rpm_limit", who, settings=env) == 42
    await db_session.commit()
    assert await ss.get(db_session, "key_rpm_limit", env) == 42
    assert await db_session.get(SystemSetting, "key_rpm_limit") is None
    assert (await audit_rows(db_session))[-1].details == {"old": 120, "new": 42, "reset": True}
    await ss.reset(db_session, "key_rpm_limit", who, settings=env)  # nothing stored: still fine
    with pytest.raises(ss.UnknownSetting):
        await ss.reset(db_session, "nope", who, settings=env)


@pytest.mark.docker
async def test_get_all_lists_every_setting_with_its_effective_value(app, db_session, env):
    await ss.set(db_session, "max_keys_per_user", 12, None, settings=env)
    await db_session.commit()
    values = await ss.get_all(db_session, env)
    assert set(values) == CONTRACT_KEYS
    assert values["max_keys_per_user"] == 12 and values["key_rpm_limit"] == 42 and values["metrics_retention_days"] == 7


@pytest.mark.docker
async def test_a_stored_row_the_registry_no_longer_accepts_falls_back_to_the_default(app, db_session, env):
    db_session.add(SystemSetting(key="key_rpm_limit", value=-5))  # written by an older version with looser limits
    db_session.add(SystemSetting(key="wol_enabled", value="yes"))
    await db_session.commit()
    assert await ss.get(db_session, "key_rpm_limit", env) == 42
    assert (await ss.get_all(db_session, env))["wol_enabled"] is True


# ---- the out-models ------------------------------------------------------------------------------------


@pytest.mark.docker
async def test_out_models_describe_a_setting_and_an_audit_entry(app, db_session, env):
    who = await admin(db_session)
    await ss.set(db_session, "hf_mirror_max_gib", 800, who, settings=env)
    await db_session.commit()
    spec = ss.SETTINGS["hf_mirror_max_gib"]
    row = await db_session.get(SystemSetting, "hf_mirror_max_gib")
    out = SystemSettingOut.of(spec, 800, spec.default(env), row)
    assert out.model_dump(mode="json", exclude={"updated_at"}) == {
        "key": "hf_mirror_max_gib", "type": "int", "value": 800, "default": 500, "overridden": True,
        "source": "db", "group": "Модели и кэш", "pending_restart": False, "min": 1.0, "max": 1_000_000.0, "description": spec.description_ru, "restart_required": True,
        "updated_by": "admin@g.nsu.ru",
    }
    plain = SystemSettingOut.of(ss.SETTINGS["wol_enabled"], True, True, None)
    assert (plain.source, plain.group) == ("env", "Ноды и Wake-on-LAN")
    assert (plain.overridden, plain.updated_at, plain.updated_by, plain.restart_required) == (False, None, None, False)

    entry = AuditEntryOut.of((await audit_rows(db_session))[0])
    assert entry.action == "setting.update" and entry.actor_email == "admin@g.nsu.ru"
    assert entry.details == {"old": 500, "new": 800} and entry.at is not None
