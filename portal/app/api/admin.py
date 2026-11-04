"""Admin API: merged node list, join commands, users with keys and usage, logs of portal-owned pods."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from kubernetes_asyncio.client import ApiException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import require_admin, require_csrf
from app.catalog.estimate import node_capacity_bytes
from app.config import Settings
from app.controllers.backends import BACKEND_STATUS_KEY
from app.db import get_session
from app.deployments import service
from app.deps import REDIS_ERRORS, get_k8s, get_litellm, get_settings_dep
from app.k8s import K8sUnavailable
from app.litellm_client import LiteLLMError, LiteLLMUnavailable
from app.models import ApiKeyRef, ControllerTimer, NodeAssignment, NodeWake, StandaloneNode, User
from app.nodes import relay, wake

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

_K8S_ERRORS = (K8sUnavailable, ApiException)
USAGE_DAYS = 30
USAGE_CONCURRENCY = 8


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _host(url: str) -> str | None:
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


async def _backend_status(state) -> dict[str, Any]:
    """The backend controller's last round: in this process when it runs here, else from Redis."""
    status = getattr(state, "backend_status", None)
    if status is not None:
        return status
    try:
        raw = await state.redis.get(BACKEND_STATUS_KEY)
    except REDIS_ERRORS as exc:
        log.warning("admin nodes: backend status unavailable: %s", exc)
        return {}
    return json.loads(raw) if raw else {}


async def collect_nodes(
    state, db: AsyncSession, k8s, settings: Settings, with_heartbeats: bool = False
) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]], dict[str, datetime]]:
    """Cluster nodes and standalone nodes merged: (k8s reachable, rows, backends, last sign of life by node name)."""
    assignments = {a.node_name: a for a in (await db.scalars(select(NodeAssignment))).all()}
    names = {d.id: d.name for d in await service.list_all(db)}

    backends = list((await _backend_status(state)).get("backends") or [])
    per_node: dict[str, int] = {}
    for b in backends:
        per_node[str(b.get("node", "")).lower()] = per_node.get(str(b.get("node", "")).lower(), 0) + 1

    def deployment_of(name: str, label: str | None) -> str | None:
        row = assignments.get(name)
        if row is not None:
            return names.get(row.deployment_id) if row.deployment_id is not None else None
        return label

    def maintenance_of(name: str) -> bool:
        row = assignments.get(name)
        return row is not None and row.maintenance

    nics = set((await db.scalars(select(NodeWake.node_name))).all())
    timers = select(ControllerTimer).where(ControllerTimer.key.startswith(wake.TIMER_PREFIX))
    waking = {t.key.removeprefix(wake.TIMER_PREFIX): t for t in (await db.scalars(timers)).all()}

    def wake_of(name: str) -> dict[str, Any]:
        """The node reported its NIC (the «Разбудить» button works), and the controller's attempts while it is down."""
        t = waking.get(name)
        state = None
        if t is not None:
            tries = int(t.members) if t.members.isdigit() else 0
            since = datetime.fromtimestamp(t.since, UTC).isoformat()
            state = {"tries": tries, "max_tries": settings.wol_max_tries, "since": since}
        return {"wakeable": name in nics, "wake": state}

    result: list[dict[str, Any]] = []
    heartbeat_at: dict[str, datetime] = {}
    k8s_available = True
    heartbeats: dict[str, datetime] = {}
    try:
        k8s_nodes = await k8s.list_nodes()
    except _K8S_ERRORS as exc:
        log.warning("admin nodes: cluster unavailable: %s", exc)
        k8s_available, k8s_nodes = False, []
    if with_heartbeats and k8s_nodes:
        # its own try: a lease listing error only leaves the heartbeat ages empty, the nodes table stays
        try:
            heartbeats = {name.lower(): at for name, at in (await k8s.node_heartbeats()).items()}
        except _K8S_ERRORS as exc:
            log.warning("admin nodes: node leases unavailable: %s", exc)
    for n in k8s_nodes:
        name = n.name.lower()
        accel = n.accel or "cpu"
        if name in heartbeats:
            heartbeat_at[name] = heartbeats[name]
        result.append(
            {
                "name": name,
                "source": "k8s",
                "accel": accel,
                "ip": n.external_ip or n.internal_ip,
                "ready": n.ready,
                "vram_mib": n.vram_mib,
                "mem_mib": n.mem_mib,
                "capacity_bytes": node_capacity_bytes(accel, n.vram_mib, n.mem_mib),
                "deployment": deployment_of(name, n.deployment),
                "backends": per_node.get(name, 0),
                "version": n.kubelet_version,
                "is_main": n.is_main,
                "maintenance": maintenance_of(name),
                **wake_of(name),
            }
        )
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.standalone_ttl_s)
    in_cluster = {n.name.lower() for n in k8s_nodes}
    for s in (await db.scalars(select(StandaloneNode).order_by(StandaloneNode.name))).all():
        name = s.name.lower()
        if name in in_cluster:  # left behind by a PC that moved to k3s: the cluster node wins (the controllers purge it)
            continue
        heartbeat_at[name] = s.last_seen_at
        result.append(
            {
                "name": name,
                "source": "standalone",
                "accel": s.accel,
                "ip": _host(s.url),
                "ready": s.stopped_at is None and s.last_seen_at >= cutoff,
                "vram_mib": s.vram_mib,
                "mem_mib": s.ram_mib,
                "capacity_bytes": node_capacity_bytes(s.accel, s.vram_mib, s.ram_mib),
                "deployment": deployment_of(name, None),
                "backends": per_node.get(name, 0),
                "version": s.version or None,
                "is_main": False,
                "maintenance": maintenance_of(name),
                **wake_of(name),
            }
        )
    return k8s_available, result, backends, heartbeat_at


@router.get("/nodes")
async def nodes(
    request: Request,
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    k8s_available, result, backends, _ = await collect_nodes(request.app.state, db, k8s, settings)
    return {"k8s_available": k8s_available, "nodes": result, "backends": backends}


@router.post("/nodes/{name}/wake", dependencies=[Depends(require_csrf)])
async def wake_node(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """The «Разбудить» button: one magic packet now, to the same addresses the controller uses."""
    name = name.strip().lower()
    nic = await db.get(NodeWake, name)
    if nic is None:
        raise HTTPException(404, "Нода не сообщила адрес сетевой карты: переустановите её (install-node.ps1)")
    try:
        k8s_nodes = await k8s.list_nodes()
    except _K8S_ERRORS:
        k8s_nodes = []
    last_ip = None
    if settings.wol_unicast:
        sn = await db.get(StandaloneNode, name)
        last_ip = _host(sn.url) if sn is not None else None
        known = next((n for n in k8s_nodes if n.name.lower() == name), None)
        if known is not None:
            last_ip = known.external_ip or known.internal_ip
        last_ip = last_ip or nic.ip
    addrs = wake.targets(nic.ip, nic.prefix, settings.wol_overrides, last_ip)
    errors = wake.send(nic.mac, addrs)
    log.info("wake-on-lan: %s wakes node %s: magic packet for %s to %s", user.email, name, nic.mac, ", ".join(addrs))
    for e in errors:
        log.warning("wake-on-lan: node %s: send to %s failed", name, e)
    via = await _wake_via_node(request.app.state, db, settings, name, nic, k8s_nodes)
    if via is None and len(errors) >= len(addrs) * len(wake.PORTS):
        raise HTTPException(502, f"Пакет пробуждения не отправлен: {errors[0] if errors else 'нет адреса подсети'}")
    return {"node": name, "targets": addrs, "relay": via}


async def _wake_via_node(state, db: AsyncSession, settings: Settings, name: str, nic: NodeWake, k8s_nodes) -> str | None:
    """The button also goes through the first live node of the PC's subnet (unless WOL_VIA_NODE=off): it is pressed when
    the automatic packets did not help. The relay's name, or None."""
    broadcast = wake.directed_broadcast(nic.ip, nic.prefix)
    if settings.wol_via_node == "off" or broadcast is None:
        return None
    nics = {w.node_name: w for w in (await db.scalars(select(NodeWake))).all()}
    live = await service.live_nodes(db, settings.standalone_ttl_s)
    r = wake.pick_relay(name, nic.ip, nic.prefix, relay.candidates(k8s_nodes, live, nics))
    if r is None:
        return None
    error = await relay.send_via(state, r, name, nic.mac, broadcast)
    if error:
        log.warning("wake-on-lan: node %s: the packet via node %s failed: %s", name, r.name, error)
        return None
    log.info("wake-on-lan: node %s: magic packet via node %s to %s", name, r.name, broadcast)
    return r.name


@router.get("/join")
async def join(k8s=Depends(get_k8s), settings: Settings = Depends(get_settings_dep)) -> dict[str, str]:
    token = _agent_token(k8s, settings)
    host = settings.main_advertise_ip or settings.public_hostname
    linux = f"sudo ./run-node.sh --main {host} --token {token}"
    return {
        "agent_token": token,
        "main_host": host,
        "linux": linux,
        "linux_standalone": f"{linux} --mode standalone",
        "windows": f".\\run-node.ps1 -Main {host} -Token {token}",
    }


def _agent_token(k8s, settings: Settings) -> str:
    """The agent token in the secure form k3s writes (it pins the cluster CA), else the raw one; never the server
    token: until k3s restarts with --agent-token its agent-token file is a link to server/token."""
    raw = settings.agent_token.get_secret_value()
    if not raw:
        raise HTTPException(503, "Токен подключения нод не создан: перезапустите главный сервер")
    full = k8s.read_token_file(settings.agent_token_file)
    return full if full and full.endswith("::node:" + raw) else raw


async def _tokens_30d(litellm, user_ids: list[str]) -> dict[str, int | None]:
    """Prompt plus completion tokens of the last 30 days per user; None when LiteLLM cannot tell."""
    if not user_ids or not await litellm.health():
        return dict.fromkeys(user_ids)
    end = datetime.now(UTC).date()
    start = end - timedelta(days=USAGE_DAYS - 1)
    gate = asyncio.Semaphore(USAGE_CONCURRENCY)

    async def one(user_id: str) -> int | None:
        async with gate:
            try:
                activity = await litellm.user_activity(user_id, start, end)
            except (LiteLLMUnavailable, LiteLLMError) as exc:
                log.warning("admin users: no usage for %s: %s", user_id, exc)
                return None
            return activity.prompt_tokens + activity.completion_tokens

    return dict(zip(user_ids, await asyncio.gather(*(one(u) for u in user_ids)), strict=True))


@router.get("/users")
async def users(db: AsyncSession = Depends(get_session), litellm=Depends(get_litellm)) -> list[dict[str, Any]]:
    rows = (await db.scalars(select(User).order_by(User.created_at, User.email))).all()
    counts = dict(
        (
            await db.execute(
                select(ApiKeyRef.user_id, func.count())
                .where(ApiKeyRef.is_playground.is_(False))
                .group_by(ApiKeyRef.user_id)
            )
        ).all()
    )
    tokens = await _tokens_30d(litellm, [str(u.id) for u in rows])
    return [
        {
            "id": str(u.id),
            "email": u.email,
            "full_name": u.full_name,
            "role": u.role,
            "created_at": _iso(u.created_at),
            "last_login_at": _iso(u.last_login_at),
            "keys": counts.get(u.id, 0),
            "tokens_30d": tokens[str(u.id)],
        }
        for u in rows
    ]


@router.get("/pods/{name}/logs")
async def pod_logs(
    name: str,
    lines: int = Query(200, ge=1, le=2000),
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
) -> dict[str, str]:
    """Logs of a pod the portal owns, i.e. one that belongs to a known deployment; anything else is 404."""
    try:
        owned = {
            pod.name for dep in await service.list_all(db) for pod in await k8s.list_pods(dep.name)
        }
        if name not in owned:
            raise HTTPException(404, "Под не найден")
        text = await k8s.pod_log_tail(name, lines)
    except _K8S_ERRORS:
        raise HTTPException(503, "Кластер недоступен") from None
    return {"name": name, "lines": text}
