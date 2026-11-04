"""Load progress of inference pods: «скачивание X %» -> «загрузка весов» -> «готово».

llama-server reports no load percentage over HTTP (docs/research/big-models.md, section 7), so the stage is pieced
together:
- the Hugging Face mirror on main reports the bytes it has sent to each node (the pods download from
  `<mirror>/c/<node name>`); against the model size (`meta.weights_bytes`) that is the download share;
- /health of the pod answers 503 while llama-server loads the weights (a split leader also sends them to its rpc
  servers then) and 200 once it is ready; during the download nothing answers, because llama-server starts its
  HTTP server only after it.
"""

import math
import posixpath
import re
from typing import Any

import httpx

TIMEOUT_S = 1.5
_SHARD = re.compile(r"^(?P<base>.+)-\d{5}-of-(?P<total>\d{5})\.gguf$")

STAGE_STARTING = "starting"
STAGE_DOWNLOADING = "downloading"
STAGE_LOADING = "loading"
STAGE_READY = "ready"


def _stage(stage: str, done: int | None = None, total: int | None = None) -> dict[str, Any]:
    percent = None
    if done is not None and total:
        percent = min(100.0, math.floor(done * 1000 / total) / 10)
    return {"stage": stage, "percent": percent, "done_bytes": done, "total_bytes": total}


def file_matches(dep, path: str) -> bool:
    """`path` is a file of the deployment's model: its GGUF file or one of its shards; for vLLM any file."""
    if not dep.file:
        return True
    if path == dep.file:
        return True
    want, got = _SHARD.match(dep.file), _SHARD.match(path)
    return bool(want and got and want["base"] == got["base"] and want["total"] == got["total"]
                and posixpath.dirname(path) == posixpath.dirname(dep.file))


def pod_progress(dep, *, ready: bool, phase: str | None, health: int | None, transfers: list[dict] | None) -> dict | None:
    """Stage of one inference pod (a replica or a split leader); None when it is not running."""
    if ready:
        return _stage(STAGE_READY)
    if phase != "Running":
        return None
    if health in (200, 503):
        return _stage(STAGE_LOADING)
    files: dict[str, dict] = {}
    for item in transfers or []:
        if item.get("repo") == dep.repo and file_matches(dep, item.get("path", "")):
            files[item["path"]] = item
    if not files:
        return _stage(STAGE_STARTING)
    done = sum(int(f.get("offset") or 0) for f in files.values())
    total = int((dep.meta or {}).get("weights_bytes") or 0) or sum(int(f.get("size") or 0) for f in files.values())
    if total and done >= total and not any(f.get("active") for f in files.values()):
        return _stage(STAGE_LOADING)  # downloaded: llama-server is starting on the weights
    return _stage(STAGE_DOWNLOADING, done, total or None)


class ProgressSource:
    """The mirror's transfer report and /health of the pods, both with a short timeout (None: unknown)."""

    def __init__(self, mirror_url: str | None, mirror_token: str, timeout_s: float = TIMEOUT_S,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.mirror_url = mirror_url.rstrip("/") if mirror_url else None
        self.mirror_token = mirror_token
        self.timeout_s = timeout_s
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport, trust_env=False)

    async def transfers(self) -> dict[str, list[dict]] | None:
        """node name -> files the mirror has sent to it (`repo`, `path`, `offset`, `size`, `active`, `updated`)."""
        if not self.mirror_url:
            return None
        try:
            async with self._client() as http:
                resp = await http.get(
                    f"{self.mirror_url}/_mirror/transfers", headers={"Authorization": f"Bearer {self.mirror_token}"}
                )
            if resp.status_code != 200:
                return None
            clients = resp.json().get("clients")
        except (httpx.HTTPError, OSError, ValueError, AttributeError):
            return None
        return clients if isinstance(clients, dict) else None

    async def health(self, url: str) -> int | None:
        try:
            async with self._client() as http:
                return (await http.get(url)).status_code
        except (httpx.HTTPError, OSError):
            return None
