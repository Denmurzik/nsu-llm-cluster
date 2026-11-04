#!/usr/bin/env python3
"""Prints the address k3s publishes in the Endpoints of the `kubernetes` service (--advertise-address).

Pods (coredns, local-path-provisioner) reach the API there, and node agents open the k3s tunnel (logs, exec) there,
so nodes on other machines need the host's LAN address. Pods reach it only if the host routes its own address back
into the container (hairpin): Linux yes, Docker Desktop with WSL mirrored networking with hostAddressLoopback=true,
Docker Desktop NAT no.

Hairpin check, before k3s starts: a listener on 0.0.0.0:6443 inside this container answers `HAIRPIN <nonce>` with
`OK <nonce>`; the client connects to MAIN_ADVERTISE_IP:6443 and sends a fresh nonce. Only the exact answer counts, so a
VPN or proxy that accepts any connection does not pass. Without hairpin the container address is printed with a warning.
The listener is closed before this process exits, so k3s can bind 6443 right after.

The address is chosen once at start (at most HAIRPIN_BUDGET_S seconds, default 8): if the host network comes up later,
restart the main container.

Environment: MAIN_ADVERTISE_IP (required); HAIRPIN_LISTEN_PORT (6443); HAIRPIN_CONNECT_PORT (= listen port, tests only);
HAIRPIN_BUDGET_S (8). Exit 64 when the listener cannot bind (e.g. --network host with 6443 taken).
"""
import ipaddress
import os
import secrets
import socket
import sys
import threading
import time

WARN = "Предупреждение"


class NoOwnAddress(Exception):
    pass


def container_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 53))  # UDP connect sends nothing, it only picks the route
            return s.getsockname()[0]
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError as exc:  # socket.gaierror included
        raise NoOwnAddress(str(exc)) from exc


def answer(conn: socket.socket) -> None:
    """One connection in its own thread, so a silent peer cannot hold up the next probe."""
    with conn:  # the server closes right after sendall
        try:
            conn.settimeout(0.5)
            line = conn.recv(128)
            if line.startswith(b"HAIRPIN ") and line.endswith(b"\n"):
                conn.sendall(b"OK " + line[len(b"HAIRPIN "):])
        except OSError:
            pass


def serve(listener: socket.socket, stop: threading.Event) -> None:
    listener.settimeout(0.2)
    while not stop.is_set():
        try:
            conn, _ = listener.accept()
        except (socket.timeout, OSError):
            continue
        threading.Thread(target=answer, args=(conn,), daemon=True).start()


def probe(ip: str, port: int, deadline: float) -> str:
    """'ok', 'wrong' (connection accepted, nonce did not come back) or 'refused' (no connection); never past `deadline`."""
    def left() -> float:
        return max(0.05, min(2.0, deadline - time.monotonic()))

    nonce = secrets.token_hex(16)
    try:
        conn = socket.create_connection((ip, port), timeout=left())
    except OSError:
        return "refused"
    reply = b""
    try:
        conn.sendall(f"HAIRPIN {nonce}\n".encode())
        while not reply.endswith(b"\n") and len(reply) < 128 and time.monotonic() < deadline:
            conn.settimeout(left())
            chunk = conn.recv(128)
            if not chunk:
                break
            reply += chunk
    except OSError:
        pass
    finally:
        conn.close()
    return "ok" if reply == f"OK {nonce}\n".encode() else "wrong"


def main() -> int:
    lan = os.environ.get("MAIN_ADVERTISE_IP", "")
    if not lan:
        print("Ошибка: MAIN_ADVERTISE_IP не задан.", file=sys.stderr)
        return 64
    listen_port = int(os.environ.get("HAIRPIN_LISTEN_PORT", "6443"))
    connect_port = int(os.environ.get("HAIRPIN_CONNECT_PORT", str(listen_port)))
    budget = float(os.environ.get("HAIRPIN_BUDGET_S", "8"))
    try:
        own = container_ip()
    except NoOwnAddress as exc:
        print(f"Ошибка: не удалось определить собственный адрес контейнера для --advertise-address ({exc}).",
              file=sys.stderr)
        return 64

    try:
        loopback = ipaddress.ip_address(lan).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        print(f"{WARN}: MAIN_ADVERTISE_IP={lan} — адрес loopback, ноды на других машинах его не достанут; API объявлен "
              f"по адресу контейнера {own}.", file=sys.stderr)
        print(own)
        return 0

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("0.0.0.0", listen_port))
    except OSError as exc:
        listener.close()
        print(f"Ошибка: порт {listen_port} занят ({exc.strerror}): k3s не сможет его открыть. Освободите порт "
              f"(при --network host его может держать другой процесс хоста).", file=sys.stderr)
        return 64
    listener.listen(8)
    stop = threading.Event()
    thread = threading.Thread(target=serve, args=(listener, stop), daemon=True)
    thread.start()
    result = "refused"
    try:
        deadline = time.monotonic() + budget
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = probe(lan, connect_port, deadline)
            if result == "ok":
                break
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    finally:
        stop.set()
        thread.join(timeout=3)
        listener.close()

    if result == "ok":
        print(lan)
        return 0
    if result == "wrong":
        reason = f"соединение с {lan}:{connect_port} принято, но ответ с nonce не вернулся (VPN или прокси?)"
    else:
        reason = f"нет соединения с {lan}:{connect_port}"
    print(f"{WARN}: нет hairpin — {reason}. API объявлен по адресу контейнера {own}: логи и exec подов на других "
          f"машинах работать не будут. Адрес выбирается при старте: после настройки сети перезапустите главный сервер.",
          file=sys.stderr)
    print(own)
    return 0


if __name__ == "__main__":
    sys.exit(main())
