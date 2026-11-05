"""«Удалить из кластера…» of the «Ноды» page (task 2-31): DELETE /api/admin/nodes/{name}.

Own module, not api/admin.py: that file belongs to the other admin tasks and this one needs nothing from it but the
router prefix. The removal itself is `app.nodes.routes.remove_node`, the one code path that the node's own DELETE
(run-node -Uninstall) takes too.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from kubernetes_asyncio.client import ApiException
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.sessions import require_admin, require_csrf
from app.config import Settings
from app.db import get_session
from app.deployments import schedule, service
from app.deps import get_k8s, get_settings_dep
from app.k8s import MAIN_NODE, K8sUnavailable
from app.models import NodeAssignment, NodeIdentity, NodeModels, NodeWake, StandaloneNode, User
from app.nodes.routes import remove_node

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

_K8S_ERRORS = (K8sUnavailable, ApiException)


def uninstall_commands(settings: Settings, name: str) -> dict[str, str]:
    """What the admin runs on the PC itself. No token: the node is already gone from the portal, the script only has to
    remove what it installed (a DELETE it would send answers 404, which it takes for «already gone»)."""
    host = settings.main_advertise_ip or settings.public_hostname
    return {
        "windows": f".\\install-node.ps1 -Uninstall -Name {name}",
        "linux": f"sudo ./run-node.sh --uninstall --main {host} --name {name}",
    }


async def _held_by(db: AsyncSession, k8s, k8s_nodes, name: str, source: str, accel: str | None, settings: Settings):
    """The deployments the node works for: {name: mode}. Its assignment, the deployment it is lent to outside the window
    of its own, and any pod of a deployment that runs on it (a split group member holds the pods of its group)."""
    deployments = await service.list_all(db)
    held: dict[str, str] = {}
    row = await db.get(NodeAssignment, name)
    now = schedule.now_utc()
    for dep in deployments:
        if row is not None and row.deployment_id == dep.id:
            held[dep.name] = dep.mode
    lent = service.effective_deployment(row, deployments, accel, source, now, settings.schedule_drain_s)
    if lent is not None:
        held[lent.name] = lent.mode
    if k8s_nodes is not None:  # the cluster answered: its pods count
        for dep in deployments:
            try:
                pods = await k8s.list_pods(dep.name)
            except _K8S_ERRORS as exc:
                log.warning("node delete: pods of %s unavailable: %s", dep.name, exc)
                continue
            if any((p.node_name or "").lower() == name for p in pods):
                held[dep.name] = dep.mode
    return held


def _busy_text(held: dict[str, str]) -> str:
    names = ", ".join(f"{n} (split)" if mode == "split" else n for n, mode in sorted(held.items()))
    return (
        f"Нода работает на развёртываниях: {names}. Они потеряют её"
        + (", группа split остановится и соберётся заново" if "split" in held.values() else "")
        + ". Удалить всё равно можно с подтверждением"
    )


@router.delete("/nodes/{name}", dependencies=[Depends(require_csrf)])
async def delete_node(
    name: str,
    request: Request,
    force: bool = Query(False),
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
    user: User = Depends(require_admin),
) -> Any:
    """Removes the node from the cluster: its Kubernetes Node object and everything the portal keeps for it (registry
    record, Wake-on-LAN NIC, size of its models volume, assignment, reports of version and update, live metrics; the
    history of the metrics and the audit log stay). A node that works for a deployment (assigned, lent, or running its
    pods, a member of a split group included) is refused with 409 and the deployments' names unless `force=1`; with it the
    node is unassigned in the same transaction and the controllers re-plan. The answer says whether the PC is still online:
    a node agent that keeps running registers again with its next heartbeat, so the answer carries the commands that
    uninstall it on the PC (no token in them)."""
    name = name.strip().lower()
    if name == MAIN_NODE:
        raise HTTPException(409, "Главный сервер нельзя удалить")
    k8s_nodes: list | None
    try:
        k8s_nodes = await k8s.list_nodes()
    except _K8S_ERRORS as exc:
        log.warning("node delete: cluster unavailable: %s", exc)
        k8s_nodes = None
    in_cluster = next((n for n in k8s_nodes or [] if n.name.lower() == name), None)
    record = await db.get(StandaloneNode, name)
    leftovers = any([await db.get(m, name) for m in (NodeAssignment, NodeWake, NodeModels, NodeIdentity)])
    if in_cluster is None and record is None and not leftovers:
        raise HTTPException(
            503 if k8s_nodes is None else 404,
            "Кластер недоступен: нода не найдена среди зарегистрированных, объект Kubernetes проверить нельзя"
            if k8s_nodes is None
            else "Нода не найдена",
        )
    if in_cluster is not None:  # the cluster wins over a standalone record of the same name, as everywhere
        source, accel, online = "k8s", in_cluster.accel, bool(in_cluster.ready)
    elif record is not None:
        fresh = {sn.name.lower() for sn in await service.live_nodes(db, settings.standalone_ttl_s)}
        source, accel, online = "standalone", record.accel, name in fresh
    else:
        source, accel, online = "none", None, False
    held = await _held_by(db, k8s, k8s_nodes, name, source, accel, settings)
    if held and not force:
        return JSONResponse(
            {
                "detail": _busy_text(held),
                "deployments": [{"name": n, "mode": m} for n, m in sorted(held.items())],
            },
            status_code=409,
        )

    async def log_it(found: bool, k8s_state: str, password_gone: bool) -> None:
        details = {
            "source": source, "forced": force, "online": online, "deployments": sorted(held), "k8s": k8s_state,
        }
        if not password_gone:
            details["k3s_cleanup"] = "left"  # (a key with «password» in it the audit log drops)
        await audit.record(db, user, "node.delete", "node", name, details, audit.client_ip(request))

    _, k8s_state, password_gone = await remove_node(request.app.state, db, name, log_it)
    log.info("node %s removed from the cluster by %s (%s, online=%s, forced=%s)", name, user.email, source, online, force)
    return {
        "node": name,
        "online": online,
        "k8s": k8s_state,
        "k3s_password": "deleted" if password_gone else "left",
        "unassigned": sorted(held),
        "uninstall": uninstall_commands(settings, name),
    }
