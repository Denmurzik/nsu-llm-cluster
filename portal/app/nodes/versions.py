"""Versions and updates of the nodes (task 2-30).

Three things live here:

* What main's image registry offers for the nodes (`REGISTRY`): the digest and the labels (version, revision) of the tag
  `<NODE_IMAGE_TAG>-cpu|gpu`. The workload builders pin the pods to that digest, so a new image under the same tag
  reaches the nodes by itself (a new pod template) and the pods stay where they are while the image does not change.
* What every node reports about the node container it runs (`POST /api/nodes/<name>/image`): version, revision, digest.
  They sit in Redis (the node reports at every start of its container, so a lost key comes back by itself).
* The update requests of the admin and the answer to the node's question «should I replace my container now»
  (`GET /api/nodes/<name>/desired`): the node itself (run-node -Update, started by node-boot.ps1 or a timer) pulls the
  image from main's registry and re-creates `llm-node`; main only decides, never serving unless the admin forced it.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import ModelDeployment, NodeAssignment

log = logging.getLogger(__name__)

SUFFIX = {"cpu": "cpu", "nvidia": "gpu"}
LOCAL_REPO = "nsu-llm-node"  # how a node names the image locally: nsu-llm-node:<version>-cpu|gpu (run-node, pull-image.sh)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

FRESH_S = 60.0  # an answer of the registry is reused this long
RETRY_S = 10.0  # after a failed question the registry is not asked again for this long
HTTP_TIMEOUT_S = 5.0
PIN_WAIT_S = 120.0  # how long the workload controller waits for the registry to say anything before it builds pods without a pin
TAGS_SHOWN = 24
REPORT_TTL_S = 400 * 86400
REQUEST_TTL_S = 7 * 86400
KEY_REPORT = "node:img:"  # Redis: what a node runs (JSON)
KEY_REQUEST = "node:upd:"  # Redis: the admin's update request for a node (JSON)
KEY_PIN = "node:pin:"  # Redis: the last digest main's registry reported for a tag, to survive a restart of the portal

WHENS = ("idle", "now")  # «после выключения развёртывания», «сейчас»

_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
_LABEL_VERSION = "org.opencontainers.image.version"
_LABEL_REVISION = "org.opencontainers.image.revision"


class RegistryError(Exception):
    """The registry did not answer or answered something that is not an image."""


@dataclass(frozen=True)
class ImageInfo:
    """One tag of the node image as main's registry has it."""

    tag: str
    digest: str
    version: str = ""
    revision: str = ""
    created: str = ""


def tag_for(s: Settings, accel: str) -> str:
    """`0.1.0-gpu` for an nvidia node, `0.1.0-cpu` for anything else."""
    return f"{s.node_image_tag}-{SUFFIX.get(accel, 'cpu')}"


def local_name(s: Settings, accel: str) -> str:
    """The name of the image on the node's own Docker: `nsu-llm-node:0.1.0-gpu`."""
    return f"{LOCAL_REPO}:{tag_for(s, accel)}"


class Registry:
    """Client and cache of main's image registry; one per process (the workload builders read the cache without awaiting).

    A tag is `ok` (digest known), `absent` (the registry answered 404) or unknown (nothing known yet). An error keeps what
    was known: a registry that is down for a minute must not turn the pinned pods into tag pods (a new pod template)."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport
        self.reset()

    def reset(self) -> None:
        self._cache: dict[tuple[str, str, str], tuple[ImageInfo | None, float]] = {}
        self._tried: dict[tuple[str, str, str], float] = {}
        self._first_refresh: float | None = None
        self._tags: tuple[list[str], float] | None = None

    # -- reads of the cache (no I/O) --------------------------------------------------

    @staticmethod
    def _key(s: Settings, tag: str) -> tuple[str, str, str]:
        return (s.registry_base, s.registry_repo, tag)

    def put(self, s: Settings, tag: str, info: ImageInfo | None) -> None:
        """Records an answer of the registry (None: it has no such tag)."""
        self._cache[self._key(s, tag)] = (info, time.monotonic())

    def known(self, s: Settings, tag: str) -> bool:
        return self._key(s, tag) in self._cache

    def cached(self, s: Settings, tag: str) -> ImageInfo | None:
        entry = self._cache.get(self._key(s, tag))
        return entry[0] if entry else None

    def digest(self, s: Settings, tag: str) -> str | None:
        """The digest to pin the pods to, or None (no tag in the registry, or nothing known yet)."""
        info = self.cached(s, tag)
        return info.digest if info else None

    # -- the registry -----------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, timeout=HTTP_TIMEOUT_S, trust_env=False)

    async def _get(self, http: httpx.AsyncClient, s: Settings, path: str, **kw: Any) -> httpx.Response:
        try:
            return await http.get(f"{s.registry_base}/v2/{s.registry_repo}/{path}", **kw)
        except httpx.HTTPError as exc:
            raise RegistryError(f"{type(exc).__name__}: {exc}") from exc

    async def fetch(self, s: Settings, tag: str) -> ImageInfo | None:
        """Asks the registry about a tag: its info, None when the registry has no such tag; RegistryError otherwise."""
        async with self._client() as http:
            r = await self._get(http, s, f"manifests/{tag}", headers={"Accept": _ACCEPT})
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                raise RegistryError(f"manifests/{tag}: HTTP {r.status_code}")
            digest = r.headers.get("Docker-Content-Digest") or "sha256:" + hashlib.sha256(r.content).hexdigest()
            if not DIGEST_RE.match(digest):
                raise RegistryError(f"manifests/{tag}: bad digest {digest[:80]!r}")
            labels: dict[str, Any] = {}
            created = ""
            try:
                manifest = r.json()
                if isinstance(manifest.get("manifests"), list) and manifest["manifests"]:  # an index: the linux/amd64 image
                    entries = manifest["manifests"]
                    pick = next(
                        (m for m in entries if (m.get("platform") or {}).get("architecture") == "amd64"), entries[0]
                    )
                    r2 = await self._get(http, s, f"manifests/{pick['digest']}", headers={"Accept": _ACCEPT})
                    manifest = r2.json() if r2.status_code == 200 else {}
                config = (manifest.get("config") or {}).get("digest")
                if config:
                    r3 = await self._get(http, s, f"blobs/{config}")
                    if r3.status_code == 200:
                        blob = r3.json()
                        labels = (blob.get("config") or {}).get("Labels") or {}
                        created = str(blob.get("created") or "")
            except (ValueError, KeyError, TypeError, AttributeError, RegistryError):
                labels = {}  # the digest is what matters; the labels only decorate the page
            return ImageInfo(
                tag, digest, str(labels.get(_LABEL_VERSION) or ""), str(labels.get(_LABEL_REVISION) or ""), created
            )

    async def list_tags(self, s: Settings) -> list[str]:
        """The tags of the node image repository; [] when it does not exist yet. RegistryError when the registry is down."""
        now = time.monotonic()
        if self._tags is not None and now - self._tags[1] < FRESH_S:
            return self._tags[0]
        async with self._client() as http:
            r = await self._get(http, s, "tags/list")
        if r.status_code == 404:
            tags: list[str] = []
        elif r.status_code == 200:
            try:
                tags = [t for t in (r.json().get("tags") or []) if isinstance(t, str)]
            except (ValueError, AttributeError):
                raise RegistryError("tags/list: not JSON") from None
        else:
            raise RegistryError(f"tags/list: HTTP {r.status_code}")
        self._tags = (tags, now)
        return tags

    async def resolve(self, s: Settings, tag: str, redis: Any = None) -> ImageInfo | None:
        """The info of a tag from the cache or, when it is stale, from the registry. An error keeps the old answer; the
        last digest in Redis stands in for the memory of a restarted process."""
        key = self._key(s, tag)
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry is not None and now - entry[1] < FRESH_S:
            return entry[0]
        if now - self._tried.get(key, -RETRY_S) < RETRY_S and entry is not None:
            return entry[0]
        self._tried[key] = now
        try:
            info = await self.fetch(s, tag)
        except RegistryError as exc:
            log.warning("node image registry: %s; keeping %s", exc, "the known answer" if entry else "no digest")
            if entry is None and redis is not None:
                entry = await self._seed(s, tag, redis, now)
            return entry[0] if entry else None
        self.put(s, tag, info)
        if redis is not None:
            await self._remember(s, tag, redis, info, entry)
        return info

    async def _seed(self, s: Settings, tag: str, redis: Any, now: float) -> tuple[ImageInfo | None, float] | None:
        try:
            digest = await redis.get(f"{KEY_PIN}{s.registry_repo}:{tag}")
        except Exception:  # Redis down: no seed
            return None
        if not digest or not DIGEST_RE.match(digest):
            return None
        entry = (ImageInfo(tag, digest), now - FRESH_S)  # stale at once: the next question asks the registry
        self._cache[self._key(s, tag)] = entry
        return entry

    async def _remember(
        self, s: Settings, tag: str, redis: Any, info: ImageInfo | None, before: tuple[ImageInfo | None, float] | None
    ) -> None:
        old = before[0].digest if before and before[0] else None
        new = info.digest if info else None
        if old == new and before is not None:
            return
        key = f"{KEY_PIN}{s.registry_repo}:{tag}"
        try:
            if new:
                await redis.set(key, new)
            else:
                await redis.delete(key)
        except Exception:
            log.warning("node image registry: the digest of %s could not be kept in Redis", tag)

    async def refresh_pins(self, state: Any) -> bool:
        """Before the workload controller builds pods: learn the digests of the node image tags. False: the registry told
        nothing yet and the process is young, so the controller skips this round instead of building pods with a tag and
        re-templating them a minute later (every model would reload)."""
        s: Settings = state.settings
        if not s.node_image_pin:
            return True
        if self._first_refresh is None:
            self._first_refresh = time.monotonic()
        redis = getattr(state, "redis", None)
        tags = [tag_for(s, accel) for accel in SUFFIX]
        await asyncio.gather(*(self.resolve(s, t, redis) for t in tags))
        if all(self.known(s, t) for t in tags):
            return True
        return time.monotonic() - self._first_refresh >= PIN_WAIT_S


REGISTRY = Registry()


async def refresh_pins(state: Any) -> bool:
    return await REGISTRY.refresh_pins(state)


def pinned_digest(s: Settings, accel: str) -> str | None:
    """The digest the pods of this accel run, or None: the tag form (builders._image)."""
    if not s.node_image_pin:
        return None
    return REGISTRY.digest(s, tag_for(s, accel))


# ---------------------------------------------------------------- what a node runs, what the admin asked


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _get_json(redis: Any, key: str) -> dict[str, Any] | None:
    try:
        raw = await redis.get(key)
    except Exception as exc:
        log.warning("node versions: Redis unavailable: %s", exc)
        return None
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def _many(redis: Any, prefix: str, names: list[str]) -> dict[str, dict[str, Any]]:
    if not names:
        return {}
    try:
        raws = await redis.mget([prefix + n for n in names])
    except Exception as exc:
        log.warning("node versions: Redis unavailable: %s", exc)
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, raw in zip(names, raws, strict=True):
        try:
            value = json.loads(raw) if raw else None
        except ValueError:
            value = None
        if isinstance(value, dict):
            result[name] = value
    return result


async def load_reports(redis: Any, names: list[str]) -> dict[str, dict[str, Any]]:
    return await _many(redis, KEY_REPORT, names)


async def load_requests(redis: Any, names: list[str]) -> dict[str, dict[str, Any]]:
    return await _many(redis, KEY_REQUEST, names)


async def save_report(
    redis: Any, name: str, *, version: str, revision: str, digest: str, tag: str, update_error: str
) -> dict[str, Any]:
    """Keeps what the node runs. A failed update is remembered (and delays the next try); a report that carries a
    different digest than before means the container was replaced, so the failure is gone."""
    before = await _get_json(redis, KEY_REPORT + name) or {}
    report: dict[str, Any] = {
        "version": version,
        "revision": revision,
        "digest": digest,
        "tag": tag,
        "reported_at": _now(),
        "update_error": "",
        "update_failed_at": None,
    }
    if update_error:
        # the updater of the host reports a failure without knowing the container's labels: what was known stays
        for key in ("version", "revision", "digest", "tag"):
            report[key] = report[key] or before.get(key, "")
        report["update_error"] = update_error
        report["update_failed_at"] = _now()
    elif before.get("update_failed_at") and before.get("digest") == digest:
        report["update_error"] = before.get("update_error", "")
        report["update_failed_at"] = before["update_failed_at"]
    await redis.set(KEY_REPORT + name, json.dumps(report), ex=REPORT_TTL_S)
    return report


async def set_request(redis: Any, name: str, when: str, by: str) -> dict[str, Any]:
    request = {"when": when, "at": _now(), "by": by}
    await redis.set(KEY_REQUEST + name, json.dumps(request), ex=REQUEST_TTL_S)
    # a new request is a new chance: the delay after an earlier failure does not apply to it
    report = await _get_json(redis, KEY_REPORT + name)
    if report and report.get("update_failed_at"):
        report["update_failed_at"] = None
        report["update_error"] = ""
        await redis.set(KEY_REPORT + name, json.dumps(report), ex=REPORT_TTL_S)
    return request


async def mark_attempted(redis: Any, name: str) -> None:
    """The node was told to go for a request that cannot be checked against the registry: the one try is spent."""
    request = await _get_json(redis, KEY_REQUEST + name)
    if request:
        request["attempted"] = _now()
        await redis.set(KEY_REQUEST + name, json.dumps(request), ex=REQUEST_TTL_S)


async def clear_request(redis: Any, name: str) -> bool:
    return bool(await redis.delete(KEY_REQUEST + name))


def outdated(report: dict[str, Any] | None, offered: ImageInfo | None) -> bool | None:
    """Does the node run another image than main's registry offers for it? None when that cannot be told: no report, no
    such tag in the registry, or an image that carries no digest and no comparable labels (loaded from an archive)."""
    if not report or offered is None:
        return None
    digest = report.get("digest") or ""
    if digest:
        return digest != offered.digest
    revision, version = report.get("revision") or "", report.get("version") or ""
    if _real(revision) and _real(offered.revision):
        return revision != offered.revision
    if _real(version) and _real(offered.version) and version != offered.version:
        return True
    return None


def _real(label: str) -> bool:
    return bool(label) and label not in ("unknown", "dev")


async def serving_nodes(db: AsyncSession) -> set[str]:
    """Nodes whose container must not be replaced without the admin's «сейчас»: assigned to an enabled deployment and
    not on maintenance. A node of a disabled deployment, or without one, is idle."""
    stmt = (
        select(NodeAssignment.node_name)
        .join(ModelDeployment, ModelDeployment.id == NodeAssignment.deployment_id)
        .where(ModelDeployment.enabled.is_(True), NodeAssignment.maintenance.is_(False))
    )
    return {n.lower() for n in (await db.scalars(stmt)).all()}


@dataclass
class Verdict:
    """The answer to «should this node replace its container now»."""

    go: bool
    mode: str | None  # requested | auto | None
    when: str | None  # idle | now (requests only)
    waiting: bool  # wanted but held back: the node serves, or the last try failed
    reason: str = ""


def decide(
    s: Settings,
    report: dict[str, Any] | None,
    request: dict[str, Any] | None,
    offered: ImageInfo | None,
    serving: bool,
    auto_update: bool = False,
    now: datetime | None = None,
) -> Verdict:
    """Main decides, the node obeys. A request of the admin («сейчас» goes at once, «после выключения развёртывания»
    when the node serves nothing) or, with the setting node_auto_update (settings_store: the admin's value, else NODE_AUTO_UPDATE),
    an outdated node that serves nothing. A node that is already
    on the offered image has nothing to do whatever was asked. After a failed update the node waits NODE_UPDATE_RETRY_S."""
    if offered is None:
        return Verdict(False, None, None, False, "в реестре главного нет образа ноды")
    stale = outdated(report, offered)
    if stale is False:
        return Verdict(False, None, None, False, "нода уже на актуальном образе")
    if request and request.get("attempted") and stale is None:
        # a request that cannot be verified got its one try (main/node version labels are not comparable): done, not endless
        return Verdict(False, None, None, False, "запрос выполнен (образ ноды с реестром не сравнить)")
    when = request.get("when") if request else None
    mode = "requested" if request else ("auto" if auto_update and stale else None)
    if mode is None:
        return Verdict(False, None, None, False, "")
    failed = (report or {}).get("update_failed_at")
    if failed:
        try:
            age = ((now or datetime.now(UTC)) - datetime.fromisoformat(failed)).total_seconds()
        except ValueError:
            age = s.node_update_retry_s
        if age < s.node_update_retry_s:
            return Verdict(False, mode, when, True, "предыдущая попытка не удалась, повтор позже")
    if when == "now":
        return Verdict(True, mode, when, False)
    if serving:
        return Verdict(False, mode, when, True, "нода обслуживает развёртывание")
    return Verdict(True, mode, when, False)
