"""Wake-on-LAN of class PCs (task 2-18): the packet, where it goes, when it is sent (pure parts, no database)."""

import socket

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.nodes import wake


# ---- the packet -------------------------------------------------------------------------------------


def test_magic_packet_is_six_ff_then_sixteen_macs():
    packet = wake.magic_packet("00:1A:2b:3c:4D:5e")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("001a2b3c4d5e") * 16


@pytest.mark.parametrize(
    "raw",
    ["00-1A-2B-3C-4D-5E", "00:1a:2b:3c:4d:5e", "001A2B3C4D5E", " 00-1a-2b-3c-4d-5e "],
)
def test_mac_forms_of_windows_and_linux_are_accepted(raw):
    assert wake.normalize_mac(raw) == "00:1a:2b:3c:4d:5e"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "00:1a:2b:3c:4d",  # too short
        "00:1a:2b:3c:4d:5e:6f",
        "00-1a:2b-3c:4d-5e",  # mixed separators
        "zz:1a:2b:3c:4d:5e",
        "00:00:00:00:00:00",
        "ff:ff:ff:ff:ff:ff",
        "01:00:5e:00:00:01",  # multicast: never a NIC
    ],
)
def test_bad_macs_are_rejected(raw):
    assert wake.normalize_mac(raw) is None
    with pytest.raises(ValueError):
        wake.magic_packet(raw)


# ---- where it goes ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ip", "prefix", "broadcast"),
    [
        ("192.168.0.193", 24, "192.168.0.255"),
        ("10.1.2.3", 23, "10.1.3.255"),
        ("10.20.30.40", 16, "10.20.255.255"),
        ("172.16.5.9", 30, "172.16.5.11"),
        ("10.0.0.1", 31, None),  # point-to-point: no broadcast address
        ("10.0.0.1", 32, None),
    ],
)
def test_directed_broadcast_of_the_node_subnet(ip, prefix, broadcast):
    assert wake.directed_broadcast(ip, prefix) == broadcast


def test_overrides_parse_and_the_most_specific_wins():
    overrides = wake.parse_overrides(" 10.1.0.0/16=10.1.255.255 , 10.1.2.0/24=10.1.2.7,192.168.0.0/24=192.168.0.255")
    assert wake.broadcast_for("10.1.2.3", 24, overrides) == "10.1.2.7"
    assert wake.broadcast_for("10.1.9.3", 24, overrides) == "10.1.255.255"
    assert wake.broadcast_for("10.2.1.3", 24, overrides) == "10.2.1.255"  # no override: the directed broadcast
    assert wake.parse_overrides("") == []


@pytest.mark.parametrize(
    "text",
    ["10.1.2.0/24", "10.1.2.0/24=", "10.1.2.0/33=10.1.2.255", "nonsense=10.1.2.255", "10.1.2.0/24=10.1.2.300"],
)
def test_bad_overrides_are_rejected_in_russian(text):
    with pytest.raises(ValueError, match="WOL_BROADCAST_OVERRIDE"):
        wake.parse_overrides(text)


def test_settings_validate_the_override_map():
    assert Settings(wol_broadcast_override="10.1.2.0/24=10.1.2.255").wol_overrides[0][1].compressed == "10.1.2.255"
    with pytest.raises(ValidationError, match="WOL_BROADCAST_OVERRIDE"):
        Settings(wol_broadcast_override="10.1.2.0/24")


def test_settings_defaults():
    s = Settings()
    assert (s.wol_enabled, s.wol_delay_s, s.wol_max_tries, s.wol_unicast) == (True, 60.0, 6, True)
    assert s.wol_overrides == []


def test_targets_broadcast_then_unicast_without_duplicates():
    assert wake.targets("10.1.2.3", 24, [], "10.1.2.3") == ["10.1.2.255", "10.1.2.3"]
    assert wake.targets("10.1.2.3", 24, [], None) == ["10.1.2.255"]
    assert wake.targets("10.0.0.1", 32, [], "10.0.0.1") == ["10.0.0.1"]  # no broadcast: unicast only
    assert wake.targets("10.0.0.1", 32, [], None) == []
    overrides = wake.parse_overrides("10.1.2.0/24=10.1.2.3")
    assert wake.targets("10.1.2.3", 24, overrides, "10.1.2.3") == ["10.1.2.3"]


# ---- when it is sent ------------------------------------------------------------------------------


def test_attempts_back_off_from_the_delay():
    since, delay, retry = 1000.0, 60.0, 60.0
    due = [wake.attempt_due_at(since, n, delay, retry) for n in range(7)]
    assert due == [1060.0, 1120.0, 1240.0, 1480.0, 1960.0, 2920.0, 4720.0]  # +60, +120, +240, +480, +960, +1800
    # the pause between two attempts stops growing at RETRY_CAP_S
    assert wake.attempt_due_at(0.0, 20, 0.0, 60.0) - wake.attempt_due_at(0.0, 19, 0.0, 60.0) == wake.RETRY_CAP_S


# ---- sending ----------------------------------------------------------------------------------------


class FakeSocket:
    def __init__(self, sent, fail_on=()):
        self.sent, self.fail_on, self.options = sent, fail_on, []

    def setsockopt(self, level, name, value):
        self.options.append((level, name, value))

    def sendto(self, data, addr):
        if addr[0] in self.fail_on:
            raise OSError(101, "Network is unreachable")
        self.sent.append((data, addr))

    def close(self):
        pass


def test_send_goes_to_ports_9_and_7_of_every_target(monkeypatch):
    sent: list = []
    sockets: list[FakeSocket] = []

    def make(*_):
        sockets.append(FakeSocket(sent))
        return sockets[-1]

    monkeypatch.setattr(wake.socket, "socket", make)
    errors = wake.send("00:1a:2b:3c:4d:5e", ["10.1.2.255", "10.1.2.3"])
    assert errors == []
    assert [addr for _, addr in sent] == [("10.1.2.255", 9), ("10.1.2.255", 7), ("10.1.2.3", 9), ("10.1.2.3", 7)]
    assert all(data == wake.magic_packet("00:1a:2b:3c:4d:5e") for data, _ in sent)
    assert (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in sockets[0].options


def test_send_reports_errors_and_keeps_going(monkeypatch):
    sent: list = []
    monkeypatch.setattr(wake.socket, "socket", lambda *_: FakeSocket(sent, fail_on=("10.1.2.255",)))
    errors = wake.send("00:1a:2b:3c:4d:5e", ["10.1.2.255", "10.1.2.3"])
    assert [addr for _, addr in sent] == [("10.1.2.3", 9), ("10.1.2.3", 7)]
    assert len(errors) == 2 and all("10.1.2.255" in e for e in errors)


# ---- the fallback: a live node of the same subnet sends it ------------------------------------------


CANDIDATES = [
    wake.Relay("pc09", "k8s", "cpu", "10.1.2.9"),
    wake.Relay("pc03", "standalone", "cpu", "10.1.2.3"),
    wake.Relay("pc02", "k8s", "nvidia", "10.1.3.2"),
    wake.Relay("pc01", "k8s", "cpu", "10.1.2.1"),
]


def test_relay_is_the_first_live_node_by_name_in_the_subnet():
    assert wake.pick_relay("pc05", "10.1.2.5", 24, CANDIDATES).name == "pc01"
    assert wake.pick_relay("pc01", "10.1.2.1", 24, CANDIDATES).name == "pc03"  # never the node being woken
    assert wake.pick_relay("pc05", "10.1.3.5", 24, CANDIDATES).name == "pc02"
    assert wake.pick_relay("pc05", "10.1.3.5", 23, CANDIDATES).name == "pc01"  # /23 holds both
    assert wake.pick_relay("pc05", "10.9.0.5", 24, CANDIDATES) is None  # nobody alive in that subnet
    assert wake.pick_relay("pc05", "10.1.2.5", 31, CANDIDATES) is None  # no broadcast address to send to


@pytest.mark.parametrize(
    ("mode", "after", "tries", "via"),
    [
        ("fallback", 2, 1, False),
        ("fallback", 2, 2, False),
        ("fallback", 2, 3, True),  # main's packets did not wake it
        ("fallback", 0, 1, True),
        ("always", 2, 1, True),
        ("off", 2, 6, False),
    ],
)
def test_relay_joins_after_mains_own_attempts(mode, after, tries, via):
    assert wake.via_node(mode, after, tries) is via


def test_relay_settings():
    s = Settings()
    assert (s.wol_via_node, s.wol_via_node_after) == ("fallback", 2)
    assert Settings(wol_via_node="always").wol_via_node == "always"
    with pytest.raises(ValidationError):
        Settings(wol_via_node="sometimes")


def test_wake_job_runs_the_sender_on_the_relay_node():
    from app.workloads import builders

    job = builders.wake_job("pc03", "nvidia", "pc01", "00:1a:2b:3c:4d:5e", "10.1.2.255", Settings())
    assert (job["apiVersion"], job["kind"]) == ("batch/v1", "Job")
    meta = job["metadata"]
    assert meta["generateName"] == "llm-wake-" and meta["namespace"] == "llm"
    assert meta["labels"] == {"app.kubernetes.io/part-of": "nsu-llm", "llm.nsu/role": "wake", "llm.nsu/wake-target": "pc01"}
    spec = job["spec"]
    # one try, a minute at most, the finished Job is collected by Kubernetes
    assert (spec["backoffLimit"], spec["activeDeadlineSeconds"], spec["ttlSecondsAfterFinished"]) == (0, 60, 300)
    pod = spec["template"]["spec"]
    assert pod["nodeName"] == "pc03"  # no scheduling: exactly the relay
    assert pod["hostNetwork"] is True  # the broadcast of the PC's own network, not of the pod network
    assert pod["restartPolicy"] == "Never"
    assert {"key": "llm.nsu/class", "operator": "Equal", "value": "true", "effect": "NoSchedule"} in pod["tolerations"]
    assert "runtimeClassName" not in pod and "serviceAccountName" not in pod
    assert pod["automountServiceAccountToken"] is False
    (c,) = pod["containers"]
    assert c["image"] == "nsu-llm-node:0.2.1-gpu" and c["imagePullPolicy"] == "Never"
    assert c["command"] == ["wol-send.sh", "00:1a:2b:3c:4d:5e", "10.1.2.255"]
    assert c["securityContext"] == {"allowPrivilegeEscalation": False, "privileged": False}
    assert "env" not in c and "envFrom" not in c  # no secrets in the relay pod
