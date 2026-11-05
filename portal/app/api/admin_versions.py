"""Versions and node updates (task 2-30).

Admin side (`router`, browser session): the «Версии» block of «Состояние», the version tags of «Ноды», the «Обновить»
buttons. Node side (`node_router`, bearer agent token like app.nodes.routes): what a node tells about its container and the
question «should I replace it now» that run-node -Update asks (node-boot.ps1 every minute, a timer on Linux).
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, settings_store
from app.api.admin import collect_nodes
from app.auth.sessions import require_admin, require_csrf
from app.config import Settings
from app.db import get_session
from app.deps import get_k8s, get_settings_dep
from app.models import User
from app.deployments import service
from app.k8s import MAIN_NODE
from app.nodes import identity, versions
from app.nodes.routes import require_node_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])
node_router = APIRouter(prefix="/api/nodes", dependencies=[Depends(require_node_token)])
# no token: the installer asks before it has one that main would trust and before the node's CA is pinned; the answer is the public
# name of the image (main's NODE_IMAGE_TAG), nothing a node could not read from the registry listing
public_router = APIRouter(prefix="/api")

_NAME = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"  # DNS-1123 label, as everywhere for nodes
SEEN_TTL_S = 7 * 86400
UPDATE_ERROR_KEPT = 500  # a longer message from the node is cut, not refused: a refused report is a failure nobody sees
KEY_SEEN = "node:upd-seen:"  # Redis: when the node last asked «should I update»; tells a node without a working updater


def _redis(request: Request):
    return request.app.state.redis


def _accel_of_tag(tag: str) -> str:
    return "nvidia" if tag.endswith("-gpu") else "cpu"


# ---------------------------------------------------------------- node side


class ImageReportIn(BaseModel):
    version: str = Field(default="", max_length=64)
    revision: str = Field(default="", max_length=64)
    digest: str = Field(default="", pattern=r"^(sha256:[0-9a-f]{64})?$")
    tag: str = Field(default="", max_length=128)  # nsu-llm-node:0.1.0-gpu
    update_error: str = Field(default="", max_length=20000)  # the update the node tried failed: why (the portal keeps the first 500 characters)


@node_router.post("/{name}/image")
async def report_image(
    request: Request, body: ImageReportIn, name: str = Path(pattern=_NAME), db: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """The node container tells what it runs: at its start (registrar.sh, both modes) and when the updater failed."""
    await identity.check(request, db, name)
    await db.rollback()
    redis = _redis(request)
    await versions.save_report(
        redis, name, version=body.version, revision=body.revision, digest=body.digest, tag=body.tag, update_error=body.update_error[:UPDATE_ERROR_KEPT]
    )
    # the request is fulfilled once the node runs what the registry offers
    settings: Settings = request.app.state.settings
    if body.digest:
        offered = await versions.REGISTRY.resolve(settings, versions.tag_for(settings, _accel_of_tag(body.tag)), redis)
        if offered is not None and offered.digest == body.digest and await versions.clear_request(redis, name):
            log.info("node %s: runs the offered image %s, the update request is done", name, body.digest[:19])
    return {"ok": True}


@public_router.get("/node-image")
async def node_image(request: Request) -> dict[str, Any]:
    """The one place that names the node image: the tag main runs (NODE_IMAGE_TAG) and the local image names of both flavours.
    install-node, run-node and the other scripts take the version from here, not from a constant of their own. `digest`:
    what main's registry holds for each flavour (sha256:…, empty while it has none), so the installer pulls exactly that
    image and refuses another (pull-image.sh --digest)."""
    settings: Settings = request.app.state.settings
    redis = _redis(request)
    infos = await asyncio.gather(
        *(versions.REGISTRY.resolve(settings, versions.tag_for(settings, a), redis) for a in ("cpu", "nvidia"))
    )
    return {
        "tag": settings.node_image_tag,
        "cpu": versions.local_name(settings, "cpu"),
        "nvidia": versions.local_name(settings, "nvidia"),
        "digest": {a: (i.digest if i else "") for a, i in zip(("cpu", "nvidia"), infos, strict=True)},
    }


async def rpc_peers(state: Any, db: AsyncSession, name: str) -> list[str] | None:
    """The addresses that may reach this node's rpc server (18050, run-node's firewall): the other nodes of its split
    deployment, those the portal labelled for it and the members of its current groups. A superset of the group, so the
    leader of a group that is forming is let in before its pod starts. Never the node itself, never main. [] for a node in
    no split deployment; None when the cluster does not answer (the node leaves its firewall as it is)."""
    try:
        nodes = await state.k8s.list_nodes()
        owned = await state.k8s.list_owned()
    except Exception as exc:  # noqa: BLE001 - K8sUnavailable, ApiException: an unknown answer, not an empty one
        log.info("node %s: rpc peers unknown, the cluster does not answer: %s", name, exc)
        return None
    split = {d.name for d in await service.list_all(db) if d.mode == "split"}
    me = next((n for n in nodes if n.name.lower() == name), None)
    peers: set[str] = set()
    mine = me.deployment if me is not None and me.deployment in split else None
    if mine is not None:
        peers |= {n.name.lower() for n in nodes if n.deployment == mine}
    for o in owned:
        members = [m.strip().lower() for m in (o.group_members or "").split(",") if m.strip()]
        if o.deployment in split and name in members:
            peers |= set(members)
    by_name = {n.name.lower(): n for n in nodes}
    out = set()
    for peer in peers - {name, MAIN_NODE}:
        n = by_name.get(peer)
        ip = (n.external_ip or n.internal_ip) if n is not None else None
        if ip and _ipv4(ip):
            out.add(ip)
    return sorted(out, key=lambda a: tuple(int(x) for x in a.split(".")))


def _ipv4(value: str) -> bool:
    import ipaddress

    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


@node_router.get("/{name}/desired")
async def desired(
    request: Request,
    name: str = Path(pattern=_NAME),
    accel: Literal["cpu", "nvidia"] = Query("cpu"),
    fmt: Literal["json", "lines"] = Query("json", alias="format"),
    db: AsyncSession = Depends(get_session),
):
    """What the node should run and whether to replace its container now (versions.decide). `format=lines`: plain
    `key=value` lines for run-node.sh, which has no JSON parser on a bare Linux host."""
    await identity.check(request, db, name)
    settings: Settings = request.app.state.settings
    redis = _redis(request)
    offered = await versions.REGISTRY.resolve(settings, versions.tag_for(settings, accel), redis)
    report = (await versions.load_reports(redis, [name])).get(name)
    update_request = (await versions.load_requests(redis, [name])).get(name)
    serving = name in await versions.serving_nodes(db)
    auto_update = bool(await settings_store.get(db, "node_auto_update", settings))
    await db.rollback()
    verdict = versions.decide(settings, report, update_request, offered, serving, auto_update)
    if update_request and versions.outdated(report, offered) is None:
        # the node cannot be compared with the registry (no digest, no real revision): the request gets one try, then it is done,
        # or the node would replace its container every minute until the request expires
        if verdict.go and not update_request.get("attempted"):
            await versions.mark_attempted(redis, name)
        elif update_request.get("attempted"):
            await versions.clear_request(redis, name)
    try:
        await redis.set(KEY_SEEN + name, datetime.now(UTC).isoformat(), ex=SEEN_TTL_S)
    except Exception:  # only a hint for the admin's page
        log.warning("node %s: could not note that it asked for updates", name)
    image = None if offered is None else {
        "name": versions.local_name(settings, accel),
        "digest": offered.digest,
        "version": offered.version,
        "revision": offered.revision,
    }
    update = {"go": verdict.go, "mode": verdict.mode, "when": verdict.when, "waiting": verdict.waiting, "reason": verdict.reason}
    # the local name of the image main wants on this node (NODE_IMAGE_TAG), also while its registry has no such tag or does not
    # answer: run-node takes the tag from here instead of a constant of its own (they used to say 0.1.0 whatever main ran)
    name_of_image = versions.local_name(settings, accel)
    peers = await rpc_peers(request.app.state, db, name)
    await db.rollback()
    if fmt == "json":
        reply: dict[str, Any] = {"name": name_of_image, "image": image, "update": update}
        if peers is not None:  # absent: the cluster did not answer, the node keeps its firewall
            reply["rpc_peers"] = peers
        return reply
    one_line = lambda v: " ".join(str(v or "").split())  # noqa: E731
    lines = [
        f"name={name_of_image}",
        f"go={1 if verdict.go and image else 0}",
        f"image={image['name'] if image else ''}",
        f"digest={image['digest'] if image else ''}",
        f"mode={one_line(verdict.mode)}",
        f"when={one_line(verdict.when)}",
        f"reason={one_line(verdict.reason)}",
    ]
    if peers is not None:
        lines.append(f"rpc_peers={','.join(peers)}")
    return PlainTextResponse("\n".join(lines) + "\n")


# ---------------------------------------------------------------- admin side


def _group_key(n: dict[str, Any]) -> tuple[str, str, str]:
    return (n["digest"] or "", n["revision"] or "", n["version"] or "")


async def _offered(settings: Settings, redis: Any) -> tuple[dict[str, versions.ImageInfo | None], list[dict[str, Any]], str | None]:
    """The two tags the nodes should run, and every tag of the repository, with the registry's error if it is down."""
    infos = await asyncio.gather(*(versions.REGISTRY.resolve(settings, versions.tag_for(settings, a), redis) for a in versions.SUFFIX))
    wanted = dict(zip(versions.SUFFIX, infos, strict=True))
    error: str | None = None
    tags: list[dict[str, Any]] = []
    try:
        names = sorted(await versions.REGISTRY.list_tags(settings), reverse=True)[: versions.TAGS_SHOWN]
    except versions.RegistryError as exc:
        error, names = str(exc), []
    current = {versions.tag_for(settings, accel): accel for accel in versions.SUFFIX}
    for tag, info in zip(names, await asyncio.gather(*(versions.REGISTRY.resolve(settings, t, redis) for t in names)), strict=True):
        tags.append(
            {
                "tag": tag,
                "current": tag in current,
                "accel": "nvidia" if tag.endswith("-gpu") else "cpu" if tag.endswith("-cpu") else None,
                "digest": info.digest if info else None,
                "version": (info.version or None) if info else None,
                "revision": (info.revision or None) if info else None,
                "created": (info.created or None) if info else None,
            }
        )
    return wanted, tags, error


@router.get("/versions")
async def get_versions(
    request: Request,
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    state = request.app.state
    redis = _redis(request)
    _, rows, _, _ = await collect_nodes(state, db, k8s, settings)
    serving = await versions.serving_nodes(db)
    auto_update = bool(await settings_store.get(db, "node_auto_update", settings))
    await db.rollback()
    names = [r["name"] for r in rows if not r["is_main"]]
    reports = await versions.load_reports(redis, names)
    requests = await versions.load_requests(redis, names)
    seen = await _seen(redis, names)
    wanted, tags, registry_error = await _offered(settings, redis)

    nodes = []
    for r in rows:
        if r["is_main"]:
            continue
        name = r["name"]
        report = reports.get(name)
        offered = wanted.get("nvidia" if r["accel"] == "nvidia" else "cpu")
        verdict = versions.decide(settings, report, requests.get(name), offered, name in serving, auto_update)
        request_row = requests.get(name)
        nodes.append(
            {
                "name": name,
                "source": r["source"],
                "accel": r["accel"],
                "ready": r["ready"],
                "version": (report or {}).get("version") or (r["version"] if r["source"] == "standalone" else None) or None,
                "revision": (report or {}).get("revision") or None,
                "digest": (report or {}).get("digest") or None,
                "reported_at": (report or {}).get("reported_at"),
                "outdated": versions.outdated(report, offered),
                "update": None
                if request_row is None
                else {"when": request_row.get("when"), "at": request_row.get("at"), "by": request_row.get("by"), "waiting": verdict.waiting},
                "update_error": (report or {}).get("update_error") or None,
                "update_failed_at": (report or {}).get("update_failed_at"),
                "updater_seen_at": seen.get(name),
            }
        )
    nodes.sort(key=lambda n: n["name"])

    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for n in nodes:
        g = groups.setdefault(
            _group_key(n),
            {"version": n["version"], "revision": n["revision"], "digest": n["digest"], "outdated": n["outdated"], "count": 0, "nodes": []},
        )
        g["count"] += 1
        g["nodes"].append(n["name"])
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["version"] or ""))

    return {
        "at": datetime.now(UTC).isoformat(),
        "main": {
            "version": settings.nsu_llm_version or None,
            "revision": settings.nsu_llm_revision or None,
            "digest": settings.main_image_digest or None,
        },
        "registry": {"repo": settings.registry_repo, "available": registry_error is None, "error": registry_error, "tags": tags},
        "offered": {
            accel: None
            if info is None
            else {"tag": info.tag, "digest": info.digest, "version": info.version or None, "revision": info.revision or None}
            for accel, info in wanted.items()
        },
        "auto_update": auto_update,
        "nodes": nodes,
        "groups": ordered,
    }


async def _seen(redis: Any, names: list[str]) -> dict[str, str]:
    if not names:
        return {}
    try:
        values = await redis.mget([KEY_SEEN + n for n in names])
    except Exception:
        return {}
    return {n: v for n, v in zip(names, values, strict=True) if v}


class UpdateIn(BaseModel):
    when: Literal["idle", "now"] = "idle"  # «после выключения развёртывания» / «сейчас»
    force: bool = False  # also a node that already runs the offered image


class BulkUpdateIn(UpdateIn):
    names: list[str] = Field(min_length=1, max_length=200)


async def _ask(
    db: AsyncSession,
    request: Request,
    settings: Settings,
    rows: dict[str, dict[str, Any]],
    name: str,
    body: UpdateIn,
    user: User,
    todo: list[tuple[str, str]],
) -> dict[str, Any]:
    """Audits the request for one node and queues it in `todo`; the caller sets the requests in Redis once the audit rows are
    committed (a request must not act without its audit row). The result row has `error` instead of raising (the bulk call goes on)."""
    redis = _redis(request)
    row = rows.get(name)
    if row is None:
        return {"name": name, "ok": False, "error": "Нода не найдена"}
    if row["is_main"]:
        return {"name": name, "ok": False, "error": "Главный сервер обновляется установщиком главного (docs/release.md), не из панели"}
    accel = "nvidia" if row["accel"] == "nvidia" else "cpu"
    offered = await versions.REGISTRY.resolve(settings, versions.tag_for(settings, accel), redis)
    if offered is None:
        return {"name": name, "ok": False, "error": "В реестре главного нет образа ноды: загрузите образы командой install-main.sh --update (docs/release.md)"}
    report = (await versions.load_reports(redis, [name])).get(name)
    if versions.outdated(report, offered) is False and not body.force:
        return {"name": name, "ok": False, "error": "Нода уже работает на актуальном образе"}
    await audit.record(
        db, user, "node.update", "node", name, {"when": body.when, "force": body.force, "accel": accel}, audit.client_ip(request)
    )
    todo.append((name, body.when))
    log.info("node update: %s asks %s to replace its container (%s)", user.email, name, "сейчас" if body.when == "now" else "после выключения развёртывания")
    serving = name in await versions.serving_nodes(db)
    return {"name": name, "ok": True, "when": body.when, "waiting": body.when == "idle" and serving}


async def _rows(request: Request, db: AsyncSession, k8s, settings: Settings) -> dict[str, dict[str, Any]]:
    _, rows, _, _ = await collect_nodes(request.app.state, db, k8s, settings)
    return {r["name"]: r for r in rows}


@router.post("/nodes/update", dependencies=[Depends(require_csrf)])
async def update_nodes(
    body: BulkUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """«Обновить» for the ticked nodes: one request each, a node that cannot be asked is reported, the others go on."""
    rows = await _rows(request, db, k8s, settings)
    todo: list[tuple[str, str]] = []
    results = [await _ask(db, request, settings, rows, n.strip().lower(), body, user, todo) for n in dict.fromkeys(body.names)]
    await db.commit()  # the audit rows of the nodes that were asked, then the requests themselves
    for node, when in todo:
        await versions.set_request(_redis(request), node, when, user.email)
    return {"results": results}


@router.post("/nodes/{name}/update", dependencies=[Depends(require_csrf)])
async def update_node(
    name: str,
    body: UpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    rows = await _rows(request, db, k8s, settings)
    todo: list[tuple[str, str]] = []
    result = await _ask(db, request, settings, rows, name.strip().lower(), body, user, todo)
    await db.commit()
    for node, when in todo:
        await versions.set_request(_redis(request), node, when, user.email)
    if not result["ok"]:
        raise HTTPException(404 if result["error"] == "Нода не найдена" else 409, result["error"])
    return result


@router.delete("/nodes/{name}/update", dependencies=[Depends(require_csrf)])
async def cancel_update(
    name: str, request: Request, db: AsyncSession = Depends(get_session), user: User = Depends(require_admin)
) -> dict[str, Any]:
    """Takes the request back (the node has not started yet; one that is replacing its container finishes)."""
    node = name.strip().lower()
    cleared = node in await versions.load_requests(_redis(request), [node])
    if cleared:
        await audit.record(db, user, "node.update", "node", node, {"cancelled": True}, audit.client_ip(request))
        await db.commit()
        await versions.clear_request(_redis(request), node)
    return {"name": name, "cancelled": cleared}
