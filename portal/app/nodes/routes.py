"""Node API used by the node image's registrar (bearer agent token plus the node's own key, no browser session).

The agent token only joins: every call for a name that has a key must carry it as X-Node-Key (app.nodes.identity).
"""

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, mirror_tokens
from app.db import get_session
from app.deployments.service import desired_for_node
from app.workloads.builders import has_local_source
from app.nodes import cleanup, identity, registry, relay, wake

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 15
MAIN_NODE = "llm-main"

_NAME = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"  # DNS-1123 label
_URL = r"^http://[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?:[0-9]{1,5}$"


# client addresses this worker process already warned about (at most this many, then the set is cleared and the warning repeats)
LEGACY_WARNED_MAX = 1024


async def require_node_token(request: Request) -> None:
    """The agent token (AGENT_TOKEN, k3s --agent-token). For one release the k3s server token (CLUSTER_TOKEN) of
    nodes installed before the agent token still works, unless NODE_LEGACY_TOKEN=false."""
    scheme, _, presented = request.headers.get("Authorization", "").partition(" ")
    presented = presented.strip()
    settings = request.app.state.settings
    if scheme.lower() == "bearer":
        if registry.token_matches(presented, settings.agent_token.get_secret_value(), "node"):
            return
        if settings.node_legacy_token and registry.token_matches(
            presented, settings.cluster_token.get_secret_value(), "server"
        ):
            _warn_legacy(request)
            return
    raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})


def _warn_legacy(request: Request) -> None:
    """Log the legacy-token warning once per client address per worker process. The state lives in app.state, so with
    several workers (PORTAL_WORKERS) and after every restart the same node is warned about again, once per worker."""
    host = request.client.host if request.client else "?"
    warned = getattr(request.app.state, "legacy_token_warned", None)
    if warned is None:
        warned = request.app.state.legacy_token_warned = set()
    if host in warned:
        return
    if len(warned) >= LEGACY_WARNED_MAX:
        warned.clear()
    warned.add(host)
    log.warning(
        "node at %s uses the k3s server token: re-run run-node with the agent token from the Nodes page", host
    )


# Bearer-token API: no cookies are involved, so there is no CSRF dependency here.
router = APIRouter(prefix="/api/nodes", dependencies=[Depends(require_node_token)])


class WakeInfo(BaseModel):
    """The physical NIC that carries the node's address (install-node.ps1 finds it), for Wake-on-LAN."""

    mac: str = Field(max_length=17)
    ip: str = Field(max_length=15)
    prefix: int = Field(ge=1, le=32)

    @field_validator("mac")
    @classmethod
    def _valid_mac(cls, value: str) -> str:
        mac = wake.normalize_mac(value)
        if mac is None:
            raise ValueError("Не MAC-адрес сетевой карты (например, 00-1A-2B-3C-4D-5E)")
        return mac

    @field_validator("ip")
    @classmethod
    def _valid_ip(cls, value: str) -> str:
        try:
            ip = ipaddress.IPv4Address(value)
        except ValueError:
            raise ValueError("Нужен адрес IPv4 сетевой карты") from None
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise ValueError("Адреса loopback, link-local, multicast и неопределённые не допускаются")
        return ip.compressed


class WakeIn(WakeInfo):
    name: str = Field(pattern=_NAME)


class ModelsIn(BaseModel):
    """k3s mode: how many bytes the node's models volume holds (registrar.sh --models-report)."""

    name: str = Field(pattern=_NAME)
    models_bytes: int = Field(ge=0, le=2**62)


class RegisterIn(BaseModel):
    name: str = Field(pattern=_NAME)
    url: str = Field(pattern=_URL, max_length=255)
    accel: Literal["cpu", "nvidia"]
    vram_mib: int | None = Field(default=None, ge=0)
    ram_mib: int | None = Field(default=None, ge=0)
    version: str = Field(default="", max_length=64)
    wake: dict[str, Any] | None = None  # WakeInfo; a malformed one is ignored, never costs the node its heartbeat
    models_bytes: int | None = Field(default=None, ge=0, le=2**62)

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        host, _, port = value.removeprefix("http://").rpartition(":")
        if not 0 < int(port) < 65536:
            raise ValueError("Порт вне диапазона 1-65535")
        if _local_host(host):
            raise ValueError("Адреса loopback, link-local и неопределённые не допускаются")
        return value


def _local_host(host: str) -> bool:
    """The portal itself or this machine's neighbourhood: the portal would probe and proxy to it (SSRF).

    Literal addresses and `localhost` names here; a DNS name is resolved at registration (`_resolves_local`).
    """
    host = host.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if not any(c.isalpha() for c in host):  # digits and dots only: must be a full dotted IPv4 address
        try:
            ip = ipaddress.IPv4Address(host)
        except ValueError:
            return True  # shorthand forms such as 127.1 or 2130706433 reach loopback in the resolver
        return _local_ip(ip)
    return False


def _local_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:127.0.0.1 from an AAAA record is loopback too
    if mapped is not None:
        ip = mapped
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast


RESOLVE_TIMEOUT_S = 3.0


async def _resolves_local(host: str) -> bool:
    """A DNS name of a node url that resolves to a loopback, link-local, unspecified or multicast address (finding L5:
    `localtest.me` would have the portal probe and proxy to itself). A name that does not resolve is let through: the
    backend controller's probe fails for it like for any node that does not answer."""
    if not any(c.isalpha() for c in host):
        return False  # a literal address, the model checked it already
    for address in await _lookup(host):
        try:
            if _local_ip(ipaddress.ip_address(address.split("%")[0])):
                return True
        except ValueError:
            continue
    return False


async def _lookup(host: str) -> list[str]:
    """The addresses a name resolves to on main (none when it does not resolve in RESOLVE_TIMEOUT_S)."""
    try:
        async with asyncio.timeout(RESOLVE_TIMEOUT_S):
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, TimeoutError):
        return []
    return [str(info[4][0]) for info in infos]


class HeartbeatIn(BaseModel):
    name: str = Field(pattern=_NAME)
    applied_spec_hash: str | None = Field(default=None, max_length=64)
    wake: dict[str, Any] | None = None  # WakeInfo; a malformed one is ignored, never costs the node its heartbeat
    models_bytes: int | None = Field(default=None, ge=0, le=2**62)
    # what models-cleanup.sh did for an order of an earlier reply (app.nodes.cleanup); junk is ignored
    purge_result: dict[str, Any] | None = None


def _identity_reply(checked: identity.Checked) -> dict[str, Any]:
    """What a registration tells the node about itself: approved or waiting, and its key when this call issued it
    (once: the portal keeps only its hash)."""
    reply: dict[str, Any] = {"approved": checked.approved}
    if checked.issued is not None:
        reply["node_key"] = checked.issued
    return reply


@router.post("/register")
async def register(body: RegisterIn, request: Request, db: AsyncSession = Depends(get_session)):
    """Standalone mode: registers the node (its url, accelerator, memory). A new name gets its own key here, with the
    agent token alone; a name that has a key re-registers only with it (app.nodes.identity)."""
    host = body.url.removeprefix("http://").rpartition(":")[0]
    if await _resolves_local(host):
        raise HTTPException(422, f"Имя {host} указывает на loopback или link-local адрес: так нода смотрела бы в сам портал")
    checked = await identity.enroll(request, db, body.name)
    if checked.claim:  # a claim waits for the admin: the name's record (its url) stays as it is
        return {"ok": True, "heartbeat_interval": HEARTBEAT_INTERVAL_S, **_identity_reply(checked)}
    await registry.upsert(db, body.name, body.url, body.accel, body.vram_mib, body.ram_mib, body.version)
    await _store_wake(db, body.name, body.wake)
    if body.models_bytes is not None:
        await registry.set_models(db, body.name, body.models_bytes)
        await db.commit()
    return {"ok": True, "heartbeat_interval": HEARTBEAT_INTERVAL_S, **_identity_reply(checked)}


class EnrollIn(BaseModel):
    name: str = Field(pattern=_NAME)


@router.post("/enroll")
async def enroll(body: EnrollIn, request: Request, db: AsyncSession = Depends(get_session)):
    """k3s mode (registrar.sh at the start of the node container): the node's identity, without a standalone record.
    The k3s token makes it a node of the cluster; the portal labels it for a deployment only once it is approved."""
    checked = await identity.enroll(request, db, body.name)
    return {"ok": True, **_identity_reply(checked)}


@router.get("/{name}/known")
async def known(name: str, request: Request, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """run-node asks this before it starts the container (task B2): does main know the name, does this PC hold its key, and
    in Russian what will happen and what the admin clicks. The agent token is enough: it says nothing about the name's key."""
    if not re.fullmatch(_NAME, name.lower()):
        raise HTTPException(422, "Не имя ноды")
    return await identity.name_state(request, db, name)


_WAKE_WARNED: set[str] = set()


async def _store_wake(db: AsyncSession, name: str, raw: dict[str, Any] | None) -> None:
    if raw is None:
        return
    try:
        info = WakeInfo.model_validate(raw)
    except ValidationError as exc:
        if name not in _WAKE_WARNED:  # once per node and worker process: the heartbeat repeats it every 15 s
            if len(_WAKE_WARNED) >= LEGACY_WARNED_MAX:
                _WAKE_WARNED.clear()
            _WAKE_WARNED.add(name)
            log.warning("node %s: its NIC for Wake-on-LAN is ignored: %s", name, exc.errors(include_url=False, include_input=False))
        return
    await registry.set_wake(db, name, info.mac, info.ip, info.prefix)
    await db.commit()


async def _store_cleanup(redis, db: AsyncSession, body: HeartbeatIn) -> None:
    """The size of the node's models volume and the answer to a cleanup order, when the heartbeat carries them. Junk is
    ignored: it never costs the node its heartbeat."""
    if body.purge_result is not None:
        outcome = await cleanup.store_result(redis, body.name, body.purge_result)
        if outcome is None:
            log.warning("node %s: a cleanup result without a valid id is ignored", body.name)
        elif outcome.models_bytes is not None:  # what the cleanup left is newer than the size measured before it
            await registry.set_models(db, body.name, outcome.models_bytes)
            await db.commit()
            return
    if body.models_bytes is not None:
        await registry.set_models(db, body.name, body.models_bytes)
        await db.commit()


@router.post("/wake")
async def report_wake(body: WakeIn, request: Request, db: AsyncSession = Depends(get_session)):
    """k3s nodes report their NIC here once per container start (the registrar's --wake-report); standalone nodes send
    it with every heartbeat."""
    await identity.check(request, db, body.name)
    await registry.set_wake(db, body.name, body.mac, body.ip, body.prefix)
    await db.commit()
    return {"ok": True}


@router.post("/models")
async def report_models(body: ModelsIn, request: Request, db: AsyncSession = Depends(get_session)):
    """k3s nodes report the size of their models volume here every few minutes (the registrar's --models-report);
    standalone nodes send it with every heartbeat."""
    await identity.check(request, db, body.name)
    await registry.set_models(db, body.name, body.models_bytes)
    await db.commit()
    return {"ok": True}


@router.post("/heartbeat")
async def heartbeat(body: HeartbeatIn, request: Request, db: AsyncSession = Depends(get_session)):
    """A standalone node is alive and runs `applied_spec_hash`. The reply: what it should run (only an approved node gets a
    deployment, its own backend key and a mirror token for its deployment's repositories), Wake-on-LAN relay commands and
    cleanup orders. A name the portal does not know (no identity, or its record was purged) is 404: the node registers
    again."""
    checked = await identity.check(request, db, body.name, allow_unknown=True, claim_ok=True)
    if checked.claim:  # a claim waits for the admin: nothing to run, the name's record is not touched
        return {"ok": True, "heartbeat_interval": HEARTBEAT_INTERVAL_S, "approved": False, "desired": None}
    if checked.row is None or not await registry.heartbeat(db, body.name, body.applied_spec_hash):
        raise HTTPException(404, "unknown node")
    await _store_wake(db, body.name, body.wake)
    await _store_cleanup(request.app.state.redis, db, body)
    settings = request.app.state.settings
    reply: dict[str, Any] = {"ok": True, "heartbeat_interval": HEARTBEAT_INTERVAL_S, "approved": checked.approved}
    if not checked.approved:  # waiting for the admin: nothing to run, no keys, no relay or cleanup work
        reply["desired"] = None
        return reply
    desired = await desired_for_node(db, body.name, settings.schedule_drain_s)
    if desired is not None:
        # the node's own key; a legacy node without one (an old image) keeps the shared key of before until it has its own
        own = checked.row.backend_key if checked.row.key_hash else None
        desired = replace(desired, backend_api_key=own or settings.backend_api_key.get_secret_value())
        # own files (the model, its draft model or projector) exist on the mirror only
        draft, mmproj = desired.llm_env.get("DRAFT_REPO"), desired.llm_env.get("MMPROJ_FILE")
        if settings.hf_mirror_enabled or has_local_source(desired.model_repo, draft, mmproj):
            name = body.name.lower()
            repos = mirror_tokens.repos_of(desired.model_repo, draft, mmproj)
            desired = replace(
                desired,
                hf_endpoint=f"{settings.node_hf_endpoint}/c/{name}",
                # the repositories of its deployment only, never the mirror's own key
                hf_token=mirror_tokens.mint(settings.hf_mirror_token.get_secret_value(), name, repos),
            )
    reply["desired"] = asdict(desired) if desired is not None else None
    # Wake-on-LAN relay: magic packets this node sends for down PCs of its subnet (registrar.sh runs wol-send.sh)
    commands = await relay.pop_commands(request.app.state.redis, body.name)
    if commands:
        reply["wake"] = commands
    # cleanup of the models volume: orders for this node (models-cleanup.sh runs them in the background)
    orders = await cleanup.pop_orders(request.app.state.redis, body.name)
    if orders:
        reply["purge"] = orders
    return reply


@router.post("/{name}/stop", status_code=204)
async def stopped(name: str, request: Request, db: AsyncSession = Depends(get_session)):
    """A clean stop of a standalone node (registrar.sh on TERM). Not an uninstall: the record and the NIC for
    Wake-on-LAN stay; DELETE below (run-node -Uninstall) forgets the PC. An unknown node is fine: nothing to stop."""
    # only the approved node itself, with its key: the agent token or a claim never takes a node out of routing
    await identity.check(request, db, name, strict=True)
    if not await registry.stop(db, name.lower()):
        log.info("node %s: stop of an unknown node ignored", name)
    return Response(status_code=204)


@router.delete("/{name}", status_code=204)
async def deregister(name: str, request: Request, db: AsyncSession = Depends(get_session)):
    """The node forgets itself (run-node -Uninstall): its own removal and the admin's «Удалить из кластера» share
    `remove_node`."""
    name = name.lower()
    if name == MAIN_NODE:
        raise HTTPException(409, "Главный сервер нельзя удалить")
    # only the approved node itself, with its own key: the agent token alone, a legacy call without a key, a claim or a
    # waiting node delete nothing (the admin can, «Удалить из кластера…»)
    await identity.check(request, db, name, strict=True)

    async def log_it(found: bool, k8s_state: str, password_gone: bool) -> None:
        # no admin acts here (the actor is empty): the row says it was the node's own request and from where; a name nobody
        # knew leaves no row.
        if found or k8s_state == "deleted":
            details = {"by": "node", "k8s": k8s_state} | ({} if password_gone else {"k3s_cleanup": "left"})
            await audit.record(db, None, "node.delete", "node", name, details, audit.client_ip(request))

    await remove_node(request.app.state, db, name, log_it)
    return Response(status_code=204)


async def remove_node(
    state, db: AsyncSession, name: str, audit: Callable[[bool, str, bool], Awaitable[None]] | None = None
) -> tuple[bool, str, bool]:
    """Removes everything the portal keeps for the node `name`: the Kubernetes Node object, the registry row, its identity
    (key and approval: a PC that comes back registers as a new node), the NIC for Wake-on-LAN, the size of the models volume, the assignment and the controllers' pauses, the node's keys in Redis
    (versions, live metrics, cleanup and relay orders). Returns (a registry row existed, state of the Kubernetes node:
    deleted | absent | unavailable, the k3s node password is gone: while it stays k3s refuses a PC reinstalled under this
    name until «Подтвердить» clears it). The Kubernetes node goes first: a node the cluster still lists would be assigned
    again by the controllers between the two steps. `audit` is awaited with that result just before the commit, so
    the audit row and the database part of the removal are one transaction."""
    k8s_state, password_gone = await registry.delete_k8s_node(state.k8s, name)
    try:
        found = await registry.remove(db, name, commit=False)
        await identity.forget(db, name)
        if audit is not None:
            await audit(found, k8s_state, password_gone)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    await registry.forget_live(state.redis, name)
    return found, k8s_state, password_gone
