"""Out-models of the admin operations: audit log entries and runtime settings (tasks 2-32 and 2-34 add the endpoints)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models import AuditLog, SystemSetting
from app.settings_store import SettingSpec


class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    actor_id: UUID | None
    actor_email: str | None
    action: str
    target_type: str
    target: str
    details: dict[str, Any] | None
    ip: str | None

    @classmethod
    def of(cls, row: AuditLog) -> "AuditEntryOut":
        return cls.model_validate(row)


class AuditPageOut(BaseModel):
    """One page of the log, newest first."""

    items: list[AuditEntryOut]
    total: int


class SystemSettingOut(BaseModel):
    """A setting as the panel shows it: the effective value, the env default and the rules for a new value."""

    key: str
    type: Literal["int", "float", "bool"]
    value: Any  # effective: the stored override or the default
    default: Any  # what the env gives without an override
    overridden: bool
    min: float | None
    max: float | None
    description: str
    restart_required: bool
    updated_at: datetime | None
    updated_by: str | None

    @classmethod
    def of(cls, spec: SettingSpec, value: Any, default: Any, row: SystemSetting | None) -> "SystemSettingOut":
        return cls(
            key=spec.key,
            type=spec.type,
            value=value,
            default=default,
            overridden=row is not None,
            min=spec.min,
            max=spec.max,
            description=spec.description_ru,
            restart_required=spec.restart_required,
            updated_at=row.updated_at if row is not None else None,
            updated_by=row.updated_by if row is not None else None,
        )


class SettingIn(BaseModel):
    """Body of a change; the value is checked against the registry (`settings_store.set`), not here."""

    value: Any
