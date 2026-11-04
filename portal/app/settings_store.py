"""Runtime settings changed in the admin panel (table `system_settings`), on top of the env `Settings`.

`SETTINGS` is the registry: only a key listed here can be stored. `get` returns the value from the database, or the
default of the env (`Settings.<key>` when it exists, else the fixed `fallback` of the spec). `set` validates, stores and
writes the audit row; like `audit.record` it does not commit, the caller does. A setting that applies `live` is read
from the database where it is used; one that needs a restart is only read by its process at start, so the value
changed here takes effect after the restart of that component (the panel says so).
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.config import Settings, get_settings
from app.models import SystemSetting, User

LIVE = "live"
RESTART = "restart"


class UnknownSetting(KeyError):
    """The key is not in `SETTINGS`."""


class InvalidSetting(ValueError):
    """The value does not fit the setting; the text is Russian, for the admin."""


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: Literal["int", "float", "bool"]
    fallback: Any  # the default when `Settings` has no attribute `key`
    description_ru: str
    min: float | None = None
    max: float | None = None
    applies: Literal["live", "restart"] = LIVE

    @property
    def restart_required(self) -> bool:
        return self.applies == RESTART

    def default(self, settings: Settings) -> Any:
        """The value without an override: the env setting of the same name, else the fixed fallback."""
        return self.coerce(getattr(settings, self.key, self.fallback), strict=False)

    def coerce(self, value: Any, strict: bool = True) -> Any:
        """The value as the setting's type. `strict` (a value from the panel): bool only for bool, a whole number for
        int, no NaN/inf, within min..max, InvalidSetting otherwise. Not strict (a default from the env): converted."""
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            if strict:
                raise InvalidSetting(f"«{self.key}»: нужно значение да/нет")
            return bool(value)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise InvalidSetting(f"«{self.key}»: нужно число")
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidSetting(f"«{self.key}»: нужно конечное число")
        if self.type == "int":
            if strict and isinstance(value, float) and not value.is_integer():
                raise InvalidSetting(f"«{self.key}»: нужно целое число")
            value = int(value)
        else:
            value = float(value)
        if strict:
            if self.min is not None and value < self.min:
                raise InvalidSetting(f"«{self.key}»: не меньше {_num(self.min)}")
            if self.max is not None and value > self.max:
                raise InvalidSetting(f"«{self.key}»: не больше {_num(self.max)}")
        return value


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


SETTINGS: dict[str, SettingSpec] = {
    s.key: s
    for s in (
        SettingSpec(
            "key_rpm_limit", "int", 60,
            "Запросов в минуту на один ключ API (общий лимит, у пользователя может быть свой)",
            min=1, max=1_000_000,
        ),
        SettingSpec(
            "key_tpm_limit", "int", 100_000,
            "Токенов в минуту на один ключ API (общий лимит, у пользователя может быть свой)",
            min=1, max=1_000_000_000,
        ),
        SettingSpec(
            "max_keys_per_user", "int", 5,
            "Сколько ключей API может создать пользователь (у пользователя может быть своё число)",
            min=1, max=1000,
        ),
        SettingSpec(
            "wol_enabled", "bool", True,
            "Будить выключенные ноды автоматически (Wake-on-LAN); кнопка «Разбудить» работает всегда",
        ),
        SettingSpec(
            "wol_delay_s", "float", 60.0,
            "Через сколько секунд после падения ноды отправить первый пакет пробуждения", min=0, max=86_400,
        ),
        SettingSpec(
            "wol_max_tries", "int", 6, "Сколько раз автоматически будить ноду, прежде чем сдаться", min=0, max=100
        ),
        SettingSpec(
            "hf_mirror_max_gib", "int", 500,
            "Предел кэша зеркала Hugging Face на главном сервере, ГиБ; применяется после перезапуска зеркала",
            min=1, max=1_000_000, applies=RESTART,
        ),
        SettingSpec(
            "model_cache_idle_s", "int", 600,
            "Через сколько секунд простоя ноды без развёртывания удалять с неё файлы моделей", min=60, max=2_592_000,
        ),
        SettingSpec(
            "node_auto_update", "bool", False, "Обновлять ноды автоматически, не дожидаясь команды администратора"
        ),
        SettingSpec(
            "metrics_retention_days", "int", 7, "Сколько дней хранить замеры мониторинга", min=1, max=365
        ),
    )
}


def spec_of(key: str) -> SettingSpec:
    try:
        return SETTINGS[key]
    except KeyError:
        raise UnknownSetting(key) from None


def _env(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def _stored(spec: SettingSpec, row: SystemSetting | None, env: Settings) -> Any:
    if row is not None:
        try:
            return spec.coerce(row.value)
        except InvalidSetting:
            pass  # a row the registry no longer accepts (the limits were tightened): the default is the safer value
    return spec.default(env)


async def get(db: AsyncSession, key: str, settings: Settings | None = None) -> Any:
    """The stored value of the setting, or the env default. `settings` defaults to the process's own (`get_settings`)."""
    spec = spec_of(key)
    return _stored(spec, await db.get(SystemSetting, key), _env(settings))


async def get_all(db: AsyncSession, settings: Settings | None = None) -> dict[str, Any]:
    """The effective value of every setting, one query."""
    env = _env(settings)
    rows = {r.key: r for r in (await db.scalars(select(SystemSetting))).all()}
    return {key: _stored(spec, rows.get(key), env) for key, spec in SETTINGS.items()}


async def set(  # noqa: A001 - the name of the contract (settings_store.set)
    db: AsyncSession, key: str, value: Any, actor: User | None, ip: str | None = None, settings: Settings | None = None
) -> Any:
    """Validates and stores the value (UnknownSetting for a foreign key, InvalidSetting for a wrong value), writes
    `setting.update` to the audit log, returns the stored value. Does not commit."""
    spec = spec_of(key)
    value = spec.coerce(value)
    row = await db.get(SystemSetting, key)
    old = _stored(spec, row, _env(settings))
    by = actor.email if actor is not None else None
    if row is None:
        db.add(SystemSetting(key=key, value=value, updated_at=datetime.now(UTC), updated_by=by))
    else:
        row.value, row.updated_at, row.updated_by = value, datetime.now(UTC), by
    await audit.record(db, actor, "setting.update", "setting", key, {"old": old, "new": value}, ip)
    return value


async def reset(
    db: AsyncSession, key: str, actor: User | None, ip: str | None = None, settings: Settings | None = None
) -> Any:
    """Drops the override: the setting goes back to the env default (returned). Audited as `setting.update`
    (details: old, new, reset). Does not commit."""
    spec = spec_of(key)
    row = await db.get(SystemSetting, key)
    old = _stored(spec, row, _env(settings))
    if row is not None:
        await db.delete(row)
    new = spec.default(_env(settings))
    await audit.record(db, actor, "setting.update", "setting", key, {"old": old, "new": new, "reset": True}, ip)
    return new
