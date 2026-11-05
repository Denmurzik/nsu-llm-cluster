"""Runtime settings changed in the admin panel (table `system_settings`), on top of the env `Settings`.

`SETTINGS` is the registry: only a key listed here can be stored. `get` returns the value from the database, or the
default of the env (`Settings.<key>` when it exists, else the fixed `fallback` of the spec). `set` validates, stores and
writes the audit row; like `audit.record` it does not commit, the caller does. A setting that applies `live` is read
from the database where it is used; one that needs a restart is only read by its process at start, so the value
changed here takes effect after the restart of that component (the panel says so).
"""

import logging
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.config import Settings, get_settings
from app.models import SystemSetting, User

log = logging.getLogger(__name__)

LIVE = "live"
RESTART = "restart"

# the groups of the panel page, in the order it shows them
G_KEYS = "Ключи и лимиты"
G_NODES = "Ноды и Wake-on-LAN"
G_MODELS = "Модели и кэш"
G_MONITORING = "Мониторинг"
G_LOGIN = "Вход и сессии"
GROUPS = (G_KEYS, G_NODES, G_MODELS, G_MONITORING, G_LOGIN)


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
    group: str = ""  # one of GROUPS: the section of the panel page

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
            min=1, max=1_000_000, group=G_KEYS,
        ),
        SettingSpec(
            "key_tpm_limit", "int", 100_000,
            "Токенов в минуту на один ключ API (общий лимит, у пользователя может быть свой)",
            min=1, max=1_000_000_000, group=G_KEYS,
        ),
        SettingSpec(
            "max_keys_per_user", "int", 5,
            "Сколько ключей API может создать пользователь (у пользователя может быть своё число)",
            min=1, max=1000, group=G_KEYS,
        ),
        SettingSpec(
            "wol_enabled", "bool", True,
            "Будить выключенные ноды автоматически (Wake-on-LAN); кнопка «Разбудить» работает всегда",
            group=G_NODES,
        ),
        SettingSpec(
            "wol_delay_s", "float", 60.0,
            "Через сколько секунд после падения ноды отправить первый пакет пробуждения",
            min=0, max=86_400, group=G_NODES,
        ),
        SettingSpec(
            "wol_max_tries", "int", 6, "Сколько раз автоматически будить ноду, прежде чем сдаться",
            min=0, max=100, group=G_NODES,
        ),
        SettingSpec(
            "hf_mirror_max_gib", "int", 500,
            "Предел кэша зеркала Hugging Face на главном сервере, ГиБ; применяется после перезапуска зеркала",
            min=1, max=1_000_000, applies=RESTART, group=G_MODELS,
        ),
        SettingSpec(
            "model_cache_idle_s", "int", 600,
            "Через сколько секунд простоя ноды без развёртывания удалять с неё файлы моделей",
            min=60, max=2_592_000, group=G_MODELS,
        ),
        SettingSpec(
            "node_approval_required", "bool", True,
            "Новая нода ждёт подтверждения администратора на странице «Ноды», прежде чем получит модель и ключи",
            group=G_NODES,
        ),
        SettingSpec(
            "node_key_migration_days", "int", 7,
            "Сколько дней после обновления ноды без своего ключа (старый образ) работают со своего прежнего адреса; "
            "0 — сразу только через «Подтвердить» (надёжнее за NAT и с главным в Docker Desktop)",
            min=0, max=365, group=G_NODES,
        ),
        SettingSpec(
            "node_auto_update", "bool", False, "Обновлять ноды автоматически, не дожидаясь команды администратора",
            group=G_NODES,
        ),
        SettingSpec(
            "metrics_retention_days", "int", 7, "Сколько дней хранить замеры мониторинга",
            min=1, max=365, group=G_MONITORING,
        ),
        SettingSpec(
            "session_max_days", "int", 30,
            "Сколько дней живёт вход в портал, даже если им всё время пользуются (потом нужно войти снова)",
            min=1, max=365, group=G_LOGIN,
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


async def get_many(db: AsyncSession, keys: Iterable[str], settings: Settings | None = None) -> dict[str, Any]:
    """The effective values of the listed settings, one query."""
    keys = tuple(keys)
    for key in keys:
        spec_of(key)
    env = _env(settings)
    rows = {r.key: r for r in (await db.scalars(select(SystemSetting).where(SystemSetting.key.in_(keys)))).all()}
    return {key: _stored(SETTINGS[key], rows.get(key), env) for key in keys}


async def set(  # noqa: A001 - the name of the contract (settings_store.set)
    db: AsyncSession, key: str, value: Any, actor: User | None, ip: str | None = None, settings: Settings | None = None
) -> Any:
    """Validates and stores the value (UnknownSetting for a foreign key, InvalidSetting for a wrong value), writes
    `setting.update` to the audit log, returns the stored value. Does not commit."""
    spec = spec_of(key)
    value = spec.coerce(value)
    row = await db.get(SystemSetting, key)
    old = _stored(spec, row, _env(settings))
    if row is not None and old == value:
        return value  # the same value again: no write, no audit row
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
    new = spec.default(_env(settings))
    if row is None:
        return new  # never changed: nothing to reset, no audit row
    await db.delete(row)
    await audit.record(db, actor, "setting.update", "setting", key, {"old": old, "new": new, "reset": True}, ip)
    return new


# ---- the read side of the code: a short TTL cache over `get_all` ----------------------------------------------


class RuntimeSettings:
    """What the running code asks for a setting: the stored value, at most `ttl` seconds old.

    One per process (`state.runtime`, made in `open_state`). It reads the whole table in one query and keeps the
    result, so a hot path costs a dictionary lookup. The process that changes a setting calls `invalidate()` after its
    commit and sees the new value at once; another process (an API worker, the controllers) within `ttl` seconds.
    The env default comes from `state.settings` at the time of the call. When the database cannot be read the last
    known values stay in use (the env defaults before the first read): a settings outage must not stop a controller.
    """

    def __init__(self, state: Any, ttl: float = 5.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._state = state
        self.ttl = ttl
        self._clock = clock
        self._values: dict[str, Any] | None = None
        self._at = 0.0

    def invalidate(self) -> None:
        self._at = float("-inf")

    async def _load(self) -> dict[str, Any]:
        now = self._clock()
        if self._values is None or now - self._at >= self.ttl:
            try:
                async with self._state.sessionmaker() as db:
                    self._values = await get_all(db, self._state.settings)
                self._at = now
            except Exception:
                log.warning("runtime settings: the database is unavailable, using the last known values", exc_info=True)
                if self._values is None:
                    self._values = {k: s.default(self._state.settings) for k, s in SETTINGS.items()}
                self._at = now  # do not retry on every call during an outage
        return self._values

    async def get(self, key: str) -> Any:
        spec_of(key)
        return (await self._load())[key]

    async def get_many(self, *keys: str) -> dict[str, Any]:
        for key in keys:
            spec_of(key)
        values = await self._load()
        return {key: values[key] for key in keys}


async def live(state: Any, key: str) -> Any:
    """The current value of the setting for the code of a process: `state.runtime` when the process has one (every
    process started through `open_state`), else the env default."""
    runtime = getattr(state, "runtime", None)
    if runtime is not None:
        return await runtime.get(key)
    return spec_of(key).default(state.settings)


async def live_many(state: Any, keys: Iterable[str]) -> dict[str, Any]:
    keys = tuple(keys)
    runtime = getattr(state, "runtime", None)
    if runtime is not None:
        return await runtime.get_many(*keys)
    return {key: spec_of(key).default(state.settings) for key in keys}
