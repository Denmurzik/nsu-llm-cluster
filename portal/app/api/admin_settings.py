"""Admin «Настройки»: runtime settings of the panel (app.settings_store).

  GET    /api/admin/settings                 every setting: value, default, source (db|env), the rules, the section
  PUT    /api/admin/settings/{key}           {"value": ...}: validate, store, audit
  DELETE /api/admin/settings/{key}           back to the value of the .env
  POST   /api/admin/key-limits/apply         put the key limits onto the existing keys (a background job)
  GET    /api/admin/key-limits/job           its progress
  POST   /api/admin/hf-mirror/restart        restart the mirror so that it takes `hf_mirror_max_gib`
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from kubernetes_asyncio.client.exceptions import ApiException
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, key_limits, mirror_settings, settings_store
from app.api.schemas_admin_ops import SettingIn, SystemSettingOut
from app.auth.sessions import require_admin, require_csrf
from app.config import Settings
from app.db import get_session
from app.deps import get_k8s, get_settings_dep
from app.k8s import K8sUnavailable
from app.models import SystemSetting, User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])
csrf = [Depends(require_csrf)]

UNKNOWN = "Неизвестная настройка"
_K8S_ERRORS = (K8sUnavailable, ApiException)


async def _out(db: AsyncSession, key: str, settings: Settings) -> SystemSettingOut:
    spec = settings_store.spec_of(key)
    row = await db.get(SystemSetting, key)
    value = await settings_store.get(db, key, settings)
    default = spec.default(settings)
    item = SystemSettingOut.of(spec, value, default, row)
    if key == mirror_settings.KEY:
        running = await mirror_settings.applied(db, default)
        item = item.model_copy(update={"pending_restart": running != value})
    return item


async def _all(db: AsyncSession, settings: Settings) -> list[SystemSettingOut]:
    order = {group: i for i, group in enumerate(settings_store.GROUPS)}
    keys = sorted(settings_store.SETTINGS, key=lambda k: order.get(settings_store.SETTINGS[k].group, len(order)))
    return [await _out(db, key, settings) for key in keys]  # sorted() is stable: the registry order inside a group


async def _sync_mirror(request: Request, db: AsyncSession, settings: Settings) -> None:
    """The mirror reads its limit from a ConfigMap when it starts: keep it current. The saved value is already
    committed, so a cluster that cannot be reached now only delays this (the restart button writes it again)."""
    override = await mirror_settings.override_of(db)
    try:
        await mirror_settings.write_config(request.app.state.k8s, override)
    except _K8S_ERRORS as exc:
        log.warning("hf mirror limit: could not write the ConfigMap now: %s", exc)


@router.get("/settings")
async def list_settings(
    db: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings_dep)
) -> list[SystemSettingOut]:
    return await _all(db, settings)


async def _change(
    request: Request, db: AsyncSession, user: User, settings: Settings, key: str, value, reset: bool = False
) -> SystemSettingOut:
    try:
        if reset:
            await settings_store.reset(db, key, user, audit.client_ip(request), settings)
        else:
            await settings_store.set(db, key, value, user, audit.client_ip(request), settings)
    except settings_store.UnknownSetting:
        raise HTTPException(status_code=404, detail=UNKNOWN) from None
    except settings_store.InvalidSetting as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.commit()
    request.app.state.runtime.invalidate()
    if key == mirror_settings.KEY:
        await _sync_mirror(request, db, settings)
    return await _out(db, key, settings)


@router.put("/settings/{key}", dependencies=csrf)
async def put_setting(
    key: str,
    body: SettingIn,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> SystemSettingOut:
    return await _change(request, db, user, settings, key, body.value)


@router.delete("/settings/{key}", dependencies=csrf)
async def reset_setting(
    key: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> SystemSettingOut:
    return await _change(request, db, user, settings, key, None, reset=True)


@router.post("/key-limits/apply", status_code=202, dependencies=csrf)
async def apply_key_limits(
    request: Request, user: User = Depends(require_admin), db: AsyncSession = Depends(get_session)
) -> dict:
    state = request.app.state

    async def record(job: dict) -> None:  # the audit row is committed before the first key changes
        await audit.record(
            db, user, "setting.apply_key_limits", "setting", "key_limits",
            {"rpm_limit": job["rpm_limit"], "tpm_limit": job["tpm_limit"]}, audit.client_ip(request),
        )
        await db.commit()

    try:
        await key_limits.start(state, before_spawn=record)
    except key_limits.AlreadyRunning:
        raise HTTPException(status_code=409, detail="Обновление ключей уже идёт") from None
    return await key_limits.status(state.redis)


@router.get("/key-limits/job")
async def key_limits_job(request: Request) -> dict:
    return await key_limits.status(request.app.state.redis)


@router.post("/hf-mirror/restart", dependencies=csrf)
async def restart_mirror(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
) -> SystemSettingOut:
    override = await mirror_settings.override_of(db)
    try:
        await mirror_settings.write_config(k8s, override)
        found = await k8s.restart_deployment(mirror_settings.DEPLOYMENT)
    except K8sUnavailable:
        raise HTTPException(status_code=503, detail="Кластер недоступен") from None
    except ApiException as exc:
        log.warning("hf mirror restart refused by the cluster: %s", exc)
        raise HTTPException(status_code=502, detail="Кластер отклонил перезапуск зеркала") from None
    if not found:
        raise HTTPException(status_code=404, detail="Зеркало не найдено в кластере (оно выключено?)")
    await mirror_settings.remember_applied(db, override)
    value = await settings_store.get(db, mirror_settings.KEY, settings)
    await audit.record(
        db, user, "hf_mirror.restart", "setting", mirror_settings.KEY, {"max_gib": value}, audit.client_ip(request)
    )
    await db.commit()
    return await _out(db, mirror_settings.KEY, settings)
