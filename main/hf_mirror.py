"""Caching Hugging Face mirror for the nodes of NSU LLM Cluster.

Runs as the `hf-mirror` pod on llm-main (python3 of the LiteLLM image, standard library only) and listens on
127.0.0.1: Caddy serves it at https://<PUBLIC_HOSTNAME>/hf/. The nodes download a model from Hugging Face once
through it, then over the LAN.

Clients (llama.cpp `--hf-repo`, huggingface_hub, curl in serve-llama.sh) use:
  HEAD|GET /<owner>/<repo>/resolve/<revision>/<path>   the file, with Range; one upstream download per file
  GET      /api/models/<owner>/<repo>[/refs|/tree/<rev>[/<path>]|/revision/<rev>]   metadata, proxied
An optional `/c/<client>` prefix names the client (the node) for the progress report:
  GET      /_mirror/transfers   bytes sent to each client per file and the downloads in progress
  GET      /_mirror/health      liveness, without a token
Admin API of the portal (task 2-29; only with the admin key, never with a scoped token, and refused when the request came
through a proxy: Caddy sets X-Forwarded-*, the portal on main connects to 127.0.0.1 directly):
  GET    /admin/cache                  the files in cache/ (and the downloads in progress) with the volume totals
  DELETE /admin/cache/<owner>/<repo>[/<path>]   one file or a whole repository; 409 while it is served or downloaded
  POST   /admin/prefetch               {repo, revision?, files[]}: download files into cache/ in the background
  GET    /admin/prefetch               the prefetch jobs: state, bytes done, total, speed, error
  DELETE /admin/prefetch/<id>          cancel a job (a finished one is removed from the list); without id: clear the list
Every other request needs `Authorization: Bearer <token>`; the mirror itself talks to Hugging Face with HF_TOKEN, which
never leaves llm-main. Tokens (SEC-1 rule 4, SEC-2 L2):
  HF_MIRROR_ADMIN_TOKEN  the portal on main: every repository, /_mirror/transfers and the admin API. Never leaves main.
                         Without it (tests, a mirror started by hand) HF_MIRROR_TOKEN plays this role.
  HF_MIRROR_TOKEN        only signs the scoped tokens; not accepted as a Bearer token when an admin key is set. Never leaves
                         main either (not in llm-node-secrets, not in a node's desired state).
  nsm1.<payload>.<sig>   a node or the pods of a deployment (portal/app/mirror_tokens.py): payload {"c": client, "r": [repos]},
                         sig = HMAC-SHA256(HF_MIRROR_TOKEN, "nsm1." + payload), base64url without padding. Opens files and
                         metadata of the repositories in "r" only: the operator's HF_TOKEN fetches nothing else for a node
                         (gated or private repositories it sees) and a node cannot push other files into the cache.

Data directory (HF_MIRROR_DIR):
  cache/<owner>/<repo>/<path>     downloaded files, served for any revision; under HF_MIRROR_MAX_GIB, evicted least
                                  recently used first (never while being served or written)
  offline/<owner>/<repo>/<path>   files an admin put by hand (no network on main): served first, with metadata built
                                  from the directory; not under the limit, never evicted
  models/local/<name>/<path>      own model files of the portal («Мои модели», portal/app/localmodels): served as the
                                  repository local/<name>, only from there (Hugging Face is never asked about local/*),
                                  with metadata built from the directory; not under the limit, never evicted. Only the
                                  files the portal lists as ready in models/local/<name>/.manifest.json, unchanged
                                  since and inside the directory (no links out) are served; their ETag is the sha256
                                  (or size and mtime) and the commit follows the files, so a new version of a file is
                                  never taken for the old one by a node's cache. The rest of models/ belongs to the
                                  portal too (its drop folder): the mirror does not touch it
  .mirror/state/<owner>/<repo>/<path>.json   etag, commit, size and last access of a downloaded file
  .mirror/parts/<id>.part|.json              downloads, one pair per download; resumed after a restart, evictable
                                             once abandoned
  .mirror/api/<sha1>.json                    last good metadata answers (at most API_CACHE_MAX_FILES), used while
                                             Hugging Face is unreachable
  .mirror/prefetch.json                      the prefetch jobs (queued and running ones continue after a restart)
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

CHUNK = 1 << 20
DISK_RESERVE = 1 << 30  # bytes kept free on the volume besides the size limit, at least
# and at least this share of the volume: kubelet on llm-main evicts Postgres, LiteLLM and the mirror itself below 5 % free
DISK_RESERVE_PCT = 15
STALL_S = 300  # a reader gives up when the download it waits for makes no progress this long
TRANSFER_TTL_S = 6 * 3600  # finished transfers stay in the progress report this long
API_MAX_BYTES = 64 << 20
PREFETCH_CONCURRENCY = 2  # prefetch downloads running at once
PREFETCH_HISTORY = 100  # finished prefetch jobs kept in the list
PREFETCH_MAX_FILES = 200  # files in one prefetch request
ADMIN_BODY_MAX = 1 << 20
API_CACHE_MAX_FILES = 2000
# the metadata endpoints llama.cpp b12033 (common/hf-cache.cpp: refs, tree) and huggingface_hub (model_info, revision,
# list_repo_tree) read; everything else of the Hugging Face API is refused
_API_ALLOWED = re.compile(r"/api/models/[^/]+/[^/]+(/refs|/revision/[^/]+|/tree/[^/]+(/.*)?)?")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPO_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_CLIENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
_JOB_ID = re.compile(r"[0-9a-f]{12}")
_PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded")
_ADMIN_GET = ("/admin/cache", "/admin/prefetch")
LOCAL_OWNER = "local"  # repositories local/<name>: the portal's own model files in models/local/<name>/
_LOCAL_API = re.compile(r"/api/models/local/[^/]+(/.*)?")
MANIFEST = ".manifest.json"  # written by the portal (portal/app/localmodels/store.py)


def is_local(repo: str) -> bool:
    return repo.split("/", 1)[0] == LOCAL_OWNER
_HF_HEADERS = ("X-Repo-Commit", "X-Linked-Etag", "X-Linked-Size", "ETag", "Content-Length")


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} hf-mirror: {msg}", flush=True)


def disk_reserve(total: int) -> int:
    """Bytes the cache leaves free on a volume of `total` bytes."""
    return max(DISK_RESERVE, total * DISK_RESERVE_PCT // 100)


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class UpstreamUnavailable(Exception):
    pass


def _etag(value: str | None) -> str | None:
    """`W/"abc"` -> `abc`."""
    if not value:
        return None
    value = value.strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"') or None


TOKEN_PREFIX = "nsm1"
SCOPED_TOKEN_MAX = 16384  # a payload with a few repositories is far below; a longer header is refused before any work


def _b64url_decode(text: str) -> bytes:
    import base64

    if not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def scoped_token_repos(secret: str, token: str) -> frozenset[str] | None:
    """The repositories a scoped token `nsm1.<payload>.<sig>` opens, or None when it is not one signed with `secret`."""
    scoped = scoped_token(secret, token)
    return None if scoped is None else scoped[1]


def scoped_token(secret: str, token: str) -> tuple[str, frozenset[str]] | None:
    """(client, repositories) of a scoped token signed with `secret`, else None. The client is a node name or "*"."""
    if not secret or len(token) > SCOPED_TOKEN_MAX:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    want = hmac.new(secret.encode(), f"{TOKEN_PREFIX}.{parts[1]}".encode(), hashlib.sha256).digest()
    try:
        got = _b64url_decode(parts[2])
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(got, want):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("c"), str) or not isinstance(payload.get("r"), list):
        return None
    # optional expiry "e" (unix seconds): a token the portal minted with one stops working on its own
    exp = payload.get("e")
    if exp is not None and (isinstance(exp, bool) or not isinstance(exp, int) or time.time() >= exp):
        return None
    return payload["c"], frozenset(r for r in payload["r"] if isinstance(r, str))


def check_repo(repo: str) -> str:
    parts = repo.split("/")
    if len(parts) != 2 or not all(_REPO_PART.fullmatch(p) and ".." not in p for p in parts):
        raise HttpError(400, f"invalid repository: {repo}")
    return repo


def check_revision(revision: str) -> str:
    if not _REVISION.fullmatch(revision) or ".." in revision:
        raise HttpError(400, f"invalid revision: {revision}")
    return revision


def check_path(path: str) -> str:
    p = PurePosixPath(path)
    if not path or path.startswith("/") or path.endswith("/") or any(
        part in ("", ".", "..") or part.startswith(".") or any(ord(c) < 32 for c in part) for part in p.parts
    ):
        raise HttpError(400, f"invalid file path: {path}")
    return str(p)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None  # urllib raises HTTPError: the redirects are followed by Upstream.open


class Upstream:
    """Hugging Face over urllib (proxies from the environment). Redirects are followed here, so that the token is
    dropped when a redirect leaves the Hugging Face host (the LFS CDN takes a signed URL) and the headers of the
    first answer (X-Repo-Commit, X-Linked-*) are kept."""

    def __init__(self, base: str, token: str, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirect)

    def open(self, method: str, path: str, headers: dict[str, str] | None = None):
        """-> (response, status, hf headers). The response is file-like; the caller closes it."""
        url = self.base + path
        hdrs = {"User-Agent": "nsu-llm-hf-mirror/0.1", "Accept-Encoding": "identity", **(headers or {})}
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        meta: dict[str, str] = {}
        host = urllib.parse.urlsplit(url).netloc
        for _ in range(8):
            req = urllib.request.Request(url, method=method, headers=hdrs)
            try:
                resp = self.opener.open(req, timeout=self.timeout)
                status = resp.status
            except urllib.error.HTTPError as e:
                resp, status = e, e.code
            except (urllib.error.URLError, OSError) as e:
                raise UpstreamUnavailable(f"{method} {url}: {e}") from e
            for k in _HF_HEADERS:
                v = resp.headers.get(k)
                if v is not None and k != "Content-Length":
                    meta.setdefault(k, v)
            if status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                url = urllib.parse.urljoin(url, resp.headers["Location"])
                resp.close()
                if urllib.parse.urlsplit(url).netloc != host:
                    hdrs.pop("Authorization", None)
                continue
            if resp.headers.get("Content-Length") is not None:
                meta["Content-Length"] = resp.headers["Content-Length"]
            return resp, status, meta
        raise UpstreamUnavailable(f"{method} {path}: too many redirects")


def _file_meta(meta: dict[str, str], status: int) -> dict:
    """size / etag / commit of a file from the headers of an upstream answer."""
    size = meta.get("X-Linked-Size")
    if size is None and status == 200:
        size = meta.get("Content-Length")
    return {
        "size": int(size) if size and size.isdigit() else None,
        "etag": _etag(meta.get("X-Linked-Etag") or meta.get("ETag")),
        "commit": meta.get("X-Repo-Commit"),
    }


MSG_FULL = "Кэш зеркала заполнен: увеличьте HF_MIRROR_MAX_GIB или освободите том зеркала"
MSG_TOO_BIG = "Файл больше лимита кэша зеркала HF_MIRROR_MAX_GIB: увеличьте лимит"
MSG_NO_SPACE = "На томе зеркала кончилось место: освободите том или уменьшите HF_MIRROR_MAX_GIB"
MSG_CANCELLED = "Скачивание отменено"
MSG_BUSY = "Файл сейчас отдаётся нодам или скачивается: попробуйте позже"


class Download(threading.Thread):
    """One upstream download into its own .part file; readers follow `written` under `cond`.

    `part_id` names the .part / .json pair: a new download gets a fresh one, a resumed one (Mirror.download) the id of
    the part it continues, so two downloads of one file never share a .part.
    """

    def __init__(self, mirror: Mirror, repo: str, path: str, revision: str, part_id: str | None = None):
        super().__init__(daemon=True)
        self.mirror, self.repo, self.path, self.revision = mirror, repo, path, revision
        part_id = part_id or uuid.uuid4().hex
        self.part = mirror.parts_dir / f"{part_id}.part"
        self.part_meta = mirror.parts_dir / f"{part_id}.json"
        self.final = mirror.file_path(repo, path)
        self.cond = threading.Condition()
        self.size: int | None = None
        self.etag: str | None = None
        self.commit: str | None = None
        self.written = 0
        self.progress_at = time.monotonic()  # last change of `written` (readers give up after STALL_S without it)
        self.ready = False  # headers known and the first bytes on disk (or the file complete)
        self.done = False
        self.cancelled = False  # a prefetch cancelled while nobody else waits for this file (the part stays, resumable)
        self.nodes = 0  # node requests that wait for this download or follow it (counted under Mirror.lock)
        self.error: HttpError | None = None

    @property
    def active(self) -> bool:
        return not self.done and self.error is None

    def remaining(self) -> int:
        """Bytes this download will still write (its reservation in the size limit)."""
        return max(0, (self.size or 0) - self.written) if self.active else 0

    def run(self) -> None:
        try:
            self._download()
        except HttpError as e:
            self._fail(e)
        except UpstreamUnavailable as e:
            log(f"download of {self.repo}/{self.path} failed: {e}")
            self._fail(HttpError(502, "Hugging Face is unreachable"))
        except OSError as e:
            log(f"download of {self.repo}/{self.path} failed: {e!r}")
            if e.errno == errno.ENOSPC:
                self._fail(HttpError(507, MSG_NO_SPACE))
            else:
                self._fail(HttpError(502, f"download failed: {e}"))
        except Exception as e:  # noqa: BLE001 - a reader must never wait forever
            log(f"download of {self.repo}/{self.path} failed: {e!r}")
            self._fail(HttpError(502, f"download failed: {e}"))
        finally:
            self.mirror.download_finished(self)

    def _fail(self, error: HttpError) -> None:
        with self.cond:
            self.error = error
            self.cond.notify_all()

    def cancel(self) -> None:
        with self.cond:
            self.cancelled = True
            self.cond.notify_all()

    def _set_ready(self) -> None:
        with self.cond:
            self.ready = True
            self.progress_at = time.monotonic()
            self.cond.notify_all()

    def _write(self, out, chunk: bytes) -> None:
        out.write(chunk)
        out.flush()

    def _download(self) -> None:
        self.mirror.parts_dir.mkdir(parents=True, exist_ok=True)
        have = self.part.stat().st_size if self.part.exists() else 0
        saved = _read_json(self.part_meta) or {}
        headers = {"Range": f"bytes={have}-"} if have and saved.get("etag") else {}
        quoted = urllib.parse.quote(self.revision, safe="")
        path = f"/{self.repo}/resolve/{quoted}/{urllib.parse.quote(self.path)}"
        resp, status, meta = self.mirror.upstream.open("GET", path, headers)
        with resp:
            info = _file_meta(meta, 200)
            if status == 416 and headers and saved.get("size") == have:
                # the kept part is complete (an earlier run died before the rename): no new download
                self.size, self.etag, self.commit = have, saved.get("etag"), saved.get("commit")
                self.written = have
                return self._finish()
            if status == 206 and info["etag"] and info["etag"] == saved.get("etag"):
                offset = have
            elif status == 206 or (status == 416 and headers):
                resp.close()  # the kept part belongs to another version of the file: start over
                self.part.unlink(missing_ok=True)
                self.part_meta.unlink(missing_ok=True)
                return self._download()
            elif status == 200:
                offset = 0
            else:
                body = resp.read(65536)
                raise HttpError(status, _upstream_message(body, status))
            if info["size"] is None and status == 200 and meta.get("Content-Length", "").isdigit():
                info["size"] = int(meta["Content-Length"])
            self.etag, self.commit = info["etag"], info["commit"]
            with self.cond:
                self.size, self.written = info["size"], offset
            if self.cancelled:
                raise HttpError(499, MSG_CANCELLED)
            if self.size is not None:
                room = self.mirror.make_room(self.size - offset, self)
                if room != "ok":
                    raise HttpError(507, MSG_TOO_BIG if room == "too_big" else MSG_FULL)
            _write_json(self.part_meta, {"repo": self.repo, "path": self.path, **info})
            mode = "r+b" if offset else "wb"
            if not self.part.exists():
                self.part.touch()
            with open(self.part, mode) as out:
                out.seek(offset)
                out.truncate()
                log(f"downloading {self.repo}/{self.path} ({self.size} bytes, from {offset})")
                while True:
                    if self.cancelled:
                        raise HttpError(499, MSG_CANCELLED)
                    # read1: a connection that breaks early ends the loop with the bytes received so far kept
                    chunk = resp.read1(CHUNK)
                    if not chunk:
                        break
                    self._write(out, chunk)
                    with self.cond:
                        self.written += len(chunk)
                        self.progress_at = time.monotonic()
                        self.cond.notify_all()
                    if not self.ready:
                        self._set_ready()  # a full disk shows up as 507 before any reader sends headers
        if self.size is not None and self.written != self.size:
            raise HttpError(502, f"upstream closed the connection at {self.written} of {self.size} bytes")
        self._finish()

    def _finish(self) -> None:
        """The state first, then the rename: a crash in between leaves a part to resume, not a file without state."""
        self.final.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        self.mirror.write_state(self.repo, self.path, {
            "size": self.written, "etag": self.etag, "commit": self.commit, "checked": now, "accessed": now,
        })
        for attempt in range(100):
            try:
                os.replace(self.part, self.final)
                break
            except PermissionError:  # Windows (tests): a reader has the .part open for a moment
                if attempt == 99:
                    raise
                time.sleep(0.05)
        self.part_meta.unlink(missing_ok=True)
        log(f"cached {self.repo}/{self.path} ({self.written} bytes)")
        with self.cond:
            self.ready = True
            self.done = True
            self.cond.notify_all()

    def wait_ready(self) -> None:
        with self.cond:
            self.cond.wait_for(lambda: self.ready or self.error is not None, timeout=120)
            if self.error is not None:
                raise self.error
            if not self.ready:
                raise HttpError(504, "Hugging Face did not answer in time")

    def read_at(self, pos: int, n: int) -> bytes:
        """Bytes of the data from the .part, or from the final file once it was renamed. The file is opened for each
        read, so that no reader holds the .part open when the download renames it (Windows refuses that)."""
        for _ in range(100):
            for f in (self.part, self.final):
                try:
                    with open(f, "rb") as fh:
                        fh.seek(pos)
                        return fh.read(n)
                except FileNotFoundError:
                    continue
                except PermissionError:  # Windows (tests): the .part is being renamed right now
                    break
            time.sleep(0.05)
        return b""


class _JobReader:
    """File-like view of a download for Handler._copy."""

    def __init__(self, job: Download, pos: int):
        self.job, self.pos = job, pos

    def read(self, n: int) -> bytes:
        data = self.job.read_at(self.pos, n)
        self.pos += len(data)
        return data


def _upstream_message(body: bytes, status: int) -> str:
    try:
        return str(json.loads(body)["error"])
    except Exception:  # noqa: BLE001
        return f"upstream answered {status}"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


class Transfers:
    """Bytes sent to each client per file, for the progress of a deployment in the portal."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items: dict[tuple[str, str, str], dict] = {}

    def update(self, client: str, repo: str, path: str, offset: int, size: int | None, active: bool) -> None:
        with self.lock:
            self.items[(client, repo, path)] = {
                "repo": repo, "path": path, "offset": offset, "size": size, "active": active, "updated": time.time(),
            }

    def report(self) -> dict[str, list[dict]]:
        cutoff = time.time() - TRANSFER_TTL_S
        out: dict[str, list[dict]] = {}
        with self.lock:
            for key in [k for k, v in self.items.items() if not v["active"] and v["updated"] < cutoff]:
                del self.items[key]
            for (client, _, _), item in sorted(self.items.items()):
                out.setdefault(client, []).append(dict(item))
        return out


class Mirror:
    def __init__(self, root, upstream: str, upstream_token: str = "", mirror_token: str = "",
                 max_bytes: int = 500 << 30, revalidate_s: float = 3600, prefix: str = "/hf", admin_token: str = ""):
        self.root = Path(root)
        # models/ belongs to the portal (task 2-14): the mirror only serves models/local/ and never evicts there
        self.local_dir = self.root / "models" / LOCAL_OWNER
        self.cache_dir = self.root / "cache"
        self.offline_dir = self.root / "offline"
        self.state_dir = self.root / ".mirror" / "state"
        self.parts_dir = self.root / ".mirror" / "parts"
        self.api_dir = self.root / ".mirror" / "api"
        for d in (self.cache_dir, self.offline_dir, self.state_dir, self.parts_dir, self.api_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.upstream = Upstream(upstream, upstream_token)
        self.upstream_base = self.upstream.base
        self.mirror_token = mirror_token
        self.admin_token = admin_token or mirror_token
        self.max_bytes = max_bytes
        self.revalidate_s = revalidate_s
        self.prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""
        self.lock = threading.RLock()
        self.downloads: dict[tuple[str, str], Download] = {}
        self.readers: dict[Path, int] = {}
        self.transfers = Transfers()
        self.prefetch = Prefetcher(self)
        self.prefetch.start()  # the jobs a restart interrupted continue

    def stop(self) -> None:
        """Stops the background work (prefetch); the jobs stay in the list and continue at the next start."""
        self.prefetch.stop()

    # ---- paths and state ----------------------------------------------------------

    def file_path(self, repo: str, path: str) -> Path:
        """Where a downloaded file lives (cache/, under the size limit)."""
        return self.cache_dir / repo / path

    def offline_path(self, repo: str, path: str) -> Path:
        """Where an admin puts a file by hand (offline/, never evicted)."""
        return self.offline_dir / repo / path

    def own_path(self, repo: str, path: str) -> Path:
        """Where an own model file of the portal lives (models/local/<name>/, repository local/<name>)."""
        return self.local_dir / repo.split("/", 1)[1] / path

    def own_manifest(self, repo: str) -> dict[str, dict]:
        data = _read_json(self.local_dir / repo.split("/", 1)[1] / MANIFEST)
        files = data.get("files") if isinstance(data, dict) else None
        return files if isinstance(files, dict) else {}

    def own_file(self, repo: str, path: str, manifest: dict | None = None) -> tuple[Path, dict] | None:
        """(file, manifest entry) of a ready own file: listed by the portal, unchanged since, a regular file inside
        models/local/ (a link out of it is never followed)."""
        entry = (self.own_manifest(repo) if manifest is None else manifest).get(path)
        if not isinstance(entry, dict):
            return None
        f = self.own_path(repo, path)
        try:
            st = f.lstat()
            inside = f.resolve().is_relative_to(self.local_dir.resolve())
        except OSError:
            return None
        if f.is_symlink() or not f.is_file() or not inside:
            return None
        mtime = entry.get("mtime")
        if entry.get("size") != st.st_size or not isinstance(mtime, int | float) or abs(mtime - st.st_mtime) >= 1e-3:
            return None
        return f, entry

    @staticmethod
    def own_etag(repo: str, path: str, entry: dict) -> str:
        sha = entry.get("sha256")
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha):
            return sha
        return hashlib.sha1(f"{repo}/{path}:{entry.get('size')}:{entry.get('mtime')}".encode()).hexdigest()

    def own_commit(self, repo: str, manifest: dict | None = None) -> str:
        """Changes with any file of the model: a node that cached the model under the old commit sees a new one."""
        manifest = self.own_manifest(repo) if manifest is None else manifest
        listed = sorted((p, e.get("size"), e.get("mtime"), e.get("sha256")) for p, e in manifest.items() if isinstance(e, dict))
        return hashlib.sha1(json.dumps([repo, listed]).encode()).hexdigest()

    def find(self, repo: str, path: str) -> tuple[Path, bool] | None:
        """(file, put by hand) of a file the mirror can serve itself; a file put by hand comes first. A local/<name>
        repository is the portal's directory alone."""
        if is_local(repo):
            own = self.own_file(repo, path)
            return (own[0], True) if own else None
        for f, by_hand in ((self.offline_path(repo, path), True), (self.file_path(repo, path), False)):
            if f.is_file():
                return f, by_hand
        return None

    def _state_path(self, repo: str, path: str) -> Path:
        return self.state_dir / repo / (path + ".json")

    def read_state(self, repo: str, path: str) -> dict | None:
        return _read_json(self._state_path(repo, path))

    def write_state(self, repo: str, path: str, state: dict) -> None:
        with self.lock:
            _write_json(self._state_path(repo, path), state)

    def touch(self, repo: str, path: str, **changes) -> None:
        with self.lock:
            state = self.read_state(repo, path)
            if state is not None:
                state.update(changes)
                self.write_state(repo, path, state)

    def reading(self, f: Path, delta: int) -> None:
        """Counts the requests that send `f`: LRU never deletes a file being served."""
        with self.lock:
            n = self.readers.get(f, 0) + delta
            if n > 0:
                self.readers[f] = n
            else:
                self.readers.pop(f, None)

    # ---- size limit and LRU --------------------------------------------------------

    def _entries(self) -> tuple[int, list[tuple[float, int, list[Path], str]]]:
        """Bytes under the limit (downloaded files and parts) and the evictable entries, least recently used first:
        (last use, size, files to delete, label). Files put by hand (offline/) and local models (models/) are not
        under the limit and never evicted."""
        used = 0
        entries = []
        protected = set(self.readers)
        for job in self.downloads.values():
            if job.active:
                protected.update((job.part, job.final))
        for dirpath, _, names in os.walk(self.cache_dir):
            for name in names:
                f = Path(dirpath) / name
                try:
                    st = f.stat()
                except OSError:
                    continue
                used += st.st_size
                rel = f.relative_to(self.cache_dir).as_posix().split("/", 2)
                if f in protected or len(rel) < 3:
                    continue
                repo, path = "/".join(rel[:2]), rel[2]
                state = self.read_state(repo, path) or {}
                last = float(state.get("accessed") or st.st_mtime)
                entries.append((last, st.st_size, [f, self._state_path(repo, path)], f"{repo}/{path}"))
        for f in self.parts_dir.glob("*.part"):
            try:
                st = f.stat()
            except OSError:
                continue
            used += st.st_size
            if f not in protected:  # left by a download that died: resumable, but evictable like any file
                entries.append((st.st_mtime, st.st_size, [f, f.with_suffix(".json")], f"abandoned {f.name}"))
        return used, sorted(entries, key=lambda e: e[0])

    def usage_bytes(self) -> int:
        with self.lock:
            return self._entries()[0]

    def make_room(self, need: int, job: Download | None = None) -> str:
        """"ok" once `need` more bytes fit the limit and the disk next to what the other downloads still write,
        evicting least recently used entries; "too_big" (nothing evicted) or "full" otherwise."""
        with self.lock:
            if need > self.max_bytes:
                return "too_big"
            pending = sum(j.remaining() for j in self.downloads.values() if j is not job)
            used, entries = self._entries()
            disk = shutil.disk_usage(self.root)
            free = disk.free - disk_reserve(disk.total)

            def fits() -> bool:
                return used + pending + need <= self.max_bytes and pending + need <= free

            for last, size, files, label in entries:
                if fits():
                    break
                log(f"evicting {label} ({size} bytes, last used {time.ctime(last)})")
                for f in files:
                    f.unlink(missing_ok=True)
                used -= size
                free += size
            return "ok" if fits() else "full"

    # ---- downloads -----------------------------------------------------------------

    def _abandoned_part(self, repo: str, path: str) -> str | None:
        """Id of a part an earlier download of this file left behind (resumed by the next one)."""
        busy = {j.part.stem for j in self.downloads.values() if j.active}
        for meta in sorted(self.parts_dir.glob("*.json")):
            saved = _read_json(meta) or {}
            if saved.get("repo") == repo and saved.get("path") == path and meta.stem not in busy:
                if meta.with_suffix(".part").exists():
                    return meta.stem
        return None

    def download(self, repo: str, path: str, revision: str) -> Download:
        with self.lock:
            job = self.downloads.get((repo, path))
            if job is None or not job.active:  # a finished job lingers until its thread ends
                job = Download(self, repo, path, revision, self._abandoned_part(repo, path))
                self.downloads[(repo, path)] = job
                job.start()
            return job

    def join(self, job: Download, delta: int) -> None:
        """A node request starts (+1) or stops (-1) waiting for `job`: a prefetch does not cancel a download somebody awaits."""
        with self.lock:
            job.nodes += delta

    def download_finished(self, job: Download) -> None:
        with self.lock:
            if self.downloads.get((job.repo, job.path)) is job:
                del self.downloads[(job.repo, job.path)]

    def should_check(self, state: dict, revision: str) -> bool:
        """Time to compare with Hugging Face: once per revalidate_s, and at once for a request pinned to a commit
        other than the one the file came from, or from an unknown one (the cache is keyed by path, not by revision)."""
        if _COMMIT.fullmatch(revision) and state.get("commit") != revision:
            return True
        return time.time() - float(state.get("checked") or 0) >= self.revalidate_s

    def revalidate(self, repo: str, revision: str, path: str, state: dict) -> dict:
        """Compares the cached file with Hugging Face; marks it stale when its etag changed. Without an answer the file
        is kept and the next check waits revalidate_s again (no HEAD per request while Hugging Face is down)."""
        quoted = urllib.parse.quote(revision, safe="")
        try:
            resp, status, meta = self.upstream.open("HEAD", f"/{repo}/resolve/{quoted}/{urllib.parse.quote(path)}")
            resp.close()
        except UpstreamUnavailable:
            status, meta = 0, {}
        if status != 200:
            self.touch(repo, path, checked=time.time())
            return state
        info = _file_meta(meta, status)
        if info["etag"] and state.get("etag") and info["etag"] != state["etag"]:
            log(f"{repo}/{path} changed upstream ({state['etag']} -> {info['etag']}), downloading again")
            return {**state, "stale": True}
        changes = {"checked": time.time()}
        if _COMMIT.fullmatch(revision):
            changes["commit"] = info["commit"] or revision  # the same content at this commit: no check next time
        self.touch(repo, path, **changes)
        return {**state, **changes}

    def report(self) -> dict:
        with self.lock:
            downloads = [
                {"repo": j.repo, "path": j.path, "written": j.written, "size": j.size} for j in self.downloads.values()
            ]
        return {"clients": self.transfers.report(), "downloads": downloads, "limit_bytes": self.max_bytes}

    # ---- the admin view of cache/ (task 2-29) --------------------------------------------

    def _abandoned_parts(self) -> list[tuple[str, str, Path, dict]]:
        """(repo, path, .part, saved meta) of the parts no download works on right now."""
        busy = {j.part.stem for j in self.downloads.values() if j.active}
        out = []
        for meta in sorted(self.parts_dir.glob("*.json")):
            saved = _read_json(meta) or {}
            part = meta.with_suffix(".part")
            if meta.stem in busy or not part.exists() or not saved.get("repo") or not saved.get("path"):
                continue
            out.append((saved["repo"], saved["path"], part, saved))
        return out

    def cache_listing(self) -> dict:
        """Every file of cache/ with its state, the downloads in progress and the parts left by dead ones, and the
        totals of the size limit and of the volume."""
        with self.lock:
            used = self._entries()[0]
            active = {(j.repo, j.path): j for j in self.downloads.values() if j.active}
            files: dict[tuple[str, str], dict] = {}
            for dirpath, _, names in os.walk(self.cache_dir):
                for name in names:
                    f = Path(dirpath) / name
                    rel = f.relative_to(self.cache_dir).as_posix().split("/", 2)
                    if len(rel) < 3:
                        continue
                    repo, path = "/".join(rel[:2]), rel[2]
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    state = self.read_state(repo, path) or {}
                    files[(repo, path)] = {
                        "repo": repo, "path": path, "revision": state.get("commit"), "size": st.st_size,
                        "total": st.st_size, "mtime": st.st_mtime,
                        "last_served": float(state.get("accessed") or st.st_mtime),
                        "downloading": (repo, path) in active, "partial": False, "busy": f in self.readers,
                    }
            for (repo, path), job in active.items():
                entry = files.get((repo, path))
                if entry is not None:  # a changed file downloaded again: the old copy is still served
                    entry["written"] = job.written
                    continue
                files[(repo, path)] = {
                    "repo": repo, "path": path, "revision": job.commit, "size": job.written, "total": job.size,
                    "mtime": None, "last_served": None, "downloading": True, "partial": True,
                    "busy": job.final in self.readers, "written": job.written,
                }
            for repo, path, part, saved in self._abandoned_parts():
                if (repo, path) in files:
                    continue
                try:
                    st = part.stat()
                except OSError:
                    continue
                files[(repo, path)] = {
                    "repo": repo, "path": path, "revision": saved.get("commit"), "size": st.st_size,
                    "total": saved.get("size"), "mtime": st.st_mtime, "last_served": None, "downloading": False,
                    "partial": True, "busy": False,
                }
        disk = shutil.disk_usage(self.root)
        return {
            "files": [files[k] for k in sorted(files)],
            "used_bytes": used,
            "limit_bytes": self.max_bytes,
            "disk_total_bytes": disk.total,
            "disk_free_bytes": disk.free,
            "disk_reserve_bytes": disk_reserve(disk.total),
        }

    def delete_cache(self, repo: str, path: str | None = None) -> dict:
        """Deletes one file of cache/ (with its state and a part left for it) or the whole repository. Nothing is deleted
        while one of the files is served or downloaded (409); a missing one is 404."""
        check_repo(repo)
        if path is not None:
            check_path(path)
        with self.lock:
            targets: dict[str, int] = {}  # path -> bytes freed
            base = self.cache_dir / repo
            if path is None:
                for dirpath, _, names in os.walk(base):
                    for name in names:
                        f = Path(dirpath) / name
                        targets[f.relative_to(base).as_posix()] = f.stat().st_size
            elif (base / path).is_file():
                targets[path] = (base / path).stat().st_size
            parts = [(p, part) for r, p, part, _ in self._abandoned_parts() if r == repo and (path is None or p == path)]
            for p, part in parts:
                targets[p] = targets.get(p, 0) + part.stat().st_size
            downloading = {p for (r, p), j in self.downloads.items() if r == repo and j.active and (path is None or p == path)}
            for p in sorted(downloading | {p for p in targets if base / p in self.readers}):
                raise HttpError(409, f"{MSG_BUSY}: {repo}/{p}")
            if not targets:
                raise HttpError(404, "В кэше нет такого файла" if path else "В кэше нет такого репозитория")
            for p in targets:
                for f in (base / p, self._state_path(repo, p)):
                    f.unlink(missing_ok=True)
            for _, part in parts:
                part.unlink(missing_ok=True)
                part.with_suffix(".json").unlink(missing_ok=True)
            for root in (self.cache_dir, self.state_dir):  # directories nothing is left in
                for dirpath, _, _ in os.walk(root / repo, topdown=False):
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                owner = (root / repo).parent
                if owner.is_dir() and not os.listdir(owner):
                    owner.rmdir()
        log(f"deleted {repo}{'/' + path if path else ''} from the cache ({len(targets)} files, {sum(targets.values())} bytes)")
        return {"deleted": [{"repo": repo, "path": p, "size": n} for p, n in sorted(targets.items())],
                "freed_bytes": sum(targets.values())}

    # ---- the metadata API cache ----------------------------------------------------

    def save_api(self, cache: Path, answer: dict) -> None:
        _write_json(cache, answer)
        files = sorted(self.api_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
        for f in files[: max(0, len(files) - API_CACHE_MAX_FILES)]:
            f.unlink(missing_ok=True)

    # ---- local metadata (airgap, or Hugging Face down) -----------------------------

    def local_files(self, repo: str) -> list[tuple[str, int, dict | None, Path]]:
        """(path, size, state, file) of every file the mirror has of `repo`: put by hand first, then downloaded."""
        out: dict[str, tuple[str, int, dict | None, Path]] = {}
        if is_local(repo):  # the ready files of the portal's manifest; the state carries their ETag
            manifest = self.own_manifest(repo)
            for p in sorted(manifest):
                own = self.own_file(repo, p, manifest)
                if own:
                    out[p] = (p, own[1]["size"], {"etag": self.own_etag(repo, p, own[1])}, own[0])
            return [out[k] for k in sorted(out)]
        for base, by_hand in ((self.cache_dir / repo, False), (self.offline_dir / repo, True)):
            if not base.is_dir():
                continue
            for dirpath, dirnames, names in os.walk(base):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in sorted(names):
                    if name.startswith("."):
                        continue
                    f = Path(dirpath) / name
                    rel = f.relative_to(base).as_posix()
                    out[rel] = (rel, f.stat().st_size, None if by_hand else self.read_state(repo, rel), f)
        return [out[k] for k in sorted(out)]

    def local_commit(self, repo: str, files=None) -> str:
        if is_local(repo):
            return self.own_commit(repo)
        commits = [s["commit"] for _, _, s, _ in (files if files is not None else self.local_files(repo))
                   if s and s.get("commit") and re.fullmatch(r"[0-9a-f]{40}", s["commit"])]
        return commits[0] if commits else hashlib.sha1(f"nsu-llm-mirror:{repo}".encode()).hexdigest()

    @staticmethod
    def local_oid(repo: str, path: str, size: int, state: dict | None, f: Path) -> str:
        if state and state.get("etag") and _HEX.fullmatch(state["etag"]):
            return state["etag"]
        return hashlib.sha1(f"{repo}/{path}:{size}:{int(f.stat().st_mtime)}".encode()).hexdigest()

    def synthesize(self, path: str, query: dict[str, list[str]]):
        """Answer of a metadata endpoint built from the local files, or None."""
        m = re.fullmatch(r"/api/models/([^/]+/[^/]+)(/refs|/revision/[^/]+|/tree/[^/]+(/.*)?)?", path)
        if not m:
            return None
        repo = urllib.parse.unquote(m.group(1))
        try:
            check_repo(repo)
        except HttpError:
            return None
        files = self.local_files(repo)
        if not files:
            return None
        commit = self.local_commit(repo, files)
        tail = m.group(2) or ""
        if tail == "/refs":
            return {"branches": [{"name": "main", "ref": "refs/heads/main", "targetCommit": commit}], "tags": [], "converts": []}
        if tail.startswith("/tree/"):
            sub = urllib.parse.unquote((m.group(3) or "").strip("/"))
            recursive = (query.get("recursive") or ["false"])[0].lower() in ("1", "true")
            items, dirs = [], set()
            for p, size, state, f in files:
                if sub and not p.startswith(sub + "/"):
                    continue
                rest = p[len(sub) + 1:] if sub else p
                if not recursive and "/" in rest:
                    dirs.add((sub + "/" if sub else "") + rest.split("/", 1)[0])
                    continue
                if recursive:
                    parts = p.split("/")[:-1]
                    for i in range(len(sub.split("/")) if sub else 0, len(parts)):
                        dirs.add("/".join(parts[: i + 1]))
                item = {"type": "file", "oid": self.local_oid(repo, p, size, state, f), "size": size, "path": p}
                etag = (state or {}).get("etag")
                if etag and re.fullmatch(r"[0-9a-f]{64}", etag):
                    item["lfs"] = {"oid": etag, "size": size, "pointerSize": 134}
                items.append(item)
            dir_items = [{"type": "directory", "oid": hashlib.sha1(d.encode()).hexdigest(), "size": 0, "path": d}
                         for d in sorted(dirs)]
            return dir_items + items
        return {
            "_id": commit[:24], "id": repo, "modelId": repo, "sha": commit, "private": False, "gated": False,
            "disabled": False, "downloads": 0, "likes": 0, "tags": [], "library_name": "gguf",
            "lastModified": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "siblings": [{"rfilename": p, "size": size} for p, size, _, _ in files],
        }


# ---------------------------------------------------------------- prefetch (task 2-29)

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
_FINISHED = (DONE, FAILED, CANCELLED)


class PrefetchJob:
    """One file to download into cache/ ahead of any node."""

    PERSISTED = ("id", "repo", "revision", "path", "state", "size", "written", "speed_bps", "error", "cached", "created",
                 "started", "finished")

    def __init__(self, repo: str, revision: str, path: str, **kw):
        self.id = kw.get("id") or uuid.uuid4().hex[:12]
        self.repo, self.revision, self.path = repo, revision, path
        self.state = kw.get("state") or QUEUED
        self.size: int | None = kw.get("size")
        self.written: int = kw.get("written") or 0
        self.speed_bps: float = kw.get("speed_bps") or 0.0
        self.error: str | None = kw.get("error")
        self.cached: bool = bool(kw.get("cached"))  # already in the cache (or put by hand): nothing was downloaded
        self.created: float = kw.get("created") or time.time()
        self.started: float | None = kw.get("started")
        self.finished: float | None = kw.get("finished")
        self.cancel_requested = threading.Event()
        self.settled = threading.Event()  # set once the job left running
        self.download: Download | None = None
        if self.state in _FINISHED:
            self.settled.set()

    def view(self) -> dict:
        return {k: getattr(self, k) for k in self.PERSISTED}


class Prefetcher:
    """Downloads files into cache/ in the background through Mirror.download, the path a node's request takes, so the
    file lands as a node's request would leave it (state, LRU, one download per file shared with the nodes). At most
    `concurrency` run at once. The list is kept in .mirror/prefetch.json: queued and running jobs continue after a restart
    (the part of a running one is resumed with Range)."""

    def __init__(self, mirror: Mirror, concurrency: int = PREFETCH_CONCURRENCY):
        self.mirror = mirror
        self.concurrency = max(1, concurrency)
        self.file = mirror.root / ".mirror" / "prefetch.json"
        self.lock = threading.RLock()
        self.jobs: list[PrefetchJob] = []
        self.workers: list[threading.Thread] = []
        self.stopping = False
        self._load()

    # ---- persistence --------------------------------------------------------------------

    def _load(self) -> None:
        data = _read_json(self.file) or {}
        for raw in data.get("jobs") or []:
            try:
                fields = {k: raw[k] for k in PrefetchJob.PERSISTED if k in raw and k not in ("repo", "revision", "path")}
                job = PrefetchJob(check_repo(raw["repo"]), check_revision(raw["revision"]), check_path(raw["path"]), **fields)
            except (KeyError, TypeError, HttpError):
                continue
            if job.state == RUNNING:  # the mirror died under it: starts again, from the part
                job.state, job.started = QUEUED, None
            if job.state in (QUEUED, *_FINISHED):
                self.jobs.append(job)

    def _save(self) -> None:
        try:
            _write_json(self.file, {"jobs": [j.view() for j in self.jobs]})
        except OSError as e:
            log(f"prefetch list not saved: {e!r}")

    # ---- the API ------------------------------------------------------------------------

    def start(self) -> None:
        with self.lock:
            self._spawn()

    def add(self, repo: str, revision: str, files: list[str]) -> list[PrefetchJob]:
        """Queues `files` of `repo`; a file that is queued or running already gives its job back."""
        check_repo(repo)
        if is_local(repo):
            raise HttpError(400, "Свои модели (local/…) лежат на главном и в кэш не качаются")
        check_revision(revision)
        paths = [check_path(p) for p in files]
        with self.lock:
            out = []
            for path in dict.fromkeys(paths):
                job = next((j for j in self.jobs if j.repo == repo and j.path == path and j.state in (QUEUED, RUNNING)), None)
                if job is None:
                    self.jobs = [j for j in self.jobs if not (j.repo == repo and j.path == path)]  # an old result
                    job = PrefetchJob(repo, revision, path)
                    self.jobs.append(job)
                out.append(job)
            self._trim()
            self._save()
            self._spawn()
            return out

    def listing(self) -> dict:
        with self.lock:
            return {"jobs": [j.view() for j in self.jobs], "concurrency": self.concurrency}

    def cancel(self, job_id: str) -> PrefetchJob:
        """Stops a queued or running job (the part of a running one stays, resumable); removes a finished one."""
        with self.lock:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if job is None:
                raise HttpError(404, "Нет такого задания")
            if job.state in _FINISHED:
                self.jobs.remove(job)
                self._save()
                return job
            if job.state == QUEUED:
                self._settle(job, CANCELLED, MSG_CANCELLED)
                return job
            job.cancel_requested.set()
            download = job.download
        if download is not None:
            with download.cond:
                download.cond.notify_all()
        job.settled.wait(10)
        return job

    def clear(self) -> int:
        """Removes the finished jobs from the list."""
        with self.lock:
            kept = [j for j in self.jobs if j.state not in _FINISHED]
            removed = len(self.jobs) - len(kept)
            self.jobs = kept
            self._save()
            return removed

    def stop(self) -> None:
        with self.lock:
            self.stopping = True
            running = [j.download for j in self.jobs if j.state == RUNNING and j.download is not None]
            workers = list(self.workers)
        for download in running:
            with download.cond:
                download.cond.notify_all()
        for t in workers:
            t.join(10)

    # ---- workers ------------------------------------------------------------------------

    def _trim(self) -> None:
        finished = sorted((j for j in self.jobs if j.state in _FINISHED), key=lambda j: j.finished or 0)
        for job in finished[: max(0, len(finished) - PREFETCH_HISTORY)]:
            self.jobs.remove(job)

    def _spawn(self) -> None:
        self.workers = [t for t in self.workers if t.is_alive()]
        queued = sum(1 for j in self.jobs if j.state == QUEUED)
        while not self.stopping and len(self.workers) < min(self.concurrency, queued):
            t = threading.Thread(target=self._work, name="prefetch", daemon=True)
            self.workers.append(t)
            t.start()

    def _next(self) -> PrefetchJob | None:
        with self.lock:
            job = next((j for j in self.jobs if j.state == QUEUED), None)
            if job is None or self.stopping:
                self.workers = [t for t in self.workers if t is not threading.current_thread()]
                return None
            job.state, job.started, job.error = RUNNING, time.time(), None
            job.written, job.speed_bps = 0, 0.0
            job.cancel_requested.clear()
            job.settled.clear()
            self._save()
            return job

    def _work(self) -> None:
        while (job := self._next()) is not None:
            try:
                self._run(job)
            except Exception as e:  # noqa: BLE001 - one bad job must not stop the worker
                log(f"prefetch of {job.repo}/{job.path} failed: {e!r}")
                self._settle(job, FAILED, f"ошибка зеркала: {e}")

    def _settle(self, job: PrefetchJob, state: str, error: str | None = None) -> None:
        with self.lock:
            if job.state in _FINISHED:
                return
            job.state, job.error, job.finished = state, error, time.time()
            if state == DONE and not job.cached and job.started and job.size:
                job.speed_bps = job.size / max(job.finished - job.started, 1e-3)
            else:
                job.speed_bps = 0.0
            self._trim()
            self._save()
        job.settled.set()

    def _requeue(self, job: PrefetchJob) -> None:
        """The mirror is stopping: the job waits for the next start."""
        with self.lock:
            job.state, job.started = QUEUED, None
            self._save()
        job.settled.set()

    def _run(self, job: PrefetchJob) -> None:
        mirror = self.mirror
        found = mirror.find(job.repo, job.path)
        if found is not None:  # already here: put by hand, or downloaded earlier (and still the same upstream)
            f, by_hand = found
            state = None if by_hand else mirror.read_state(job.repo, job.path)
            if state is not None and mirror.should_check(state, job.revision):
                state = mirror.revalidate(job.repo, job.revision, job.path, state)
            if state is None or not state.get("stale"):
                try:
                    size = f.stat().st_size
                except OSError:  # deleted by the admin just now: download it again
                    size = None
                if size is not None:
                    if state is not None:
                        mirror.touch(job.repo, job.path, accessed=time.time())  # asked for: keep it longer
                    job.size = job.written = size
                    job.cached = True
                    return self._settle(job, DONE)
        download = mirror.download(job.repo, job.path, job.revision)
        job.download = download
        # the cache is keyed by path: a download that was already running for another revision (a pin against main) is not
        # this job's file; wait for it to end and look again, the check against the pinned commit then downloads what is needed
        foreign = download.revision != job.revision and (_COMMIT.fullmatch(job.revision) or _COMMIT.fullmatch(download.revision))
        last_at, last_bytes = time.monotonic(), download.written
        while True:
            with download.cond:
                if not (download.done or download.error is not None or job.cancel_requested.is_set() or self.stopping):
                    download.cond.wait(timeout=0.25)  # a chunk, a cancel or the mirror stopping wakes it earlier
                written, size, done, error = download.written, download.size, download.done, download.error
            job.size, job.written = size, written
            now = time.monotonic()
            if now - last_at >= 0.5:
                rate = (written - last_bytes) / (now - last_at)
                job.speed_bps = rate if job.speed_bps == 0 else 0.6 * job.speed_bps + 0.4 * rate
                last_at, last_bytes = now, written
            if done and foreign:
                job.download = None
                return self._run(job)
            if done:
                job.size = job.written = size or written
                return self._settle(job, DONE)
            if error is not None and foreign and not job.cancel_requested.is_set():
                job.download = None
                return self._run(job)
            if error is not None:
                return self._settle(job, CANCELLED if job.cancel_requested.is_set() else FAILED, error.message)
            if job.cancel_requested.is_set() or self.stopping:
                self._detach(download)
                if job.cancel_requested.is_set():
                    return self._settle(job, CANCELLED, MSG_CANCELLED)
                return self._requeue(job)

    def _detach(self, download: Download) -> None:
        """Stops the download unless a node reads it (then it goes on for the node and only this job lets go)."""
        with self.mirror.lock:
            if self.mirror.readers.get(download.final, 0) == 0 and download.nodes == 0:
                download.cancel()


# ---------------------------------------------------------------- HTTP front end


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "nsu-llm-hf-mirror"
    mirror: Mirror  # set by make_server

    def log_message(self, fmt, *args):
        pass

    def send_response(self, code, message=None):
        self.responded = True
        super().send_response(code, message)

    def do_HEAD(self):
        self._dispatch(head=True)

    def do_GET(self):
        self._dispatch(head=False)

    def do_POST(self):
        self._dispatch(head=False)

    def do_DELETE(self):
        self._dispatch(head=False)

    # ---- plumbing ------------------------------------------------------------------

    def _dispatch(self, head: bool) -> None:
        self.responded = False
        self.body_read = False
        try:
            self._route(head)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001
            if self.responded:  # the body is cut short: the client must not wait for the rest
                log(f"{self.command} {self.path} broke off: {e!r}")
                self.close_connection = True
                return
            status, message = (e.status, e.message) if isinstance(e, HttpError) else (500, "internal error of the mirror")
            if not isinstance(e, HttpError):
                log(f"{self.command} {self.path} failed: {e!r}")
            try:
                self._send_json(status, {"error": message}, head)
            except OSError:
                self.close_connection = True
        finally:
            self._drain_body()

    def _drain_body(self) -> None:
        """Reads the body of a refused request: unread bytes would be taken for the next request, and closing a socket
        with unread input resets it, which can cost the client the answer."""
        if self.body_read:
            return
        length = self.headers.get("Content-Length") or "0"
        if self.headers.get("Transfer-Encoding") or not length.isdigit() or int(length) > ADMIN_BODY_MAX:
            self.close_connection = True
            return
        try:
            self.connection.settimeout(10)
            self.rfile.read(int(length))
            self.connection.settimeout(None)
            self.body_read = True
        except OSError:
            self.close_connection = True

    def _send_json(self, status: int, obj, head: bool, extra: dict | None = None) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _authorize(self) -> None:
        """Sets self.scope: None for the admin key (or a mirror without keys), else the repositories of a scoped token
        signed with HF_MIRROR_TOKEN; 401 for anything else."""
        self.scope = None
        admin, signing = self.mirror.admin_token, self.mirror.mirror_token
        if not admin and not signing:
            return
        got = self.headers.get("Authorization", "")
        if admin and hmac.compare_digest(got.encode(), f"Bearer {admin}".encode()):
            return
        scoped = scoped_token(signing, got[7:]) if got.startswith("Bearer ") else None
        if scoped is None:
            raise HttpError(401, "Invalid credentials in Authorization header")
        client, self.scope = scoped
        # a token minted for one node (standalone heartbeat) works only under that node's /c/<name> prefix
        if client != "*" and self.client_prefix != f"/c/{client}":
            raise HttpError(403, f"Токен зеркала выдан ноде {client}: запрос должен идти от её имени (/c/{client}/...)")

    def _need_repo(self, repo: str) -> None:
        if self.scope is not None and repo not in self.scope:
            raise HttpError(403, f"Токен зеркала не открывает репозиторий {repo}: его выдаёт портал для моделей развёртываний ноды")

    def _need_full(self) -> None:
        if self.scope is not None:
            raise HttpError(403, "Нужен ключ портала к зеркалу (HF_MIRROR_ADMIN_TOKEN), а не токен ноды")

    def _route(self, head: bool) -> None:
        split = urllib.parse.urlsplit(self.path)
        raw = split.path
        client = ""
        m = re.match(r"/c/([^/]*)(/.*)$", raw)
        if m:
            if not _CLIENT.fullmatch(m.group(1)):
                raise HttpError(400, "invalid client name")
            client, raw = m.group(1), m.group(2)
        self.client = client or f"ip:{(self.headers.get('X-Forwarded-For') or self.client_address[0]).split(',')[0].strip()}"
        self.client_prefix = f"/c/{client}" if client else ""
        if raw == "/_mirror/health":
            return self._send_json(200, {"status": "ok"}, head)
        self._authorize()
        if raw == "/_mirror/transfers":
            self._need_full()
            return self._send_json(200, self.mirror.report(), head)
        if re.fullmatch(r"/admin/(cache|prefetch)(/.*)?", raw) and (self.command in ("POST", "DELETE") or raw in _ADMIN_GET):
            self._need_full()
            return self._admin(raw, head)
        if self.command not in ("GET", "HEAD"):
            raise HttpError(405, "method not allowed")
        if raw.startswith("/api/"):
            m_api = re.match(r"/api/models/([^/]+)/([^/]+)", raw)
            if self.scope is not None:
                if not m_api:
                    raise HttpError(403, "Токен ноды открывает только метаданные своих репозиториев")
                self._need_repo(check_repo(urllib.parse.unquote(m_api.group(1)) + "/" + urllib.parse.unquote(m_api.group(2))))
            return self._api(raw, split.query, head)
        segments = raw.split("/")
        # ['', owner, repo, 'resolve', revision, path...]
        if len(segments) >= 6 and segments[3] == "resolve":
            repo = check_repo(urllib.parse.unquote(segments[1]) + "/" + urllib.parse.unquote(segments[2]))
            self._need_repo(repo)
            revision = urllib.parse.unquote(segments[4])
            path = check_path(urllib.parse.unquote("/".join(segments[5:])))
            return self._file(repo, revision, path, head)
        raise HttpError(404, "not found")

    # ---- admin API of the portal (task 2-29) ----------------------------------------------

    def _json_body(self) -> dict:
        length = self.headers.get("Content-Length") or ""
        if not length.isdigit():
            raise HttpError(411, "Content-Length is required")
        if int(length) > ADMIN_BODY_MAX:
            raise HttpError(413, "request body too large")
        data = self.rfile.read(int(length))
        self.body_read = True
        try:
            body = json.loads(data)
        except ValueError as e:
            raise HttpError(400, "the body is not JSON") from e
        if not isinstance(body, dict):
            raise HttpError(400, "the body is not a JSON object")
        return body

    def _admin(self, raw: str, head: bool) -> None:
        # the portal on main connects to 127.0.0.1; whatever came through Caddy (the nodes have the mirror key too)
        # carries X-Forwarded-* and is refused
        if self.client_prefix or any(self.headers.get(h) for h in _PROXY_HEADERS):
            raise HttpError(403, "Админский API зеркала доступен только изнутри главного, не через Caddy")
        mirror, method = self.mirror, self.command
        parts = [urllib.parse.unquote(p) for p in raw.split("/")[2:]]
        if parts[0] == "cache":
            if method in ("GET", "HEAD") and len(parts) == 1:
                return self._send_json(200, mirror.cache_listing(), head)
            if method == "DELETE" and len(parts) >= 3:
                repo = check_repo(parts[1] + "/" + parts[2])
                return self._send_json(200, mirror.delete_cache(repo, "/".join(parts[3:]) or None), head)
        elif method in ("GET", "HEAD") and len(parts) == 1:
            return self._send_json(200, mirror.prefetch.listing(), head)
        elif method == "POST" and len(parts) == 1:
            body = self._json_body()
            repo, revision, files = body.get("repo"), body.get("revision") or "main", body.get("files")
            if not isinstance(repo, str) or not isinstance(revision, str):
                raise HttpError(400, "repo and revision are strings")
            if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
                raise HttpError(400, "files is a non-empty list of paths")
            if len(files) > PREFETCH_MAX_FILES:
                raise HttpError(400, f"at most {PREFETCH_MAX_FILES} files in one request")
            jobs = mirror.prefetch.add(repo, revision, files)
            return self._send_json(202, {"jobs": [j.view() for j in jobs]}, head)
        elif method == "DELETE" and len(parts) == 1:
            return self._send_json(200, {"removed": mirror.prefetch.clear()}, head)
        elif method == "DELETE" and len(parts) == 2 and _JOB_ID.fullmatch(parts[1]):
            return self._send_json(200, mirror.prefetch.cancel(parts[1]).view(), head)
        raise HttpError(404, "not found")

    # ---- metadata API --------------------------------------------------------------

    def _external_base(self) -> str:
        """Address of the mirror as the client sees it: behind Caddy (X-Forwarded-Proto) with the /hf prefix, else
        direct (the portal on main)."""
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "127.0.0.1"
        proto = self.headers.get("X-Forwarded-Proto")
        if proto:
            return f"{proto.split(',')[0].strip()}://{host}{self.mirror.prefix}{self.client_prefix}"
        return f"http://{host}{self.client_prefix}"

    def _api(self, path: str, query: str, head: bool) -> None:
        mirror = self.mirror
        # the upstream decodes %2e%2e and %2f: a dot segment hidden in the escapes would walk out of the allowed prefix
        if {".", ".."} & set(urllib.parse.unquote(path).split("/")) or not _API_ALLOWED.fullmatch(path):
            raise HttpError(403, "Зеркало передаёт в Hugging Face только запросы метаданных моделей")
        if _LOCAL_API.fullmatch(path):  # an own model of the portal: answered from its directory, never upstream
            local = mirror.synthesize(path, urllib.parse.parse_qs(query))
            if local is None:
                return self._send_json(404, {"error": "Repository not found"}, head, {"X-Error-Code": "RepositoryNotFound"})
            return self._send_json(200, local, head)
        target = path + ("?" + query if query else "")
        cache = mirror.api_dir / (hashlib.sha1(target.encode()).hexdigest() + ".json")
        local = None
        try:
            resp, status, _ = mirror.upstream.open("GET", target, {"Accept": "application/json"})
            with resp:
                body = resp.read(API_MAX_BYTES)
                ctype = resp.headers.get("Content-Type") or "application/json"
                link = resp.headers.get("Link")
            if status == 200:
                mirror.save_api(cache, {"body": body.decode("utf-8", "replace"), "type": ctype, "link": link})
                return self._send_api(body, ctype, link, head)
            if status < 500:
                local = mirror.synthesize(path, urllib.parse.parse_qs(query))
                if local is None:
                    return self._send_api(body, ctype, None, head, status)
        except UpstreamUnavailable as e:
            log(f"metadata from the cache, Hugging Face is unreachable: {e}")
        cached = _read_json(cache)
        if cached is not None and local is None:
            return self._send_api(cached["body"].encode(), cached.get("type") or "application/json", cached.get("link"), head)
        if local is None:
            local = mirror.synthesize(path, urllib.parse.parse_qs(query))
        if local is None:
            raise HttpError(502, "Hugging Face is unreachable and the mirror has no copy of this answer")
        return self._send_json(200, local, head)

    def _send_api(self, body: bytes, ctype: str, link: str | None, head: bool, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if link:
            self.send_header("Link", link.replace(self.mirror.upstream_base, self._external_base()))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    # ---- files ---------------------------------------------------------------------

    def _file(self, repo: str, revision: str, path: str, head: bool) -> None:
        mirror = self.mirror
        if is_local(repo):  # an own model of the portal: its ready files or nothing, no download, no eviction
            manifest = mirror.own_manifest(repo)
            own = mirror.own_file(repo, path, manifest)
            if own is None:
                return self._send_json(404, {"error": "Entry not found"}, head, {"X-Error-Code": "EntryNotFound"})
            state = {"etag": mirror.own_etag(repo, path, own[1]), "commit": mirror.own_commit(repo, manifest)}
            return self._serve_cached(repo, path, own[0], True, state, head)
        with mirror.lock:
            job = mirror.downloads.get((repo, path))
            if job is not None and not job.active:
                job = None
        if job is not None and job.revision != revision and (_COMMIT.fullmatch(revision) or _COMMIT.fullmatch(job.revision)):
            # the cache is keyed by path: a download of another revision is no answer for this one (a deployment
            # pinned to a commit while another follows main, task 2-15); passed through, not cached, until it is done
            if head:
                return self._head_upstream(repo, revision, path)
            return self._range_upstream(repo, revision, path)
        found = mirror.find(repo, path) if job is None else None
        if found is not None:
            f, by_hand = found
            state = None if by_hand else mirror.read_state(repo, path)
            if state is not None and mirror.should_check(state, revision):
                state = mirror.revalidate(repo, revision, path, state)
            if state is not None and state.get("stale"):
                job = mirror.download(repo, path, revision)
            else:
                return self._serve_cached(repo, path, f, by_hand, state, head)
        if head:
            if job is not None and job.ready:
                return self._send_file_headers(200, job.size, job.etag, job.commit, None)
            return self._head_upstream(repo, revision, path)
        if job is None and re.fullmatch(r"bytes=\d+-\d+", (self.headers.get("Range") or "").strip()):
            # a bounded range of a file the mirror does not have (the portal reads a GGUF header): passed through,
            # not a reason to download the whole file
            return self._range_upstream(repo, revision, path)
        if job is None:
            job = mirror.download(repo, path, revision)
        mirror.join(job, 1)
        try:
            job.wait_ready()
            self._serve_download(job)
        finally:
            mirror.join(job, -1)

    def _range_upstream(self, repo: str, revision: str, path: str) -> None:
        """The file (or the requested range of it) straight from Hugging Face, not cached."""
        quoted = urllib.parse.quote(revision, safe="")
        rng = (self.headers.get("Range") or "").strip()
        try:
            resp, status, meta = self.mirror.upstream.open(
                "GET", f"/{repo}/resolve/{quoted}/{urllib.parse.quote(path)}", {"Range": rng} if rng else {}
            )
        except UpstreamUnavailable as e:
            raise HttpError(502, "Hugging Face is unreachable and the file is not in the mirror") from e
        with resp:
            if status not in (200, 206):
                raise HttpError(status, _upstream_message(resp.read(65536), status))
            info = _file_meta(meta, status)
            length = resp.headers.get("Content-Length")
            self._send_file_headers(
                status, info["size"], info["etag"], info["commit"], resp.headers.get("Content-Range"),
                int(length) if length and length.isdigit() else None,
            )
            while True:
                chunk = resp.read1(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _head_upstream(self, repo: str, revision: str, path: str) -> None:
        quoted = urllib.parse.quote(revision, safe="")
        try:
            resp, status, meta = self.mirror.upstream.open("HEAD", f"/{repo}/resolve/{quoted}/{urllib.parse.quote(path)}")
            resp.close()
        except UpstreamUnavailable as e:
            raise HttpError(502, "Hugging Face is unreachable and the file is not in the mirror") from e
        if status != 200:
            raise HttpError(status, f"upstream answered {status}")
        info = _file_meta(meta, status)
        self._send_file_headers(200, info["size"], info["etag"], info["commit"], None)

    def _send_file_headers(self, status: int, size: int | None, etag: str | None, commit: str | None,
                           content_range: str | None, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        if length is None:
            length = size
        if length is not None:
            self.send_header("Content-Length", str(length))
        else:
            self.close_connection = True
        if size is not None:
            self.send_header("X-Linked-Size", str(size))
        if etag:
            self.send_header("ETag", f'"{etag}"')
            self.send_header("X-Linked-Etag", f'"{etag}"')
        if commit:
            self.send_header("X-Repo-Commit", commit)
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()

    def _range(self, size: int | None) -> tuple[int, int] | None:
        """(start, end inclusive) of a single `Range: bytes=` request, None for the whole file."""
        value = (self.headers.get("Range") or "").strip()
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
        if not m or (not m.group(1) and not m.group(2)) or size is None:
            return None
        if not m.group(1):
            n = int(m.group(2))
            return (max(0, size - n), size - 1) if n else None
        start = int(m.group(1))
        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        if start >= size or end < start:
            raise _RangeNotSatisfiable(size)
        return start, end

    def _serve_cached(self, repo: str, path: str, final: Path, by_hand: bool, state: dict | None, head: bool) -> None:
        mirror = self.mirror
        size = final.stat().st_size
        if state is None:  # put by hand (or downloaded by a run that died before writing the state)
            etag = mirror.local_oid(repo, path, size, None, final)
            commit = mirror.local_commit(repo)
        else:
            etag, commit = state.get("etag") or mirror.local_oid(repo, path, size, None, final), state.get("commit")
            if not head:
                mirror.touch(repo, path, accessed=time.time())
        try:
            rng = self._range(size)
        except _RangeNotSatisfiable:
            return self._send_unsatisfiable(size)
        start, end = rng if rng else (0, size - 1)
        content_range = f"bytes {start}-{end}/{size}" if rng else None
        self._send_file_headers(206 if rng else 200, size, etag, commit, content_range, end - start + 1)
        if head:
            return
        mirror.reading(final, +1)
        try:
            # the open file keeps its data even if a newer version replaces the path meanwhile
            with open(final, "rb") as f:
                f.seek(start)
                self._copy(f, start, end + 1, repo, path, size, lambda: end + 1)
        finally:
            mirror.reading(final, -1)

    def _send_unsatisfiable(self, size: int) -> None:
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_download(self, job: Download) -> None:
        size = job.size
        try:
            rng = self._range(size)
        except _RangeNotSatisfiable:
            return self._send_unsatisfiable(size)
        start, end = rng if rng else (0, (size - 1) if size is not None else None)
        content_range = f"bytes {start}-{end}/{size}" if rng else None
        length = (end - start + 1) if end is not None else None
        self._send_file_headers(206 if rng else 200, size, job.etag, job.commit, content_range, length)
        stop = (end + 1) if end is not None else None

        def available() -> int | None:
            """How far the data reaches; waits while the download makes progress, gives up STALL_S after its last
            byte (not after the reader's own position: a range far ahead may wait long for a moving download)."""
            with job.cond:
                while True:
                    if job.written > pos[0] or job.done:
                        return job.written if stop is None else min(job.written, stop)
                    if job.error is not None:
                        raise HttpError(job.error.status, "download interrupted")
                    idle = time.monotonic() - job.progress_at
                    if idle >= STALL_S:
                        raise HttpError(504, "download stalled")
                    job.cond.wait(timeout=min(5.0, STALL_S - idle))

        pos = [start]
        self.mirror.reading(job.final, +1)  # the finished file stays while this request still sends it
        try:
            self._copy(_JobReader(job, start), start, stop, job.repo, job.path, size, available, pos)
        finally:
            self.mirror.reading(job.final, -1)

    def _copy(self, f, start: int, stop: int | None, repo: str, path: str, size: int | None, available, pos=None) -> None:
        """Sends bytes [start, stop) of `f`; `available()` is how far the data reaches right now."""
        pos = pos if pos is not None else [start]
        transfers = self.mirror.transfers
        last = 0.0
        try:
            while stop is None or pos[0] < stop:
                try:
                    limit = available()
                except HttpError:
                    self.close_connection = True  # the client sees a short body and retries with Range
                    return
                if limit is None or limit <= pos[0]:
                    break  # unknown size and the download is complete
                while pos[0] < limit:
                    data = f.read(min(CHUNK, limit - pos[0]))
                    if not data:
                        self.close_connection = True
                        return
                    self.wfile.write(data)
                    pos[0] += len(data)
                    now = time.monotonic()
                    if now - last > 1:
                        last = now
                        transfers.update(self.client, repo, path, pos[0], size, True)
        finally:
            transfers.update(self.client, repo, path, pos[0], size, False)


class _RangeNotSatisfiable(Exception):
    def __init__(self, size: int):
        super().__init__(size)
        self.size = size


def make_server(mirror: Mirror, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"mirror": mirror})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def max_gib_setting(env) -> float:
    """The cache limit in GiB: HF_MIRROR_MAX_GIB_OVERRIDE (the value from the portal panel, from a ConfigMap; empty or
    absent when none was set) wins over HF_MIRROR_MAX_GIB of the .env."""
    return float(env.get("HF_MIRROR_MAX_GIB_OVERRIDE") or env.get("HF_MIRROR_MAX_GIB", "500"))


def main() -> int:
    env = os.environ
    try:
        max_gib = max_gib_setting(env)
        port = int(env.get("HF_MIRROR_PORT", "8090"))
        revalidate_s = float(env.get("HF_MIRROR_REVALIDATE_S", "3600"))
    except ValueError as e:
        print(f"hf-mirror: bad setting: {e}", file=sys.stderr)
        return 2
    mirror = Mirror(
        root=env.get("HF_MIRROR_DIR", "/data"),
        upstream=env.get("HF_MIRROR_UPSTREAM", "https://huggingface.co"),
        upstream_token=env.get("HF_TOKEN", ""),
        mirror_token=env.get("HF_MIRROR_TOKEN", ""),
        admin_token=env.get("HF_MIRROR_ADMIN_TOKEN", ""),
        max_bytes=int(max_gib * (1 << 30)),
        revalidate_s=revalidate_s,
        prefix=env.get("HF_MIRROR_PREFIX", "/hf"),
    )
    server = make_server(mirror, env.get("HF_MIRROR_HOST", "127.0.0.1"), port)
    log(f"serving {mirror.root} on {server.server_address[0]}:{port}, limit {max_gib:g} GiB, upstream {mirror.upstream_base}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
