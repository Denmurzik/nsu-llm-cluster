"""Client of the admin API of the Hugging Face mirror on main (main/hf_mirror.py, `/admin/cache`, `/admin/prefetch`).

The portal reaches the mirror at http://127.0.0.1:<hf_mirror_port> of main with the mirror key. The mirror refuses these
paths to anything that came through Caddy, so only the portal can use them. Requests go straight, no proxy from the
environment.
"""

import urllib.parse
from typing import Any

import httpx

from app.config import Settings

TIMEOUT_S = 15.0  # cancelling a running download waits for it to stop, up to 10 s on the mirror's side


class MirrorUnavailable(Exception):
    """The mirror does not answer (it is down, or HF_MIRROR_ENABLED=false)."""


class MirrorRefused(Exception):
    """The mirror answered with an error; `detail` is its message."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class MirrorAdmin:
    def __init__(self, url: str | None, token: str, timeout_s: float = TIMEOUT_S, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url.rstrip("/") if url else None
        self.token = token
        self.timeout_s = timeout_s
        self.transport = transport

    @classmethod
    def from_settings(cls, s: Settings) -> "MirrorAdmin":
        url = f"http://127.0.0.1:{s.hf_mirror_port}" if s.hf_mirror_enabled else None
        return cls(url, s.mirror_admin_key)

    async def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        if not self.url:
            raise MirrorUnavailable("the mirror is off")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport, trust_env=False) as http:
                resp = await http.request(
                    method, self.url + path, json=body, headers={"Authorization": f"Bearer {self.token}"}
                )
        except (httpx.HTTPError, OSError) as e:
            raise MirrorUnavailable(str(e)) from e
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            message = data.get("error") if isinstance(data, dict) else None
            raise MirrorRefused(resp.status_code, str(message or f"the mirror answered {resp.status_code}"))
        if not isinstance(data, dict):
            raise MirrorRefused(502, "the mirror sent something unreadable")
        return data

    @staticmethod
    def _quote(repo: str, path: str | None = None) -> str:
        return urllib.parse.quote(repo + (f"/{path}" if path else ""), safe="/")

    async def cache(self) -> dict:
        return await self._call("GET", "/admin/cache")

    async def delete(self, repo: str, path: str | None = None) -> dict:
        return await self._call("DELETE", f"/admin/cache/{self._quote(repo, path)}")

    async def prefetch_add(self, repo: str, revision: str | None, files: list[str]) -> list[dict]:
        body: dict[str, Any] = {"repo": repo, "files": files}
        if revision:
            body["revision"] = revision
        return (await self._call("POST", "/admin/prefetch", body))["jobs"]

    async def prefetch_jobs(self) -> dict:
        return await self._call("GET", "/admin/prefetch")

    async def prefetch_cancel(self, job_id: str | None = None) -> dict:
        """One job (cancelled, or removed when finished); without an id the finished jobs are removed."""
        return await self._call("DELETE", "/admin/prefetch" + (f"/{urllib.parse.quote(job_id, safe='')}" if job_id else ""))
