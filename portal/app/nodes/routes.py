"""Standalone node API used by the node image's registrar (bearer agent token, no browser session)."""

import ipaddress
import logging
from dataclasses import asdict, replace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from kubernetes_asyncio.client import ApiException
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deployments.service import desired_for_node
from app.k8s import K8sUnavailable
from app.workloads.builders import has_local_source
from app.nodes import registry, relay, wake

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


class RegisterIn(BaseModel):
    name: str = Field(pattern=_NAME)
    url: str = Field(pattern=_URL, max_length=255)
    accel: Literal["cpu", "nvidia"]
    vram_mib: int | None = Field(default=None, ge=0)
    ram_mib: int | None = Field(default=None, ge=0)
    version: str = Field(default="", max_length=64)
    wake: dict[str, Any] | None = None  # WakeInfo; a malformed one is ignored, never costs the node its heartbeat

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

    Only literal addresses and `localhost` names are recognised. A DNS name that resolves to a loopback or
    link-local address is not caught here (the portal does not resolve names at registration).
    """
    host = host.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if not any(c.isalpha() for c in host):  # digits and dots only: must be a full dotted IPv4 address
        try:
            ip = ipaddress.IPv4Address(host)
        except ValueError:
            return True  # shorthand forms such as 127.1 or 2130706433 reach loopback in the resolver
        return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast
    return False


class HeartbeatIn(BaseModel):
    name: str = Field(pattern=_NAME)
    applied_spec_hash: str | None = Field(default=None, max_length=64)
    wake: dict[str, Any] | None = None  # WakeInfo; a malformed one is ignored, never costs the node its heartbeat


@router.post("/register")
async def register(body: RegisterIn, db: AsyncSession = Depends(get_session)):
    await registry.upsert(db, body.name, body.url, body.accel, body.vram_mib, body.ram_mib, body.version)
    await _store_wake(db, body.name, body.wake)
    return {"ok": True, "heartbeat_interval": HEARTBEAT_INTERVAL_S}


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


@router.post("/wake")
async def report_wake(body: WakeIn, db: AsyncSession = Depends(get_session)):
    """k3s nodes report their NIC here once per container start (the registrar's --wake-report); standalone nodes send
    it with every heartbeat."""
    await registry.set_wake(db, body.name, body.mac, body.ip, body.prefix)
    await db.commit()
    return {"ok": True}


@router.post("/heartbeat")
async def heartbeat(body: HeartbeatIn, request: Request, db: AsyncSession = Depends(get_session)):
    if not await registry.heartbeat(db, body.name, body.applied_spec_hash):
        raise HTTPException(404, "unknown node")
    await _store_wake(db, body.name, body.wake)
    settings = request.app.state.settings
    desired = await desired_for_node(db, body.name, settings.schedule_drain_s)
    if desired is not None:
        desired = replace(desired, backend_api_key=settings.backend_api_key.get_secret_value())
        # own files (the model, its draft model or projector) exist on the mirror only
        if settings.hf_mirror_enabled or has_local_source(
            desired.model_repo, desired.llm_env.get("DRAFT_REPO"), desired.llm_env.get("MMPROJ_FILE")
        ):
            desired = replace(
                desired,
                hf_endpoint=f"{settings.node_hf_endpoint}/c/{body.name.lower()}",
                hf_token=settings.hf_mirror_token.get_secret_value(),
            )
    reply = {
        "ok": True,
        "heartbeat_interval": HEARTBEAT_INTERVAL_S,
        "desired": asdict(desired) if desired is not None else None,
    }
    # Wake-on-LAN relay: magic packets this node sends for down PCs of its subnet (registrar.sh runs wol-send.sh)
    commands = await relay.pop_commands(request.app.state.redis, body.name)
    if commands:
        reply["wake"] = commands
    return reply


@router.post("/{name}/stop", status_code=204)
async def stopped(name: str, db: AsyncSession = Depends(get_session)):
    """A clean stop of a standalone node (registrar.sh on TERM). Not an uninstall: the record and the NIC for
    Wake-on-LAN stay; DELETE below (run-node -Uninstall) forgets the PC. An unknown node is fine: nothing to stop."""
    if not await registry.stop(db, name.lower()):
        log.info("node %s: stop of an unknown node ignored", name)
    return Response(status_code=204)


@router.delete("/{name}", status_code=204)
async def deregister(name: str, request: Request, db: AsyncSession = Depends(get_session)):
    if name.lower() == MAIN_NODE:
        raise HTTPException(409, "Главный сервер нельзя удалить")
    await registry.remove(db, name)
    k8s = request.app.state.k8s
    try:
        if await k8s.available():
            await k8s.delete_node(name)
    except (K8sUnavailable, ApiException) as exc:
        log.warning("node %s: could not delete the Kubernetes node: %s", name, exc)
    return Response(status_code=204)
