"""Wake-on-LAN through a live node of the PC's subnet (the fallback of WOL_VIA_NODE, app.nodes.wake.via_node).

A k8s relay runs a one-shot Job pinned to it (builders.wake_job: hostNetwork, the node image's wol-send.sh, no
privilege, no token, no new port on the node). Chosen over an endpoint of the node agent: k3s nodes run no agent of
ours that listens, and a new listener would need its own authentication and a firewall opening on every PC; the Job
reuses the Kubernetes API the portal already drives. A standalone relay has no Kubernetes: the command waits in Redis
(shared by the controllers and every API worker) and goes out with the node's next heartbeat (registrar.sh runs
wol-send.sh).
"""

import json
import logging

from kubernetes_asyncio.client.exceptions import ApiException

from app.deps import REDIS_ERRORS
from app.k8s import K8sUnavailable
from app.nodes import wake
from app.workloads import builders

log = logging.getLogger(__name__)

KEY_PREFIX = "wol:relay:"  # Redis list per standalone relay: commands its next heartbeat takes
TTL_S = 300  # a command nobody took by then is stale (the relay is gone too)


def _host(url: str) -> str | None:
    return url.removeprefix("http://").rpartition(":")[0] or None


def candidates(k8s_nodes, live_standalone, nics: dict) -> list[wake.Relay]:
    """Live nodes that can send for their subnet: Ready k8s nodes (not main) and standalone nodes with a fresh
    heartbeat; their address is the NIC they reported, else the one the cluster or the registry knows."""
    result = []
    for n in k8s_nodes:
        name = n.name.lower()
        ip = nics[name].ip if name in nics else (n.external_ip or n.internal_ip)
        if n.ready and not n.is_main and ip:
            result.append(wake.Relay(name, "k8s", n.accel, ip))
    for sn in live_standalone:
        name = sn.name.lower()
        ip = nics[name].ip if name in nics else _host(sn.url)
        if ip:
            result.append(wake.Relay(name, "standalone", sn.accel, ip))
    return result


async def send_via(state, relay: wake.Relay, target: str, mac: str, broadcast: str) -> str | None:
    """Hands the packet to the relay; the error text, or None once it is on its way (Job created, command queued)."""
    try:
        if relay.source == "k8s":
            await state.k8s.create_job(builders.wake_job(relay.name, relay.accel, target, mac, broadcast, state.settings))
        else:
            key = KEY_PREFIX + relay.name
            await state.redis.rpush(key, json.dumps({"target": target, "mac": mac, "broadcast": broadcast}))
            await state.redis.expire(key, TTL_S)
    except (K8sUnavailable, ApiException, *REDIS_ERRORS) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


async def pop_commands(redis, name: str) -> list[dict]:
    """The packets a standalone node should send now (heartbeat reply); empty when Redis does not answer."""
    key = KEY_PREFIX + name
    try:
        async with redis.pipeline(transaction=True) as pipe:
            raw, _ = await pipe.lrange(key, 0, -1).delete(key).execute()
    except REDIS_ERRORS as exc:
        log.warning("node %s: wake-on-lan relay commands unavailable: %s", name, exc)
        return []
    result = []
    for item in raw:
        try:
            cmd = json.loads(item)
            result.append({"mac": str(cmd["mac"]), "broadcast": str(cmd["broadcast"])})
        except (ValueError, KeyError, TypeError):
            continue
    return result
