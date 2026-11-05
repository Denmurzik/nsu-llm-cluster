"""Admin «Резервные копии»: the Postgres dumps of the main server (app.backups), run now, download, delete.

  GET    /api/admin/backups                  the sets and safety copies (time, size, checksum if any), free space,
                                             schedule, retention, the state of the backup Job
  POST   /api/admin/backups                  make a backup now (a Job from the CronJob, app.backup_jobs); 409 while one runs
  GET    /api/admin/backups/{file}/download  the dump as a file (audit `backup.download`)
  DELETE /api/admin/backups/{name}           delete a set or a safety copy (audit `backup.delete`)

Restore stays on the command line (main/backup/pg-restore.sh): each set carries the exact command.
"""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, backups
from app.auth.sessions import require_admin, require_csrf
from app.backup_jobs import AlreadyRunning, BackupJobError, BackupJobs, JobInfo
from app.config import Settings
from app.db import get_session
from app.deps import get_k8s, get_settings_dep
from app.k8s import K8sUnavailable
from app.models import User

router = APIRouter(prefix="/api/admin/backups", dependencies=[Depends(require_admin)])
csrf = [Depends(require_csrf)]

NOT_FOUND = "Такой копии нет"
ALREADY_RUNNING = "Копия уже делается: дождитесь её окончания"
K8S_DOWN = "Kubernetes недоступен, копию сейчас сделать нельзя"


def get_backup_jobs(request: Request) -> BackupJobs:
    state = request.app.state
    jobs = getattr(state, "backup_jobs", None)
    if jobs is None:
        jobs = state.backup_jobs = BackupJobs(get_k8s(request))
    return jobs


def _job(job: JobInfo | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "name": job.name,
        "state": job.state,
        "manual": job.manual,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _entry(e: backups.BackupEntry) -> dict[str, Any]:
    return {
        "name": e.name,
        "kind": e.kind,
        "at": e.at.isoformat(),
        "size_bytes": e.size_bytes,
        "complete": e.complete,
        "restore_command": e.restore_command,
        "files": [{"name": f.name, "db": f.db, "size_bytes": f.size_bytes, "sha256": f.sha256} for f in e.files],
    }


@router.get("")
async def list_backups(
    settings: Settings = Depends(get_settings_dep), jobs: BackupJobs = Depends(get_backup_jobs)
) -> dict[str, Any]:
    entries = await asyncio.to_thread(backups.list_entries, settings.backup_dir)
    disk = await asyncio.to_thread(backups.disk_usage, settings.backup_dir)
    state = await jobs.state()
    schedule = running = last_run = None
    if state is not None:
        sched, all_jobs = state
        schedule = {
            "cron": sched.cron,
            "timezone": "UTC",
            "suspended": sched.suspended,
            "last_schedule_at": sched.last_schedule_at.isoformat() if sched.last_schedule_at else None,
        }
        running = any(j.state == "running" for j in all_jobs)
        last_run = _job(next((j for j in all_jobs if j.state != "running"), None))
    return {
        "available": entries is not None,
        "items": [_entry(e) for e in entries or []],
        "disk": disk,
        "schedule": schedule,
        "retention_keep": state[0].keep if state is not None else None,
        "running": running,  # None: Kubernetes could not be asked
        "last_run": last_run,
    }


@router.post("", status_code=202, dependencies=csrf)
async def run_now(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    jobs: BackupJobs = Depends(get_backup_jobs),
) -> dict[str, str]:
    try:
        name = await jobs.run_now()
    except AlreadyRunning:
        raise HTTPException(status_code=409, detail=ALREADY_RUNNING) from None
    except K8sUnavailable:
        raise HTTPException(status_code=503, detail=K8S_DOWN) from None
    except BackupJobError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    await audit.record(db, user, "backup.run", "backup", name, None, audit.client_ip(request))
    await db.commit()
    return {"job": name}


@router.get("/{file}/download")
async def download(
    file: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> FileResponse:
    path = await asyncio.to_thread(backups.resolve_file, settings.backup_dir, file)
    if path is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    size = path.stat().st_size
    await audit.record(db, user, "backup.download", "backup", file, {"size_bytes": size}, audit.client_ip(request))
    await db.commit()
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=file,
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/{name}", status_code=204, dependencies=csrf)
async def delete_backup(
    name: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    try:
        removed = await asyncio.to_thread(backups.delete_entry, settings.backup_dir, name)
    except OSError:
        raise HTTPException(status_code=500, detail="Не удалось удалить файлы копии") from None
    if removed is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    await audit.record(db, user, "backup.delete", "backup", name, {"files": removed}, audit.client_ip(request))
    await db.commit()
    return Response(status_code=204)
