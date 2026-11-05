#!/usr/bin/env python3
"""Minimal stdlib mock of the portal node registry (Task 10 contract) for node tests.

  mock_main.py --port 29090 --calls calls.json --desired desired.json [--token T] [--interval 3]

* Every request is appended to --calls (a JSON array of {method, path, body, auth}).
* --desired is re-read on each heartbeat; a missing file or JSON null means "no desired config".
* The first heartbeat succeeds, the second answers 404 once (forces a re-register).
* POST /api/nodes/<name>/image (the image the node runs) answers 200.
"""
import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = None
lock = threading.Lock()
calls = []
state = {"heartbeats": 0, "not_found_sent": False}


def dump_calls():
    tmp = args.calls + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(calls, f)
    for _ in range(50):
        try:
            os.replace(tmp, args.calls)
            return
        except PermissionError:  # reader holds the file open (Windows)
            threading.Event().wait(0.05)


def read_desired():
    try:
        with open(args.desired, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # quiet
        pass

    def _send(self, code, payload=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = {"_raw": raw.decode(errors="replace")}
        auth = self.headers.get("Authorization", "")
        with lock:
            calls.append({"method": method, "path": self.path, "body": body, "auth": auth})
            dump_calls()
            if args.token and auth != f"Bearer {args.token}":
                return self._send(401, {"detail": "unauthorized"})
            if method == "POST" and self.path == "/api/nodes/register":
                return self._send(200, {"ok": True, "heartbeat_interval": args.interval})
            if method == "POST" and self.path == "/api/nodes/heartbeat":
                state["heartbeats"] += 1
                if state["heartbeats"] == 2 and not state["not_found_sent"]:
                    state["not_found_sent"] = True
                    return self._send(404, {"detail": "unknown node"})
                return self._send(
                    200, {"ok": True, "heartbeat_interval": args.interval, "desired": read_desired()}
                )
            if method == "DELETE" and self.path.startswith("/api/nodes/"):
                return self._send(204)
            if method == "POST" and self.path.startswith("/api/nodes/") and self.path.endswith("/stop"):
                return self._send(204)
            # what the node container runs (task 2-30); a portal older than that answers 404 here
            if method == "POST" and self.path.startswith("/api/nodes/") and self.path.endswith("/image"):
                return self._send(200, {"ok": True})
            return self._send(404, {"detail": "not found"})

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_GET(self):
        self._handle("GET")


def main():
    global args
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--calls", required=True)
    p.add_argument("--desired", required=True)
    p.add_argument("--token", default="")
    p.add_argument("--interval", type=int, default=3)
    args = p.parse_args()
    with lock:
        dump_calls()
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
