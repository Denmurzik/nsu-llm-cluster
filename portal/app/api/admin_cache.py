"""Admin API «Кэш моделей»: the cache of the Hugging Face mirror on main and background downloads into it (task 2-29).

  GET    /api/admin/cache                        the files in the cache, who uses each, the volume totals
  DELETE /api/admin/cache/{owner}/{repo}[/{path}] one file or a whole repository, unless a deployment uses it
  GET    /api/admin/prefetch                     the background downloads: state, bytes, speed, error
  POST   /api/admin/prefetch                     {repo, revision?, files[]}: download files ahead of the nodes
  DELETE /api/admin/prefetch/{id}                cancel a download (a finished one leaves the list)
  DELETE /api/admin/prefetch                     clear the list of finished downloads
  POST   /api/admin/deployments/{id}/prefetch    queue the weights of a deployment: the model (every shard), the image
                                                 projector and the draft model, at the pinned commit

The mirror does the work (main/hf_mirror.py `/admin/*`, reachable from this process only); the portal adds what the mirror
cannot know: which deployments use a file, so that one in use is not deleted.
"""

import posixpath
import re
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.sessions import require_admin, require_csrf
from app.catalog.hf import HfNotFound, HfUnavailable
from app.catalog.mirror_admin import MirrorAdmin, MirrorRefused, MirrorUnavailable
from app.db import get_session
from app.deployments import service
from app.deps import get_hf
from app.localmodels.store import is_local
from app.models import ModelDeployment, User

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])
csrf = [Depends(require_csrf)]

_SHARD = re.compile(r"^(?P<base>.+)-\d{5}-of-(?P<total>\d{5})\.gguf$")
MAX_FILES = 200  # in one request, like the mirror


async def _log(db: AsyncSession, user: User, request: Request, action: str, target: str, details: dict | None) -> None:
    """Audit row of a cache change, written after the mirror did it (the change lives in the mirror, not in the database:
    the row is added only when the mirror succeeded, so the log never shows an action that failed)."""
    await audit.record(db, user, action, "cache", target, details, audit.client_ip(request))
    await db.commit()


def get_mirror(request: Request) -> MirrorAdmin:
    """The admin client of the mirror; tests put their own on `app.state.mirror_admin`."""
    return getattr(request.app.state, "mirror_admin", None) or MirrorAdmin.from_settings(request.app.state.settings)


@contextmanager
def _errors():
    try:
        yield
    except MirrorUnavailable as e:
        raise HTTPException(
            503, "Зеркало Hugging Face на главном недоступно или выключено (HF_MIRROR_ENABLED): кэша моделей нет"
        ) from e
    except MirrorRefused as e:
        if e.status in (404, 409):
            raise HTTPException(e.status, e.detail) from e
        if e.status in (400, 411, 413):
            raise HTTPException(422, e.detail) from e
        raise HTTPException(502, f"Зеркало ответило ошибкой: {e.detail}") from e
    except HfNotFound as e:
        raise HTTPException(404, "Репозиторий или файл не найден") from e
    except HfUnavailable as e:
        raise HTTPException(502, "Hugging Face недоступен") from e


# ---------- which deployment uses which file ----------


def shard_set(file: str) -> list[str]:
    """`file` and, for a part of a multi-file GGUF, all its parts (the pods download every part)."""
    m = _SHARD.match(file)
    if not m:
        return [file]
    total = int(m["total"])
    return [f"{m['base']}-{i:05d}-of-{total:05d}.gguf" for i in range(1, total + 1)]


def deployment_downloads(dep: ModelDeployment) -> list[tuple[str, str, list[str]]]:
    """(repository, revision, files) the pods of a llama.cpp deployment download from Hugging Face: the model with all its
    parts and the image projector (both of `dep.repo`, at its pinned commit), and the draft model (its own repository and
    commit). Own files (`local/...`) are not downloaded, they lie on main. A vLLM deployment has no file: `files` is empty
    and the caller lists the repository."""
    out: list[tuple[str, str, list[str]]] = []
    if dep.engine == "vllm":
        return [] if is_local(dep.repo) else [(dep.repo, dep.revision or "main", [])]
    if not is_local(dep.repo):
        files = shard_set(dep.file) if dep.file else []
        if dep.mmproj_file and not is_local(dep.mmproj_file):
            files.append(dep.mmproj_file)
        if files:
            out.append((dep.repo, dep.revision or "main", files))
    if dep.draft_repo and dep.draft_file and not is_local(dep.draft_repo):
        out.append((dep.draft_repo, dep.draft_revision or "main", shard_set(dep.draft_file)))
    return out


class Usage:
    """Which deployments use which cached file (any deployment, enabled or not: its weights are what the nodes fetch)."""

    def __init__(self, deps: list[ModelDeployment]):
        self.files: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.repos: dict[str, list[dict[str, Any]]] = {}  # vLLM: the whole repository
        for dep in deps:
            who = {"id": dep.id, "name": dep.name, "enabled": dep.enabled}
            for repo, _, files in deployment_downloads(dep):
                if not files:
                    self.repos.setdefault(repo, []).append(who)
                for path in files:
                    self.files.setdefault((repo, path), []).append(who)

    def of_file(self, repo: str, path: str) -> list[dict[str, Any]]:
        return [*self.files.get((repo, path), []), *self.repos.get(repo, [])]

    def of_repo(self, repo: str) -> list[dict[str, Any]]:
        found = {w["id"]: w for (r, _), ws in self.files.items() if r == repo for w in ws}
        found.update({w["id"]: w for w in self.repos.get(repo, [])})
        return list(found.values())


# ---------- the cache ----------


@router.get("/cache")
async def cache(db: AsyncSession = Depends(get_session), mirror: MirrorAdmin = Depends(get_mirror)) -> dict:
    with _errors():
        data = await mirror.cache()
    usage = Usage(await service.list_all(db))
    files = [{**f, "used_by": usage.of_file(f["repo"], f["path"])} for f in data["files"]]
    return {**data, "files": files}


@router.delete("/cache/{target:path}", dependencies=csrf)
async def delete_cache(
    target: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    mirror: MirrorAdmin = Depends(get_mirror),
) -> dict:
    owner, _, rest = target.partition("/")
    repo_name, _, path = rest.partition("/")
    repo = f"{owner}/{repo_name}"
    if not re.fullmatch(service.REPO_RE, repo):
        raise HTTPException(422, "Репозиторий указывается как владелец/название")
    if path and any(part in ("", ".", "..") for part in path.split("/")):
        # `org/m/../../other/repo` would pass the check of «who uses it» for org/m and be normalized to another repo by the client
        raise HTTPException(422, "Путь к файлу не может содержать пустых частей, «.» и «..»")
    usage = Usage(await service.list_all(db))
    users = usage.of_file(repo, path) if path else usage.of_repo(repo)
    if users:
        names = ", ".join(w["name"] for w in users)
        raise HTTPException(
            409,
            f"Файл используется развёртыванием {names}: удалите развёртывание или выберите другую модель"
            if path
            else f"В репозитории есть файлы развёртывания {names}: удалите файлы по одному, кроме используемых",
        )
    with _errors():
        result = await mirror.delete(repo, path or None)
    await _log(db, user, request, "cache.delete", f"{repo}/{path}" if path else repo,
               {"freed_bytes": result.get("freed_bytes"), "files": len(result.get("deleted") or [])})
    return result


# ---------- background downloads ----------


class PrefetchIn(BaseModel):
    repo: str
    revision: str | None = Field(default=None, max_length=128)
    files: list[str] = Field(min_length=1, max_length=MAX_FILES)


def _check_prefetch_repo(repo: str) -> None:
    if not re.fullmatch(service.REPO_RE, repo):
        raise HTTPException(422, "Репозиторий указывается как владелец/название")
    if is_local(repo):
        raise HTTPException(422, "Свои модели лежат на главном, скачивать их заранее не нужно")


@router.get("/prefetch")
async def prefetch_jobs(mirror: MirrorAdmin = Depends(get_mirror)) -> dict:
    with _errors():
        return await mirror.prefetch_jobs()


@router.post("/prefetch", status_code=202, dependencies=csrf)
async def prefetch(
    body: PrefetchIn,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    mirror: MirrorAdmin = Depends(get_mirror),
) -> dict:
    _check_prefetch_repo(body.repo)
    with _errors():
        jobs = await mirror.prefetch_add(body.repo, body.revision, body.files)
    await _log(db, user, request, "cache.prefetch", body.repo,
               {"revision": body.revision or "main", "files": list(dict.fromkeys(body.files))})
    return {"jobs": jobs}


@router.delete("/prefetch/{job_id}", dependencies=csrf)
async def cancel_prefetch(
    job_id: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    mirror: MirrorAdmin = Depends(get_mirror),
) -> dict:
    with _errors():
        job = await mirror.prefetch_cancel(job_id)
    await _log(db, user, request, "cache.prefetch", f"{job.get('repo')}/{job.get('path')}",
               {"action": "cancel", "job": job_id, "revision": job.get("revision"), "state": job.get("state")})
    return job


@router.delete("/prefetch", dependencies=csrf)
async def clear_prefetch(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    mirror: MirrorAdmin = Depends(get_mirror),
) -> dict:
    with _errors():
        result = await mirror.prefetch_cancel(None)
    await _log(db, user, request, "cache.prefetch", "finished", {"action": "clear", "removed": result.get("removed")})
    return result


@router.post("/deployments/{dep_id}/prefetch", status_code=202, dependencies=csrf)
async def prefetch_deployment(
    dep_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    mirror: MirrorAdmin = Depends(get_mirror),
    hf=Depends(get_hf),
) -> dict:
    """Queues the weights of the deployment at its pinned commit; what is cached already finishes at once."""
    dep = await service.get(db, dep_id)
    if dep is None:
        raise HTTPException(404, "Развёртывание не найдено")
    plan = deployment_downloads(dep)
    if not plan:
        raise HTTPException(422, "У развёртывания только свои файлы (local/…): они лежат на главном, скачивать нечего")
    jobs: list[dict] = []
    with _errors():
        for repo, revision, files in plan:
            if not files:  # vLLM: a snapshot of the repository, every visible file
                files = [f.path for f in await hf.list_files(repo) if not _hidden(f.path)]
            for chunk in (files[i : i + MAX_FILES] for i in range(0, len(files), MAX_FILES)):
                jobs += await mirror.prefetch_add(repo, revision, chunk)
    await _log(db, user, request, "cache.prefetch", dep.name,
               {"deployment_id": dep.id, "files": [f"{j['repo']}@{j['revision'][:12]}/{j['path']}" for j in jobs]})
    return {"jobs": jobs}


def _hidden(path: str) -> bool:
    return any(part.startswith(".") for part in posixpath.normpath(path).split("/"))
