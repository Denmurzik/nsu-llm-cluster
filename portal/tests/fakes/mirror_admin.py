"""In-memory stand-in for app.catalog.mirror_admin.MirrorAdmin: the cache and the prefetch jobs of the mirror on main."""

import time

from app.catalog.mirror_admin import MirrorRefused, MirrorUnavailable

GiB = 2**30


def cached_file(repo: str, path: str, size: int, **kw) -> dict:
    """An entry of GET /admin/cache of the mirror."""
    now = time.time()
    return {
        "repo": repo, "path": path, "revision": "c" * 40, "size": size, "total": size, "mtime": now - 3600,
        "last_served": now - 60, "downloading": False, "partial": False, "busy": False, **kw,
    }


class FakeMirrorAdmin:
    def __init__(self, files: list[dict] | None = None, limit_bytes: int = 150 * GiB):
        self.files = files if files is not None else []
        self.limit_bytes = limit_bytes
        self.jobs: list[dict] = []
        self.unavailable = False
        self.calls: list[tuple] = []
        self.next_error: MirrorRefused | None = None  # raised by the next call

    def _guard(self, *call) -> None:
        self.calls.append(call)
        if self.unavailable:
            raise MirrorUnavailable("fake mirror is down")
        if self.next_error is not None:
            error, self.next_error = self.next_error, None
            raise error

    async def cache(self) -> dict:
        self._guard("cache")
        return {
            "files": [dict(f) for f in self.files],
            "used_bytes": sum(f["size"] for f in self.files),
            "limit_bytes": self.limit_bytes,
            "disk_total_bytes": 1000 * GiB,
            "disk_free_bytes": 600 * GiB,
            "disk_reserve_bytes": 150 * GiB,
        }

    async def delete(self, repo: str, path: str | None = None) -> dict:
        self._guard("delete", repo, path)
        hit = [f for f in self.files if f["repo"] == repo and (path is None or f["path"] == path)]
        if not hit:
            raise MirrorRefused(404, "В кэше нет такого файла")
        if any(f["busy"] or f["downloading"] for f in hit):
            raise MirrorRefused(409, "Файл сейчас отдаётся нодам или скачивается: попробуйте позже")
        self.files = [f for f in self.files if f not in hit]
        return {"deleted": [{"repo": f["repo"], "path": f["path"], "size": f["size"]} for f in hit],
                "freed_bytes": sum(f["size"] for f in hit)}

    async def prefetch_add(self, repo: str, revision: str | None, files: list[str]) -> list[dict]:
        self._guard("prefetch_add", repo, revision, tuple(files))
        added = []
        for path in dict.fromkeys(files):
            job = {
                "id": f"{len(self.jobs):012x}", "repo": repo, "revision": revision or "main", "path": path,
                "state": "queued", "size": None, "written": 0, "speed_bps": 0.0, "error": None, "cached": False,
                "created": time.time(), "started": None, "finished": None,
            }
            self.jobs.append(job)
            added.append(dict(job))
        return added

    async def prefetch_jobs(self) -> dict:
        self._guard("prefetch_jobs")
        return {"jobs": [dict(j) for j in self.jobs], "concurrency": 2}

    async def prefetch_cancel(self, job_id: str | None = None) -> dict:
        self._guard("prefetch_cancel", job_id)
        if job_id is None:
            kept = [j for j in self.jobs if j["state"] in ("queued", "running")]
            removed, self.jobs = len(self.jobs) - len(kept), kept
            return {"removed": removed}
        job = next((j for j in self.jobs if j["id"] == job_id), None)
        if job is None:
            raise MirrorRefused(404, "Нет такого задания")
        if job["state"] in ("done", "failed", "cancelled"):
            self.jobs.remove(job)
        else:
            job.update(state="cancelled", error="Скачивание отменено", finished=time.time())
        return dict(job)
