"""Wake-on-LAN of class PCs: the magic packet, where it goes and when the workload controller sends it.

A node reports the physical NIC that carries its address (install-node.ps1 finds it, the node container sends it:
POST /api/nodes/wake in k3s mode, the heartbeat in standalone mode). The packet goes over UDP to ports 9 and 7 of the
directed broadcast of the node's subnet (routers of the campus forward it to the subnet) and, optionally, to the
node's last address. From main's Docker container (bridge network) a directed broadcast to another subnet is plain
routed unicast; the broadcast of main's own subnet does not leave a bridge container (host networking or an entry
of WOL_BROADCAST_OVERRIDE is needed, docs/deploy-class.md).
"""

import ipaddress
import re
import socket
from dataclasses import dataclass

PORTS = (9, 7)
RETRY_CAP_S = 1800.0  # the pause between two attempts stops growing here
TIMER_PREFIX = "wake:"  # ControllerTimer key of a node being woken; `members` holds the number of attempts sent

_MAC = re.compile(r"[0-9a-f]{2}([-:]?)[0-9a-f]{2}(?:\1[0-9a-f]{2}){4}")


def normalize_mac(mac: str) -> str | None:
    """`aa:bb:cc:dd:ee:ff` from the Windows (`AA-BB-...`), Linux (`aa:bb:...`) or bare form; None for anything that
    cannot be a NIC (all zeros, broadcast, a multicast address)."""
    mac = mac.strip().lower()
    if not _MAC.fullmatch(mac):
        return None
    raw = bytes.fromhex(re.sub("[-:]", "", mac))
    if raw[0] & 1 or raw == bytes(6):
        return None
    return ":".join(f"{b:02x}" for b in raw)


def magic_packet(mac: str) -> bytes:
    """6 bytes 0xFF, then the MAC 16 times (102 bytes)."""
    norm = normalize_mac(mac)
    if norm is None:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(norm.replace(":", "")) * 16


def directed_broadcast(ip: str, prefix: int) -> str | None:
    """The broadcast address of the subnet `ip/prefix`; None for /31 and /32, which have none."""
    if prefix > 30:
        return None
    return ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False).broadcast_address.compressed


Overrides = list[tuple[ipaddress.IPv4Network, ipaddress.IPv4Address]]


def parse_overrides(text: str) -> Overrides:
    """WOL_BROADCAST_OVERRIDE: `subnet=address` pairs separated by commas, e.g. `10.1.2.0/24=10.1.2.255`; the address
    gets the packets for the nodes of that subnet instead of its directed broadcast (a relay, or a router that wants
    another address). Most specific subnet first."""
    result: Overrides = []
    for item in (p.strip() for p in text.split(",")):
        if not item:
            continue
        subnet, sep, addr = item.partition("=")
        try:
            if not sep:
                raise ValueError
            result.append((ipaddress.IPv4Network(subnet.strip(), strict=False), ipaddress.IPv4Address(addr.strip())))
        except ValueError:
            raise ValueError(
                f"WOL_BROADCAST_OVERRIDE: «{item}» не похоже на подсеть=адрес, например 10.1.2.0/24=10.1.2.255"
            ) from None
    return sorted(result, key=lambda o: o[0].prefixlen, reverse=True)


def broadcast_for(ip: str, prefix: int, overrides: Overrides) -> str | None:
    """The override of the most specific subnet holding `ip`, else the directed broadcast of `ip/prefix`."""
    addr = ipaddress.IPv4Address(ip)
    for subnet, target in overrides:
        if addr in subnet:
            return target.compressed
    return directed_broadcast(ip, prefix)


def targets(ip: str, prefix: int, overrides: Overrides, unicast_ip: str | None) -> list[str]:
    """The addresses a magic packet goes to: the subnet's broadcast (or its override), then the node's last address."""
    result = [a for a in (broadcast_for(ip, prefix, overrides), unicast_ip) if a]
    return list(dict.fromkeys(result))


def attempt_due_at(since: float, tries: int, delay_s: float, retry_s: float) -> float:
    """When attempt number `tries + 1` is due for a node seen down since `since`: `delay_s` after that, then pauses of
    `retry_s`, doubling, at most RETRY_CAP_S each."""
    return since + delay_s + sum(min(retry_s * 2**i, RETRY_CAP_S) for i in range(tries))


def send(mac: str, addrs: list[str]) -> list[str]:
    """Sends the magic packet of `mac` to UDP ports 9 and 7 of every address; the errors, one line per failed send
    (an unreachable network must not stop the other addresses)."""
    packet = magic_packet(mac)
    errors: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in addrs:
            for port in PORTS:
                try:
                    sock.sendto(packet, (addr, port))
                except OSError as exc:
                    errors.append(f"{addr}:{port}: {exc}")
    finally:
        sock.close()
    return errors


# ---- the fallback: a live node of the same subnet sends the packet on its own network --------------------------


@dataclass(frozen=True)
class Relay:
    """A live node that could send the packet for a PC of its subnet: k8s nodes run a short Job (app.nodes.relay),
    standalone nodes get it as a command in their next heartbeat."""

    name: str
    source: str  # "k8s" | "standalone"
    accel: str | None
    ip: str


def pick_relay(target: str, ip: str, prefix: int, candidates: list[Relay]) -> Relay | None:
    """The first live node by name, other than `target`, inside the subnet `ip/prefix`; None when there is none or the
    subnet has no broadcast address (/31, /32)."""
    if directed_broadcast(ip, prefix) is None:
        return None
    net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
    for c in sorted(candidates, key=lambda c: c.name):
        try:
            inside = ipaddress.IPv4Address(c.ip) in net
        except ValueError:
            continue
        if c.name != target and inside:
            return c
    return None


def via_node(mode: str, after: int, tries: int) -> bool:
    """Attempt number `tries` also goes through a live node of the subnet: WOL_VIA_NODE=always, or `fallback` once main's
    own packets (the first WOL_VIA_NODE_AFTER attempts) did not wake the PC."""
    return mode == "always" or (mode == "fallback" and tries > after)
