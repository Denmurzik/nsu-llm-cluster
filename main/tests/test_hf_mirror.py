"""Tests of main/hf_mirror.py against a fake Hugging Face (API + resolve on one host, the LFS CDN on another)."""

import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MAIN))

import hf_mirror  # noqa: E402

DISK_RESERVE_OF = hf_mirror.disk_reserve  # the real one: the fixture below pins it for the other tests


@pytest.fixture(autouse=True)
def _disk_reserve(monkeypatch):
    """The cache keeps 15 % of the volume free: the test host's own disk may be fuller than that."""
    monkeypatch.setattr(hf_mirror, "disk_reserve", lambda total: hf_mirror.DISK_RESERVE)


UPSTREAM_TOKEN = "hf_upstreamsecret0000000000000000000000000"
MIRROR_TOKEN = "hf_" + "ab" * 20
COMMIT = "c" * 40
AUTH = {"Authorization": f"Bearer {MIRROR_TOKEN}"}


def blob(n: int, seed: int = 1) -> bytes:
    return bytes((i * 31 + seed) % 251 for i in range(n))


class QuietServer(ThreadingHTTPServer):
    """A client that drops the connection (a cancelled download) is not an error worth a traceback."""

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            super().handle_error(request, client_address)


class FakeHub:
    """Two servers: `hub` answers the API and /resolve (LFS files redirect to `cdn`, a different host:port)."""

    def __init__(self):
        self.files: dict[tuple[str, str], bytes] = {}
        self.at_commit: dict[tuple[str, str, str], bytes] = {}  # (repo, commit, path): that commit's content, direct
        self.lfs: set[tuple[str, str]] = set()
        self.cdn_gets: list[dict] = []  # headers of every GET to the CDN
        self.hub_requests: list[tuple[str, str, dict]] = []
        self.chunk_delay = 0.0  # seconds between 64 KiB chunks of a CDN body
        self.cut_after: int | None = None  # the CDN drops the connection after this many bytes (once)
        self.link: str | None = None  # Link header of tree answers
        self.stall_after: int | None = None
        self.stall_s = 0.0
        hub, cdn = self, self

        class Hub(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self.handle_hub(head=True)

            def do_GET(self):
                self.handle_hub(head=False)

            def send_json(self, code, obj, extra=None):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def handle_hub(self, head):
                hub.hub_requests.append((self.command, self.path, dict(self.headers)))
                m = re.fullmatch(r"/api/models/([^/]+/[^/]+)/refs", self.path)
                if m:
                    return self.send_json(200, {"branches": [{"name": "main", "ref": "refs/heads/main", "targetCommit": COMMIT}], "tags": [], "converts": []})
                m = re.fullmatch(r"/api/models/([^/]+/[^/]+)/tree/[^/?]+(\?.*)?", self.path)
                if m:
                    items = [{"type": "file", "path": p, "size": len(d), "oid": "1" * 40} for (r, p), d in hub.files.items() if r == m.group(1)]
                    return self.send_json(200, items, {"Link": hub.link} if hub.link else None)
                m = re.fullmatch(r"/([^/]+/[^/]+)/resolve/([^/]+)/(.+)", self.path)
                if m and m.groups() in hub.at_commit:
                    data = hub.at_commit[m.groups()]
                    self.send_response(200)
                    self.send_header("X-Repo-Commit", m.group(2))
                    self.send_header("ETag", f'"{hashlib.sha256(data).hexdigest()}"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if not head:
                        self.wfile.write(data)
                    return
                if m and (m.group(1), m.group(3)) in hub.files:
                    key = (m.group(1), m.group(3))
                    data = hub.files[key]
                    etag = hashlib.sha256(data).hexdigest()
                    if key in hub.lfs:
                        self.send_response(302)
                        self.send_header("Location", f"http://127.0.0.1:{cdn.cdn_port}/cdn/{m.group(1)}/{m.group(3)}")
                        self.send_header("X-Repo-Commit", COMMIT)
                        self.send_header("X-Linked-Etag", f'"{etag}"')
                        self.send_header("X-Linked-Size", str(len(data)))
                        self.send_header("X-Xet-Hash", "deadbeef")
                        self.send_header("ETag", '"pointer-etag"')
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("X-Repo-Commit", COMMIT)
                    self.send_header("ETag", f'"{etag}"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if not head:
                        self.wfile.write(data)
                    return
                self.send_json(404, {"error": "Repository not found"})

        class Cdn(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self.serve(head=True)

            def do_GET(self):
                cdn.cdn_gets.append(dict(self.headers))
                self.serve(head=False)

            def serve(self, head):
                m = re.fullmatch(r"/cdn/([^/]+/[^/]+)/(.+)", self.path)
                data = hub.files.get((m.group(1), m.group(2))) if m else None
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, stop = 0, len(data)
                rng = self.headers.get("Range")
                if rng:
                    m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng)
                    start = int(m.group(1))
                    if start >= len(data):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    stop = int(m.group(2)) + 1 if m.group(2) else len(data)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{stop - 1}/{len(data)}")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(stop - start))
                self.send_header("ETag", '"cdn-etag"')
                self.end_headers()
                if head:
                    return
                pos = start
                data = data[:stop]
                while pos < len(data):
                    if hub.cut_after is not None and pos >= hub.cut_after:
                        hub.cut_after = None
                        self.close_connection = True
                        self.connection.shutdown(2)
                        return
                    end = min(len(data), pos + 65536)
                    if hub.cut_after is not None:
                        end = min(end, hub.cut_after)
                    self.wfile.write(data[pos:end])
                    pos = end
                    if hub.stall_after is not None and pos >= hub.stall_after:
                        hub.stall_after = None
                        self.wfile.flush()
                        time.sleep(hub.stall_s)
                    if hub.chunk_delay:
                        time.sleep(hub.chunk_delay)

        self.hub_server = QuietServer(("127.0.0.1", 0), Hub)
        self.cdn_server = QuietServer(("127.0.0.1", 0), Cdn)
        self.hub_server.daemon_threads = self.cdn_server.daemon_threads = True
        self.cdn_port = self.cdn_server.server_address[1]
        self.url = f"http://127.0.0.1:{self.hub_server.server_address[1]}"
        for s in (self.hub_server, self.cdn_server):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    def add(self, repo, path, data, lfs=True):
        self.files[(repo, path)] = data
        if lfs:
            self.lfs.add((repo, path))

    def close(self):
        for s in (self.hub_server, self.cdn_server):
            s.shutdown()
            s.server_close()


class Running:
    def __init__(self, mirror):
        self.mirror = mirror
        self.server = hf_mirror.make_server(mirror, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def request(self, method, path, headers=None, auth=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, headers={**(AUTH if auth else {}), **(headers or {})})
        resp = conn.getresponse()
        try:
            body = resp.read()
        except http.client.IncompleteRead as e:  # the mirror closed a body early
            body = e.partial
        conn.close()
        return resp, body

    def settle(self):
        """Waits until the downloads have renamed their files (a client may get the last byte a moment earlier)."""
        deadline = time.monotonic() + 10
        while self.mirror.downloads and time.monotonic() < deadline:
            time.sleep(0.01)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hub():
    h = FakeHub()
    yield h
    h.close()


@pytest.fixture
def start(tmp_path):
    started = []

    def _start(upstream, **kw):
        kw = {"root": tmp_path / "data", "upstream": upstream, "upstream_token": UPSTREAM_TOKEN,
              "mirror_token": MIRROR_TOKEN, "max_bytes": 10**9, "revalidate_s": 3600, **kw}
        r = Running(hf_mirror.Mirror(**kw))
        started.append(r)
        return r

    yield _start
    for r in started:
        r.close()


DEAD = "http://127.0.0.1:9"  # nothing listens: connection refused


def test_head_answers_with_the_metadata_huggingface_hub_and_llama_cpp_read(hub, start):
    data = blob(300_000)
    hub.add("org/m", "model.gguf", data)
    m = start(hub.url)
    resp, body = m.request("HEAD", "/org/m/resolve/main/model.gguf")
    assert resp.status == 200 and body == b""
    assert resp.getheader("Content-Length") == str(len(data))
    assert resp.getheader("X-Linked-Size") == str(len(data))
    etag = hashlib.sha256(data).hexdigest()
    assert resp.getheader("ETag") == f'"{etag}"' and resp.getheader("X-Linked-Etag") == f'"{etag}"'
    assert resp.getheader("X-Repo-Commit") == COMMIT
    assert resp.getheader("Accept-Ranges") == "bytes"
    assert resp.getheader("Location") is None
    assert not [h for h in resp.getheaders() if h[0].lower().startswith("x-xet")]
    assert hub.cdn_gets == []  # a HEAD does not start a download


def test_parallel_clients_of_one_file_share_one_download(hub, start):
    data = blob(1_000_000)
    hub.add("org/m", "model.gguf", data)
    hub.chunk_delay = 0.01
    m = start(hub.url)
    results = []

    def get():
        results.append(m.request("GET", "/org/m/resolve/main/model.gguf"))

    threads = [threading.Thread(target=get) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(results) == 4
    for resp, body in results:
        assert resp.status == 200 and body == data
    assert len(hub.cdn_gets) == 1
    m.settle()
    assert (m.mirror.root / "cache" / "org" / "m" / "model.gguf").read_bytes() == data
    m.request("GET", "/org/m/resolve/main/model.gguf")
    assert len(hub.cdn_gets) == 1  # served from the cache


def test_range_requests_resume_a_client_download(hub, start):
    data = blob(200_000)
    hub.add("org/m", "model.gguf", data)
    m = start(hub.url)
    assert m.request("GET", "/org/m/resolve/main/model.gguf")[0].status == 200
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf", {"Range": "bytes=150000-"})
    assert resp.status == 206 and body == data[150000:]
    assert resp.getheader("Content-Range") == f"bytes 150000-{len(data) - 1}/{len(data)}"
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf", {"Range": "bytes=10-19"})
    assert resp.status == 206 and body == data[10:20]
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf", {"Range": "bytes=-5"})
    assert resp.status == 206 and body == data[-5:]
    resp, _ = m.request("GET", "/org/m/resolve/main/model.gguf", {"Range": f"bytes={len(data)}-"})
    assert resp.status == 416 and resp.getheader("Content-Range") == f"bytes */{len(data)}"


def test_a_range_request_during_the_download_waits_for_its_bytes(hub, start):
    data = blob(800_000)
    hub.add("org/m", "model.gguf", data)
    hub.chunk_delay = 0.01
    m = start(hub.url)
    first = []
    t = threading.Thread(target=lambda: first.append(m.request("GET", "/org/m/resolve/main/model.gguf")))
    t.start()
    time.sleep(0.05)
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf", {"Range": "bytes=600000-"})
    t.join(60)
    assert resp.status == 206 and body == data[600000:]
    assert first[0][1] == data and len(hub.cdn_gets) == 1


def test_an_interrupted_download_resumes_after_a_mirror_restart(hub, start):
    data = blob(500_000)
    hub.add("org/m", "model.gguf", data)
    hub.cut_after = 200_000
    m = start(hub.url)
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf")
    assert len(body) < len(data)  # the client sees a short body (the upstream connection broke)
    m.close()
    m2 = start(hub.url)
    resp, body = m2.request("GET", "/org/m/resolve/main/model.gguf")
    assert resp.status == 200 and body == data
    assert hub.cdn_gets[-1].get("Range") == "bytes=200000-"


def test_the_mirror_token_is_required_and_tokens_stay_where_they_belong(hub, start):
    hub.add("org/m", "model.gguf", blob(1000))
    m = start(hub.url)
    assert m.request("GET", "/org/m/resolve/main/model.gguf", auth=False)[0].status == 401
    assert m.request("GET", "/org/m/resolve/main/model.gguf", {"Authorization": "Bearer hf_wrong"}, auth=False)[0].status == 401
    assert m.request("GET", "/api/models/org/m/refs", auth=False)[0].status == 401
    assert m.request("GET", "/_mirror/transfers", auth=False)[0].status == 401
    assert m.request("GET", "/_mirror/health", auth=False)[0].status == 200
    assert m.request("GET", "/org/m/resolve/main/model.gguf")[0].status == 200
    hub_auth = [h.get("Authorization") for _, _, h in hub.hub_requests]
    assert hub_auth and set(hub_auth) == {f"Bearer {UPSTREAM_TOKEN}"}  # the mirror token never goes upstream
    assert hub.cdn_gets and all("Authorization" not in h for h in hub.cdn_gets)  # nor any token to another host


def test_least_recently_used_files_are_evicted_to_stay_under_the_limit(hub, start):
    size = 100_000
    for name in "abc":
        hub.add("org/m", f"{name}.gguf", blob(size, ord(name)))
    m = start(hub.url, max_bytes=int(size * 2.5))
    models = m.mirror.root / "cache" / "org" / "m"
    (m.mirror.root / "offline" / "local" / "own").mkdir(parents=True)
    (m.mirror.root / "offline" / "local" / "own" / "x.gguf").write_bytes(b"x" * 10)  # put by hand: never evicted
    (m.mirror.root / "models").mkdir(exist_ok=True)
    (m.mirror.root / "models" / "own.gguf").write_bytes(b"y" * 10)  # reserved for local models (task 2-14)
    for name in ("a", "b"):
        assert m.request("GET", f"/org/m/resolve/main/{name}.gguf")[0].status == 200
        m.settle()
        time.sleep(0.02)
    assert m.request("GET", "/org/m/resolve/main/a.gguf")[0].status == 200  # a is now more recent than b
    time.sleep(0.02)
    resp, body = m.request("GET", "/org/m/resolve/main/c.gguf")
    assert resp.status == 200 and body == blob(size, ord("c"))
    m.settle()
    assert sorted(p.name for p in models.iterdir()) == ["a.gguf", "c.gguf"]
    assert (m.mirror.root / "offline" / "local" / "own" / "x.gguf").exists()
    assert (m.mirror.root / "models" / "own.gguf").exists()


def test_a_file_larger_than_the_limit_is_refused_with_507(hub, start):
    hub.add("org/m", "big.gguf", blob(300_000))
    m = start(hub.url, max_bytes=100_000)
    resp, body = m.request("GET", "/org/m/resolve/main/big.gguf")
    assert resp.status == 507 and "error" in json.loads(body)
    assert hub.cdn_gets == [] or not (m.mirror.root / "cache" / "org" / "m" / "big.gguf").exists()


def test_upstream_errors_pass_through(hub, start):
    m = start(hub.url)
    resp, body = m.request("GET", "/org/missing/resolve/main/x.gguf")
    assert resp.status == 404
    resp, body = m.request("HEAD", "/org/missing/resolve/main/x.gguf")
    assert resp.status == 404


def test_metadata_api_is_proxied_and_kept_for_offline_use(hub, start):
    hub.add("org/m", "model.gguf", blob(1000))
    m = start(hub.url)
    resp, body = m.request("GET", "/api/models/org/m/refs")
    assert resp.status == 200 and json.loads(body)["branches"][0]["targetCommit"] == COMMIT
    resp, body = m.request("GET", "/api/models/org/m/tree/main?recursive=true")
    assert resp.status == 200 and json.loads(body)[0]["path"] == "model.gguf"
    m.close()
    offline = start(DEAD)
    resp, body = offline.request("GET", "/api/models/org/m/tree/main?recursive=true")
    assert resp.status == 200 and json.loads(body)[0]["path"] == "model.gguf"
    resp, body = offline.request("GET", "/api/models/org/unknown/refs")
    assert resp.status == 502 and "error" in json.loads(body)


def test_link_headers_point_back_at_the_mirror(hub, start):
    hub.add("org/m", "model.gguf", blob(1000))
    hub.link = f'<{hub.url}/api/models/org/m/tree/main?recursive=true&cursor=abc>; rel="next"'
    m = start(hub.url, prefix="/hf")
    resp, _ = m.request(
        "GET", "/c/node-a/api/models/org/m/tree/main?recursive=true",
        {"Host": "llm.example", "X-Forwarded-Proto": "https"},
    )
    assert resp.getheader("Link") == '<https://llm.example/hf/c/node-a/api/models/org/m/tree/main?recursive=true&cursor=abc>; rel="next"'


def test_airgap_files_put_by_hand_are_served_with_synthesized_metadata(start):
    m = start(DEAD)
    repo_dir = m.mirror.root / "offline" / "org" / "big-GGUF"
    (repo_dir / "Q2").mkdir(parents=True)
    shards = {f"Q2/big-0000{i}-of-00002.gguf": blob(5000, i) for i in (1, 2)}
    for p, d in shards.items():
        (repo_dir / p).write_bytes(d)
    refs = json.loads(m.request("GET", "/api/models/org/big-GGUF/refs")[1])
    commit = refs["branches"][0]["targetCommit"]
    assert refs["branches"][0]["name"] == "main" and re.fullmatch(r"[0-9a-f]{40}", commit)
    tree = json.loads(m.request("GET", f"/api/models/org/big-GGUF/tree/{commit}?recursive=true")[1])
    files = {i["path"]: i for i in tree if i["type"] == "file"}
    assert set(files) == set(shards)
    for p, item in files.items():
        assert item["size"] == len(shards[p]) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", item["oid"])
    info = json.loads(m.request("GET", "/api/models/org/big-GGUF/revision/main")[1])
    assert info["sha"] == commit and {s["rfilename"] for s in info["siblings"]} == set(shards)
    path = f"/org/big-GGUF/resolve/{commit}/Q2/big-00002-of-00002.gguf"
    resp, _ = m.request("HEAD", path)
    assert resp.status == 200 and resp.getheader("X-Repo-Commit") == commit and resp.getheader("ETag")
    resp, body = m.request("GET", path, {"Range": "bytes=4000-"})
    assert resp.status == 206 and body == shards["Q2/big-00002-of-00002.gguf"][4000:]


def test_sharded_gguf_files_are_cached_side_by_side(hub, start):
    for i in (1, 2, 3):
        hub.add("org/m", f"m-0000{i}-of-00003.gguf", blob(10_000, i))
    m = start(hub.url)
    for i in (1, 2, 3):
        resp, body = m.request("GET", f"/org/m/resolve/{COMMIT}/m-0000{i}-of-00003.gguf")
        assert resp.status == 200 and body == blob(10_000, i)
    m.settle()
    assert sorted(p.name for p in (m.mirror.root / "cache" / "org" / "m").iterdir()) == [
        "m-00001-of-00003.gguf", "m-00002-of-00003.gguf", "m-00003-of-00003.gguf",
    ]


def test_a_file_changed_upstream_is_downloaded_again(hub, start):
    hub.add("org/m", "model.gguf", blob(1000, 1))
    m = start(hub.url, revalidate_s=0)
    assert m.request("GET", "/org/m/resolve/main/model.gguf")[1] == blob(1000, 1)
    m.settle()
    hub.add("org/m", "model.gguf", blob(1200, 2))
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf")
    assert body == blob(1200, 2)
    m.settle()
    assert m.request("GET", "/org/m/resolve/main/model.gguf")[1] == blob(1200, 2)


def test_a_cached_file_is_served_while_huggingface_is_down(hub, start):
    hub.add("org/m", "model.gguf", blob(1000))
    m = start(hub.url)
    m.request("GET", "/org/m/resolve/main/model.gguf")
    m.settle()
    m.close()
    offline = start(DEAD, revalidate_s=0)
    resp, body = offline.request("GET", "/org/m/resolve/main/model.gguf")
    assert resp.status == 200 and body == blob(1000)
    resp, _ = offline.request("HEAD", "/org/m/resolve/main/model.gguf")
    assert resp.status == 200 and resp.getheader("X-Repo-Commit") == COMMIT


def test_transfers_report_bytes_sent_to_each_client(hub, start):
    data = blob(50_000)
    hub.add("org/m", "model.gguf", data)
    m = start(hub.url)
    assert m.request("GET", "/c/node-a/org/m/resolve/main/model.gguf", {"Range": "bytes=0-"})[0].status == 206
    m.request("GET", "/c/node-b/org/m/resolve/main/model.gguf", {"Range": "bytes=40000-"})
    resp, body = m.request("GET", "/_mirror/transfers")
    clients = json.loads(body)["clients"]
    (a,) = clients["node-a"]
    assert (a["repo"], a["path"], a["offset"], a["size"], a["active"]) == ("org/m", "model.gguf", len(data), len(data), False)
    assert clients["node-b"][0]["offset"] == len(data)
    assert isinstance(a["updated"], float)


def test_bad_paths_are_rejected(start):
    m = start(DEAD)
    for path in (
        "/org/m/resolve/main/../../../etc/passwd",
        "/org/m/resolve/main/%2e%2e/x",
        "/../x/resolve/main/f",
        "/org/m/resolve/main/",
        "/c/bad%20id/org/m/resolve/main/f",
        "/datasets/org/d/resolve/main/f",
    ):
        assert m.request("GET", path)[0].status in (400, 404), path


def test_a_bounded_range_of_a_file_not_in_the_mirror_is_passed_through(hub, start):
    # the portal's inspector reads the GGUF header (bytes=0-N): no download of a 50 GB shard for that
    data = blob(300_000)
    hub.add("org/m", "model.gguf", data)
    m = start(hub.url)
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf", {"Range": "bytes=10-19"})
    assert resp.status == 206 and body == data[10:20]
    assert resp.getheader("Content-Range") == f"bytes 10-19/{len(data)}"
    assert [g.get("Range") for g in hub.cdn_gets] == ["bytes=10-19"]
    m.settle()
    assert not (m.mirror.root / "cache" / "org" / "m" / "model.gguf").exists()
    assert not list((m.mirror.root / ".mirror" / "parts").iterdir())
    # an open range (a client resuming its download) goes through the cache
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf", {"Range": "bytes=100-"})
    assert resp.status == 206 and body == data[100:]
    m.settle()
    assert (m.mirror.root / "cache" / "org" / "m" / "model.gguf").read_bytes() == data


def test_links_for_a_direct_client_have_no_caddy_prefix(hub, start):
    hub.add("org/m", "model.gguf", blob(1000))
    hub.link = f'<{hub.url}/api/models/org/m/tree/main?cursor=abc>; rel="next"'
    m = start(hub.url, prefix="/hf")
    resp, _ = m.request("GET", "/api/models/org/m/tree/main", {"Host": "127.0.0.1:8090"})
    assert resp.getheader("Link") == '<http://127.0.0.1:8090/api/models/org/m/tree/main?cursor=abc>; rel="next"'


# ---------------------------------------------------------------- fix round 1 (rev-23)


class SlowClient:
    """A client that reads the response head and then stops reading (its download is in progress)."""

    def __init__(self, port, path, rcvbuf=4096):
        import socket

        self.sock = socket.create_connection(("127.0.0.1", port))
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {MIRROR_TOKEN}\r\n\r\n".encode())
        self.head = b""
        while b"\r\n\r\n" not in self.head:
            self.head += self.sock.recv(1)

    def close(self):
        self.sock.close()


def wait_for(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


def test_concurrent_downloads_reserve_their_size_before_the_disk_fills(hub, start):
    size = 600_000
    hub.add("org/m", "a.gguf", blob(size, 1))
    hub.add("org/m", "b.gguf", blob(size, 2))
    hub.chunk_delay = 0.05
    m = start(hub.url, max_bytes=int(size * 1.5))
    first = []
    t = threading.Thread(target=lambda: first.append(m.request("GET", "/org/m/resolve/main/a.gguf")))
    t.start()
    assert wait_for(lambda: m.mirror.downloads)
    # a's remaining bytes are reserved: b does not fit next to it and gets 507 at once, nothing is evicted
    resp, body = m.request("GET", "/org/m/resolve/main/b.gguf")
    assert resp.status == 507 and "Кэш зеркала заполнен" in json.loads(body)["error"]
    t.join(60)
    assert first[0][0].status == 200 and first[0][1] == blob(size, 1)


def test_a_file_larger_than_the_limit_evicts_nothing(hub, start):
    hub.add("org/m", "small.gguf", blob(1000))
    hub.add("org/m", "huge.gguf", blob(300_000))
    m = start(hub.url, max_bytes=100_000)
    assert m.request("GET", "/org/m/resolve/main/small.gguf")[0].status == 200
    m.settle()
    resp, body = m.request("GET", "/org/m/resolve/main/huge.gguf")
    assert resp.status == 507 and "больше лимита" in json.loads(body)["error"]
    assert (m.mirror.root / "cache" / "org" / "m" / "small.gguf").exists()


def test_a_full_disk_while_writing_is_507_in_russian(hub, start, monkeypatch):
    import errno

    hub.add("org/m", "model.gguf", blob(300_000))
    m = start(hub.url)

    def no_space(self, out, chunk):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(hf_mirror.Download, "_write", no_space)
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf")
    assert resp.status in (200, 507)
    if resp.status == 507:  # headers not sent yet
        assert "кончилось место" in json.loads(body)["error"]
    m.settle()
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf")
    assert resp.status == 507 and "кончилось место" in json.loads(body)["error"]


def test_a_reader_waits_as_long_as_the_download_makes_progress(hub, start, monkeypatch):
    monkeypatch.setattr(hf_mirror, "STALL_S", 0.5)
    data = blob(1_500_000)
    hub.add("org/m", "model.gguf", data)
    hub.chunk_delay = 0.04  # ~23 chunks: about a second until the tail, longer than STALL_S
    m = start(hub.url)
    first = []
    t = threading.Thread(target=lambda: first.append(m.request("GET", "/org/m/resolve/main/model.gguf")))
    t.start()
    assert wait_for(lambda: m.mirror.downloads)
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf", {"Range": "bytes=1400000-"})
    t.join(60)
    assert resp.status == 206 and body == data[1400000:]


def test_a_stalled_download_cuts_its_readers(hub, start, monkeypatch):
    monkeypatch.setattr(hf_mirror, "STALL_S", 0.5)
    data = blob(400_000)
    hub.add("org/m", "model.gguf", data)
    hub.stall_after, hub.stall_s = 131072, 3.0
    m = start(hub.url)
    t0 = time.monotonic()
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf")
    assert len(body) < len(data) and time.monotonic() - t0 < 2.5  # cut after STALL_S, not after the upstream pause


def test_lru_never_evicts_a_file_being_served(hub, start):
    size = 8_000_000
    for name in "ab":
        hub.add("org/m", f"{name}.gguf", blob(size, ord(name)))
    m = start(hub.url, max_bytes=int(size * 1.5))
    assert m.request("GET", "/org/m/resolve/main/a.gguf")[0].status == 200
    m.settle()
    slow = SlowClient(m.port, "/org/m/resolve/main/a.gguf")  # a is being sent
    try:
        assert wait_for(lambda: m.mirror.readers)
        resp, body = m.request("GET", "/org/m/resolve/main/b.gguf")
        assert resp.status == 507  # only a could make room, and it is in use
        assert (m.mirror.root / "cache" / "org" / "m" / "a.gguf").exists()
    finally:
        slow.close()


def test_readers_of_a_finished_download_protect_its_file(hub, start):
    size = 8_000_000
    hub.add("org/m", "a.gguf", blob(size, 1))
    m = start(hub.url, max_bytes=10**9)
    slow = SlowClient(m.port, "/org/m/resolve/main/a.gguf")  # starts the download, then reads nothing
    try:
        final = m.mirror.file_path("org/m", "a.gguf")
        assert wait_for(lambda: final.exists() and not m.mirror.downloads)
        assert m.mirror.readers.get(final)  # still streaming from the finished job
    finally:
        slow.close()


def test_every_download_gets_its_own_part_file(hub, start):
    hub.add("org/m", "model.gguf", blob(1000))
    m = start(hub.url)
    one = hf_mirror.Download(m.mirror, "org/m", "model.gguf", "main")
    two = hf_mirror.Download(m.mirror, "org/m", "model.gguf", "main")
    assert one.part != two.part


def test_escaped_dot_segments_in_api_paths_are_refused_and_never_sent_upstream(hub, start):
    """The upstream decodes %2e%2e and %2f: they must not walk out of the allowed metadata prefix."""
    hub.add("org/m", "model.gguf", blob(1000))
    m = start(hub.url)
    for path in (
        "/api/models/a/b/tree/main/%2e%2e/%2e%2e/%2e%2e/%2e%2e/whoami-v2",
        "/api/models/a/b/tree/main/%2E%2E/%2E%2E/whoami-v2",
        "/api/models/a/b/tree/main/x%2f..%2f..%2fwhoami-v2",
        "/api/models/a/b/tree/main/../../../../whoami-v2",
        "/api/models/a/b/tree/main/%2e/x",
    ):
        assert m.request("GET", path)[0].status == 403, path
    assert hub.hub_requests == []
    assert m.request("GET", "/api/models/org/m/tree/refs%2Fpr%2F1")[0].status != 403  # an escaped revision is fine


def test_only_the_metadata_endpoints_the_clients_use_are_proxied(hub, start):
    hub.add("org/m", "model.gguf", blob(1000))
    m = start(hub.url)
    for path in ("/api/models/org/m", "/api/models/org/m/refs", "/api/models/org/m/tree/main",
                 "/api/models/org/m/tree/main/sub/dir?recursive=true", "/api/models/org/m/revision/main"):
        assert m.request("GET", path)[0].status != 403, path
    for path in ("/api/whoami-v2", "/api/models/org/m/discussions", "/api/datasets/org/d", "/api/models",
                 "/api/spaces/org/s", "/api/models/org/m/commits/main"):
        resp, body = m.request("GET", path)
        assert resp.status == 403, path
    before = len(hub.hub_requests)
    m.request("GET", "/api/whoami-v2")
    assert len(hub.hub_requests) == before  # never reaches Hugging Face


def test_a_complete_part_is_finalized_on_416(hub, start):
    data = blob(50_000)
    hub.add("org/m", "model.gguf", data)
    m = start(hub.url)
    etag = hashlib.sha256(data).hexdigest()
    parts = m.mirror.parts_dir
    (parts / "left.part").write_bytes(data)  # a previous run got every byte but died before the rename
    (parts / "left.json").write_text(json.dumps(
        {"repo": "org/m", "path": "model.gguf", "size": len(data), "etag": etag, "commit": COMMIT}), encoding="utf-8")
    resp, body = m.request("GET", "/org/m/resolve/main/model.gguf")
    assert resp.status == 200 and body == data
    assert [g.get("Range") for g in hub.cdn_gets] == [f"bytes={len(data)}-"]  # no second full download


def test_abandoned_parts_count_and_are_evicted(hub, start):
    hub.add("org/m", "model.gguf", blob(300_000))
    m = start(hub.url, max_bytes=400_000)
    old = m.mirror.parts_dir / "abandoned.part"
    old.write_bytes(b"z" * 200_000)
    (m.mirror.parts_dir / "abandoned.json").write_text(json.dumps({"repo": "org/x", "path": "gone.gguf"}), encoding="utf-8")
    os.utime(old, (time.time() - 86400, time.time() - 86400))
    assert m.mirror.usage_bytes() >= 200_000
    assert m.request("GET", "/org/m/resolve/main/model.gguf")[0].status == 200
    assert not old.exists() and not (m.mirror.parts_dir / "abandoned.json").exists()


def test_a_request_pinned_to_another_commit_is_checked_at_once(hub, start):
    hub.add("org/m", "model.gguf", blob(1000, 1))
    m = start(hub.url)  # revalidation only once an hour
    assert m.request("GET", "/org/m/resolve/main/model.gguf")[1] == blob(1000, 1)
    m.settle()
    heads = lambda: sum(1 for method, _, _ in hub.hub_requests if method == "HEAD")  # noqa: E731
    # same commit as cached: served without asking
    before = heads()
    assert m.request("GET", f"/org/m/resolve/{COMMIT}/model.gguf")[1] == blob(1000, 1)
    assert heads() == before
    # another commit: checked now, and the new content is downloaded
    hub.add("org/m", "model.gguf", blob(1500, 2))
    assert m.request("GET", f"/org/m/resolve/{'d' * 40}/model.gguf")[1] == blob(1500, 2)
    assert heads() == before + 1


def test_a_cached_file_of_an_unknown_commit_is_checked_for_a_pinned_request(hub, start):
    """A deployment pinned to a commit (task 2-15) never gets a cached file nobody knows the commit of unchecked."""
    hub.add("org/m", "model.gguf", blob(1000, 1))
    m = start(hub.url)
    m.request("GET", "/org/m/resolve/main/model.gguf")
    m.settle()
    m.mirror.touch("org/m", "model.gguf", commit=None)
    pinned = "e" * 40
    hub.at_commit[("org/m", pinned, "model.gguf")] = blob(1200, 3)
    assert m.request("GET", f"/org/m/resolve/{pinned}/model.gguf")[1] == blob(1200, 3)
    m.settle()
    assert m.mirror.read_state("org/m", "model.gguf")["commit"] == pinned


def test_a_pinned_request_never_joins_a_download_of_another_revision(hub, start):
    """The cache is keyed by path: while main is being downloaded, a request for another commit gets that commit's
    bytes straight from Hugging Face, not the download of main."""
    hub.add("org/m", "model.gguf", blob(400_000, 1))
    hub.chunk_delay = 0.05  # main comes slowly through the CDN
    pinned = "f" * 40
    hub.at_commit[("org/m", pinned, "model.gguf")] = blob(1000, 4)
    m = start(hub.url)
    got = {}
    t = threading.Thread(target=lambda: got.update(main=m.request("GET", "/org/m/resolve/main/model.gguf")[1]))
    t.start()
    assert wait_for(lambda: m.mirror.downloads)
    resp, body = m.request("GET", f"/org/m/resolve/{pinned}/model.gguf")
    assert resp.status == 200 and body == blob(1000, 4)
    resp, _ = m.request("HEAD", f"/org/m/resolve/{pinned}/model.gguf")
    assert resp.getheader("X-Repo-Commit") == pinned
    t.join(30)
    assert got["main"] == blob(400_000, 1)


def test_revalidation_backs_off_while_huggingface_is_down(hub, start):
    hub.add("org/m", "model.gguf", blob(1000))
    m = start(hub.url)
    m.request("GET", "/org/m/resolve/main/model.gguf")
    m.settle()
    m.mirror.touch("org/m", "model.gguf", checked=0)
    m.close()
    offline = start(DEAD, revalidate_s=60)
    assert offline.request("GET", "/org/m/resolve/main/model.gguf")[0].status == 200
    assert time.time() - offline.mirror.read_state("org/m", "model.gguf")["checked"] < 30  # next check in an hour


def test_the_metadata_cache_is_bounded(hub, start, monkeypatch):
    monkeypatch.setattr(hf_mirror, "API_CACHE_MAX_FILES", 3)
    hub.add("org/m", "model.gguf", blob(1000))
    m = start(hub.url)
    for i in range(6):
        assert m.request("GET", f"/api/models/org/m/tree/main?cursor={i}")[0].status == 200
    assert len(list(m.mirror.api_dir.glob("*.json"))) <= 3


# ---------- own model files of the portal (task 2-14): models/local/<name>/, served as the repository local/<name>


def _own(m, name: str, files: dict[str, bytes], ready: bool = True, sha256: bool = False) -> Path:
    """Files of an own model; `ready` writes the manifest the portal writes after its checks."""
    d = m.mirror.root / "models" / "local" / name
    for p, data in files.items():
        (d / p).parent.mkdir(parents=True, exist_ok=True)
        (d / p).write_bytes(data)
    if ready:
        _manifest(d, {p: hashlib.sha256(data).hexdigest() if sha256 else None for p, data in files.items()})
    return d


def _manifest(d: Path, files: dict[str, str | None]) -> None:
    entries = {}
    for p, sha in files.items():
        st = (d / p).stat()
        entries[p] = {"size": st.st_size, "mtime": st.st_mtime, "sha256": sha}
    (d / ".manifest.json").write_text(json.dumps({"files": entries}), encoding="utf-8")


def test_local_models_are_served_from_the_portal_directory_never_from_upstream(hub, start):
    hub.add("local/own", "own-Q4_K_M.gguf", blob(1000))  # a repository of that name on Hugging Face is never asked
    m = start(hub.url)
    data = blob(50_000, 3)
    shards = {f"big-0000{i}-of-00002.gguf": blob(4000, i) for i in (1, 2)}
    _own(m, "own", {"own-Q4_K_M.gguf": data, **shards})
    refs = json.loads(m.request("GET", "/c/n1/api/models/local/own/refs")[1])
    commit = refs["branches"][0]["targetCommit"]
    tree = json.loads(m.request("GET", f"/c/n1/api/models/local/own/tree/{commit}?recursive=true")[1])
    assert sorted(i["path"] for i in tree) == ["big-00001-of-00002.gguf", "big-00002-of-00002.gguf", "own-Q4_K_M.gguf"]
    info = json.loads(m.request("GET", "/api/models/local/own/revision/main")[1])
    assert info["id"] == "local/own" and len(info["siblings"]) == 3
    resp, _ = m.request("HEAD", "/c/n1/local/own/resolve/main/own-Q4_K_M.gguf")
    assert resp.status == 200 and resp.getheader("Content-Length") == str(len(data))
    assert resp.getheader("X-Repo-Commit") == commit and resp.getheader("ETag")
    resp, body = m.request("GET", "/c/n1/local/own/resolve/main/own-Q4_K_M.gguf", {"Range": "bytes=100-"})
    assert resp.status == 206 and body == data[100:]
    resp, body = m.request("GET", "/c/n2/local/own/resolve/main/big-00002-of-00002.gguf")
    assert resp.status == 200 and body == shards["big-00002-of-00002.gguf"]
    clients = json.loads(m.request("GET", "/_mirror/transfers")[1])["clients"]
    assert clients["n1"][0]["repo"] == "local/own" and clients["n2"][0]["path"] == "big-00002-of-00002.gguf"
    assert not [r for r in hub.hub_requests if "local/" in r[1]]


def test_a_missing_local_model_or_file_is_404_without_asking_upstream(hub, start):
    hub.add("local/none", "x.gguf", blob(1000))
    m = start(hub.url)
    _own(m, "own", {"a.gguf": blob(100)})
    for method, path in (
        ("GET", "/local/own/resolve/main/missing.gguf"),
        ("HEAD", "/local/own/resolve/main/missing.gguf"),
        ("GET", "/local/none/resolve/main/x.gguf"),
        ("GET", "/local/none/resolve/main/x.gguf"),
        ("GET", "/api/models/local/none/refs"),
        ("GET", "/api/models/local/none/tree/main?recursive=true"),
    ):
        resp, _ = m.request(method, path, {"Range": "bytes=0-99"} if "x.gguf" in path else None)
        assert resp.status == 404, (method, path, resp.status)
    assert not [r for r in hub.hub_requests if "local/" in r[1]]
    assert not (m.mirror.root / "cache" / "local").exists()


def test_local_models_work_while_huggingface_is_down(start):
    m = start(DEAD)
    data = blob(20_000, 5)
    _own(m, "own", {"own.gguf": data})
    assert m.request("GET", "/api/models/local/own/refs")[0].status == 200
    resp, body = m.request("GET", "/local/own/resolve/main/own.gguf")
    assert resp.status == 200 and body == data


def test_only_files_the_portal_marked_ready_are_served(start):
    m = start(DEAD)
    d = _own(m, "own", {"a.gguf": blob(100)})
    (d / "b.gguf").write_bytes(blob(50))  # copied in after the portal's last scan
    _own(m, "fresh", {"x.gguf": blob(10)}, ready=False)  # no manifest yet
    assert m.request("GET", "/local/own/resolve/main/a.gguf")[0].status == 200
    assert m.request("GET", "/local/own/resolve/main/b.gguf")[0].status == 404
    assert m.request("GET", "/local/fresh/resolve/main/x.gguf")[0].status == 404
    assert m.request("GET", "/api/models/local/fresh/refs")[0].status == 404
    tree = json.loads(m.request("GET", "/api/models/local/own/tree/main?recursive=true")[1])
    assert [i["path"] for i in tree] == ["a.gguf"]
    (d / "a.gguf").write_bytes(blob(101))  # replaced: not served until the portal has checked it again
    assert m.request("GET", "/local/own/resolve/main/a.gguf")[0].status == 404


def test_a_link_out_of_the_models_directory_is_not_served(start, tmp_path):
    m = start(DEAD)
    secret = tmp_path / "secret.gguf"
    secret.write_bytes(b"secret" * 10)
    d = _own(m, "own", {"a.gguf": blob(10)})
    try:
        os.symlink(secret, d / "link.gguf")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges here")
    _manifest(d, {"a.gguf": None, "link.gguf": None})
    resp, body = m.request("GET", "/local/own/resolve/main/link.gguf")
    assert resp.status == 404 and b"secret" not in body


def test_a_new_version_of_an_own_file_changes_its_etag_and_the_commit(start):
    """A node's llama.cpp cache must not keep an old file of the same name: ETag and commit follow the content."""
    m = start(DEAD)
    d = _own(m, "own", {"a.gguf": blob(100, 1)}, sha256=True)
    resp, _ = m.request("HEAD", "/local/own/resolve/main/a.gguf")
    etag1, commit1 = resp.getheader("ETag"), resp.getheader("X-Repo-Commit")
    assert etag1.strip('"') == hashlib.sha256(blob(100, 1)).hexdigest()
    time.sleep(0.01)
    _own(m, "own", {"a.gguf": blob(100, 2)}, sha256=True)  # same name and size, other bytes
    resp, _ = m.request("HEAD", "/local/own/resolve/main/a.gguf")
    assert resp.getheader("ETag") != etag1 and resp.getheader("X-Repo-Commit") != commit1
    refs = json.loads(m.request("GET", "/api/models/local/own/refs")[1])
    assert refs["branches"][0]["targetCommit"] == resp.getheader("X-Repo-Commit")
    _own(m, "own", {"a.gguf": blob(100, 3)})  # without a checksum: size and mtime decide
    resp2, _ = m.request("HEAD", "/local/own/resolve/main/a.gguf")
    assert resp2.getheader("ETag") != resp.getheader("ETag") and resp2.getheader("X-Repo-Commit") != commit1


def test_local_models_are_never_evicted_and_stay_outside_the_limit(hub, start):
    size = 100_000
    for name in "ab":
        hub.add("org/m", f"{name}.gguf", blob(size, ord(name)))
    m = start(hub.url, max_bytes=int(size * 1.5))
    own = _own(m, "own", {"huge.gguf": blob(size * 5, 9)})
    for name in "ab":
        assert m.request("GET", f"/org/m/resolve/main/{name}.gguf")[0].status == 200
        m.settle()
    assert m.mirror.usage_bytes() == size  # a was evicted for b; the own model is not under the limit
    assert (own / "huge.gguf").stat().st_size == size * 5
    assert m.request("GET", "/local/own/resolve/main/huge.gguf")[0].status == 200


def test_the_disk_reserve_stays_above_the_kubelet_eviction_threshold(tmp_path, monkeypatch):
    """kubelet on llm-main evicts its pods (Postgres, LiteLLM, the mirror) below 5 % free on the volume: the mirror keeps
    at least 15 % of the disk free, never less than DISK_RESERVE."""
    GiB = 1 << 30
    monkeypatch.setattr(hf_mirror, "disk_reserve", DISK_RESERVE_OF)
    m = hf_mirror.Mirror(root=tmp_path / "data", upstream=DEAD, max_bytes=10**15)
    usage = lambda total, free: lambda _p: shutil._ntuple_diskusage(total, total - free, free)  # noqa: E731
    monkeypatch.setattr(hf_mirror.shutil, "disk_usage", usage(1000 * GiB, 200 * GiB))
    assert m.make_room(51 * GiB) == "full"  # 150 GiB stay free, 50 GiB usable
    assert m.make_room(50 * GiB) == "ok"
    monkeypatch.setattr(hf_mirror.shutil, "disk_usage", usage(4 * GiB, 2 * GiB))  # a tiny disk: the 1 GiB floor
    assert m.make_room(GiB + 1) == "full" and m.make_room(GiB) == "ok"
    assert DISK_RESERVE_OF(1000 * GiB) == 150 * GiB and DISK_RESERVE_OF(GiB) == hf_mirror.DISK_RESERVE


def test_the_panel_limit_wins_over_the_env_limit():
    """The portal's setting `hf_mirror_max_gib` reaches the mirror as HF_MIRROR_MAX_GIB_OVERRIDE (a ConfigMap, read at
    start); an empty or absent one leaves the .env value."""
    assert hf_mirror.max_gib_setting({}) == 500
    assert hf_mirror.max_gib_setting({"HF_MIRROR_MAX_GIB": "2000"}) == 2000
    assert hf_mirror.max_gib_setting({"HF_MIRROR_MAX_GIB": "2000", "HF_MIRROR_MAX_GIB_OVERRIDE": ""}) == 2000
    assert hf_mirror.max_gib_setting({"HF_MIRROR_MAX_GIB": "2000", "HF_MIRROR_MAX_GIB_OVERRIDE": "800"}) == 800
    with pytest.raises(ValueError):  # main() turns it into «bad setting» and exit code 2
        hf_mirror.max_gib_setting({"HF_MIRROR_MAX_GIB_OVERRIDE": "lots"})
