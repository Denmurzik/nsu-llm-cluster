"""Admin API: merged node list, join commands, logs of portal-owned pods."""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from kubernetes_asyncio.client import ApiException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, settings_store
from app.auth.sessions import require_admin, require_csrf
from app.catalog.estimate import node_capacity_bytes
from app.config import Settings
from app.controllers.backends import BACKEND_STATUS_KEY
from app.db import get_session
from app.deployments import schedule, service
from app.deps import REDIS_ERRORS, get_k8s, get_settings_dep
from app.k8s import MAIN_NODE, K8sUnavailable
from app.models import AuditLog, ControllerTimer, NodeAssignment, NodeIdentity, NodeModels, NodeWake, StandaloneNode, User
from app.nodes import cleanup, identity, registry, relay, wake

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

_K8S_ERRORS = (K8sUnavailable, ApiException)


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
    state, db: AsyncSession, k8s, settings: Settings, with_heartbeats: bool = False, with_enrolled: bool = False
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

    identities = {r.name: r for r in (await db.scalars(select(NodeIdentity))).all()}
    approval_required = await identity.approval_required(state)
    claims = await identity.claims_by_name(db)
    # what the admin compares a claim with: every address the portal knows for the name (only names with claims: rare)
    known: dict[str, list[str]] = {}
    for name in claims:
        row = identities.get(name) or NodeIdentity(name=name)
        known[name] = sorted(await identity.known_addresses(db, row, k8s))
    since = datetime.now(UTC) - timedelta(seconds=identity.CLAIM_TTL_S)
    refused_rows = await db.execute(
        select(AuditLog.target, func.count())
        .where(AuditLog.action == "node.claim_refused", AuditLog.at >= since)
        .group_by(AuditLog.target)
    )
    refused = {target: count for target, count in refused_rows.all()}

    def identity_of(name: str, is_main: bool = False) -> dict[str, Any]:
        """Approved or waiting for the admin (app.nodes.identity), whether the node has its own key, the address it last
        called from, and the claims on its name with the addresses the portal knows for it (what the admin compares)."""
        if is_main:
            return {
                "approved": True, "has_key": False, "claim_ip": None, "legacy": False, "claims": [],
                "known_addresses": [], "claims_refused": 0,
            }
        row = identities.get(name)
        mine = known.get(name, [])
        return {
            "approved": identity.is_approved(row, approval_required),
            "has_key": row is not None and row.key_hash is not None,
            "claim_ip": row.last_ip if row is not None else None,
            # a node of before without its own key (an old image): it works from its old address until the window ends
            "legacy": row is not None and row.legacy and row.key_hash is None,
            # other registrations of this name, for the admin to pick from or reject (never approved in bulk)
            "claims": [
                {"id": c.id, "ip": c.ip, "at": c.at.isoformat(), "known": bool(c.ip) and identity._norm(c.ip) in mine}
                for c in claims.get(name, [])
            ],
            "known_addresses": mine,
            "claims_refused": refused.get(name, 0),
        }

    nics = set((await db.scalars(select(NodeWake.node_name))).all())
    models = {m.node_name: m.models_bytes for m in (await db.scalars(select(NodeModels))).all()}
    timers = select(ControllerTimer).where(ControllerTimer.key.startswith(wake.TIMER_PREFIX))
    waking = {t.key.removeprefix(wake.TIMER_PREFIX): t for t in (await db.scalars(timers)).all()}
    wol_max_tries = await settings_store.get(db, "wol_max_tries", settings)

    def wake_of(name: str) -> dict[str, Any]:
        """The node reported its NIC (the «Разбудить» button works), and the controller's attempts while it is down."""
        t = waking.get(name)
        state = None
        if t is not None:
            tries = int(t.members) if t.members.isdigit() else 0
            since = datetime.fromtimestamp(t.since, UTC).isoformat()
            state = {"tries": tries, "max_tries": wol_max_tries, "since": since}
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
                "models_bytes": models.get(name),
                **wake_of(name),
                **identity_of(name, n.is_main),
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
                "models_bytes": models.get(name),
                **wake_of(name),
                **identity_of(name),
            }
        )
    if with_enrolled and k8s_available:
        # registered with the portal but neither in the cluster nor a standalone record (its agent has not joined yet, or
        # k3s refuses it): without a row of its own the admin would see no «Подтвердить», no claim and nothing to delete
        listed = {row["name"] for row in result}
        for name in sorted((set(identities) | set(claims)) - listed - {MAIN_NODE}):
            row = identities.get(name)
            first_claim = next(iter(claims.get(name, [])), None)
            result.append(
                {
                    "name": name,
                    "source": "enrolled",
                    "accel": "cpu",
                    "ip": (row.last_ip if row is not None else None) or (first_claim.ip if first_claim else None),
                    "ready": False,
                    "vram_mib": None,
                    "mem_mib": None,
                    "capacity_bytes": None,
                    "deployment": deployment_of(name, None),
                    "backends": per_node.get(name, 0),
                    "version": None,
                    "is_main": False,
                    "maintenance": maintenance_of(name),
                    "models_bytes": models.get(name),
                    **wake_of(name),
                    **identity_of(name),
                }
            )
    return k8s_available, result, backends, heartbeat_at


@router.get("/nodes")
async def nodes(
    request: Request,
    enrolled: bool = Query(False),
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    """`enrolled=1` (the «Ноды» page): also the names that registered but are neither in the cluster nor standalone
    (source `enrolled`). The pickers of deployments leave it out: nothing can run there yet."""
    k8s_available, result, backends, _ = await collect_nodes(request.app.state, db, k8s, settings, with_enrolled=enrolled)
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
    await audit.record(db, user, "node.wake", "node", name, {"targets": addrs, "relay": via}, audit.client_ip(request))
    await db.commit()
    return {"node": name, "targets": addrs, "relay": via}


async def _wake_via_node(state, db: AsyncSession, settings: Settings, name: str, nic: NodeWake, k8s_nodes) -> str | None:
    """The button also goes through the first live node of the PC's subnet (unless WOL_VIA_NODE=off): it is pressed when
    the automatic packets did not help. The relay's name, or None."""
    broadcast = wake.directed_broadcast(nic.ip, nic.prefix)
    if settings.wol_via_node == "off" or broadcast is None:
        return None
    nics = {w.node_name: w for w in (await db.scalars(select(NodeWake))).all()}
    live = await service.live_nodes(db, settings.standalone_ttl_s)
    candidates = relay.candidates(k8s_nodes, live, nics)
    approved = await identity.approved_names(db, [c.name for c in candidates], await identity.approval_required(state))
    r = wake.pick_relay(name, nic.ip, nic.prefix, [c for c in candidates if c.name in approved])
    if r is None:
        return None
    error = await relay.send_via(state, r, name, nic.mac, broadcast)
    if error:
        log.warning("wake-on-lan: node %s: the packet via node %s failed: %s", name, r.name, error)
        return None
    log.info("wake-on-lan: node %s: magic packet via node %s to %s", name, r.name, broadcast)
    return r.name


@router.post("/nodes/{name}/purge-models", dependencies=[Depends(require_csrf)])
async def purge_models(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """The «Очистить модели» button: removes from the node's models volume (the model files and the rpc tensor cache)
    everything its deployment does not need. A node without a deployment loses all of it. What the node serves
    now, with its draft model and projector, stays, and so does the tensor cache of its split group. The answer comes
    when the node has done it (a Job on a k3s node, the next heartbeats of a standalone one), or says `pending` when it has not
    within the wait (app.nodes.cleanup)."""
    name = name.strip().lower()
    if name == MAIN_NODE:
        raise HTTPException(409, "На главном сервере нет весов моделей класса")
    k8s_error = False
    try:
        k8s_nodes = await k8s.list_nodes()
    except _K8S_ERRORS as exc:
        log.warning("purge models: cluster unavailable: %s", exc)
        k8s_error, k8s_nodes = True, []
    known = next((n for n in k8s_nodes if n.name.lower() == name), None)
    if known is not None:
        source, accel, live = "k8s", known.accel, known.ready
    else:
        record = await db.get(StandaloneNode, name)
        if record is None:
            raise HTTPException(503 if k8s_error else 404, "Кластер недоступен" if k8s_error else "Нода не найдена")
        fresh = {sn.name.lower() for sn in await service.live_nodes(db, settings.standalone_ttl_s)}
        source, accel, live = "standalone", record.accel, name in fresh
    if not live:
        raise HTTPException(409, "Нода не в сети: очистку выполняет сама нода, включите её и повторите")
    deployments = await service.list_all(db)
    _, needed = cleanup.node_deployments(
        await db.get(NodeAssignment, name), deployments, accel, source, schedule.now_utc(), settings.schedule_drain_s
    )
    keep = cleanup.keep_for(needed)
    state = request.app.state
    ref, error = await cleanup.send(state, name, source, accel, keep)
    if ref is None:
        raise HTTPException(502, f"Не удалось передать команду ноде: {error}")
    log.info(
        "models cleanup: %s cleans node %s (%s), keeping %s%s", user.email, name, ref,
        ", ".join(keep.files) or "no model files", ", the tensor cache" if keep.rpc else "",
    )
    outcome = await cleanup.wait(state, name, source, ref)
    # one commit: the new size of the volume and the audit row; a failed attempt is logged too
    if outcome.state == "done" and outcome.models_bytes is not None:
        await registry.set_models(db, name, outcome.models_bytes)
    details = {
        "status": outcome.state, "freed_bytes": outcome.freed_bytes if outcome.state == "done" else None,
        "kept": list(keep.files), "kept_rpc": keep.rpc, "skipped": outcome.skipped,
    }
    if outcome.state == "failed":
        details["error"] = outcome.error
    await audit.record(db, user, "node.purge_models", "node", name, details, audit.client_ip(request))
    await db.commit()
    if outcome.state == "failed":
        raise HTTPException(502, f"Очистка не выполнена: {outcome.error}")
    return {
        "node": name,
        "status": "done" if outcome.state == "done" else "pending",
        "freed_bytes": outcome.freed_bytes if outcome.state == "done" else None,
        "models_bytes": outcome.models_bytes,
        "removed": outcome.removed,
        "skipped": outcome.skipped,
    }


@router.get("/join")
async def join(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, str]:
    token = _agent_token(k8s, settings)
    # the token joins nodes to the cluster: who looked at it and from where stays in the journal (finding L4)
    await audit.record(db, admin, "node.join_token", "cluster", "agent_token", None, audit.client_ip(request))
    await db.commit()
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
