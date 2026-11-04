import asyncio
import json
import os
import re
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

import fake_backends
import prepare
import report
import run

BASE = "http://gw.test"
CHAT = f"{BASE}/v1/chat/completions"


def sse(*events) -> bytes:
    out = b""
    for ev in events:
        out += b"data: " + (ev if isinstance(ev, str) else json.dumps(ev)).encode() + b"\n\n"
    return out


def chunk(content: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}


class SlowStream(httpx.AsyncByteStream):
    """Sends the parts one by one with a pause before each, like a model generating tokens."""

    def __init__(self, parts, pause):
        self.parts, self.pause = parts, pause

    async def __aiter__(self):
        for part in self.parts:
            await asyncio.sleep(self.pause)
            yield part


def config(keys, **over) -> run.Config:
    values = dict(
        base=BASE, keys=keys, model="lt-fake", users=len(keys), ramp=0.0, duration=0.5,
        prompt_tokens=8, max_tokens=4, stream=True, think_min=0.05, think_max=0.05,
        request_timeout=5.0, portal_cookie=None, portal_rps=0.0, seed=1, shard_index=0, shard_count=1,
    )
    values.update(over)
    return run.Config(**values)


def read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def chat_records(records):
    return [r for r in records if r["kind"] == "chat"]


def by_user(records, users):
    return {u: [r for r in records if r["kind"] == "chat" and r["user"] == u] for u in range(users)}


# ---------------------------------------------------------------- run.py


@respx.mock
def test_run_counts_statuses_and_ttft(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        if key == "k-429":
            return httpx.Response(
                429, headers={"Retry-After": "7"}, json={"error": {"message": "Слишком много запросов, повторите позже"}}
            )
        if key == "k-500":
            return httpx.Response(502, text="bad gateway")
        parts = [
            sse(chunk("a")), sse(chunk("b")),
            sse({"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}}, "[DONE]"),
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SlowStream(parts, 0.06))

    route = respx.post(CHAT).mock(side_effect=handler)
    out = tmp_path / "r.jsonl"
    summary = asyncio.run(run.run_load(config(["k-ok", "k-429", "k-500"]), out))
    records = read_records(out)
    per_user = by_user(records, 3)

    assert route.called and summary["requests"] == len(chat_records(records))
    assert len(per_user[0]) >= 2 and len(per_user[1]) >= 2 and len(per_user[2]) >= 2
    for r in per_user[0]:
        assert (r["status"], r["error"], r["tokens"]) == (200, None, 2)
        assert 0.055 <= r["ttft"] < r["latency"]  # first SSE data chunk, not the response headers
        assert r["latency"] >= 0.17  # three pauses of 60 ms
    for r in per_user[1]:
        assert (r["status"], r["error"], r["retry_after"]) == (429, "http", "7")
        assert "Слишком много запросов" in r["msg"] and r["ttft"] is None
    for r in per_user[2]:
        assert (r["status"], r["error"]) == (502, "http")
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "lt-fake" and sent["stream"] is True and sent["max_tokens"] == 4
    assert len(sent["messages"][0]["content"].split()) == 8


@respx.mock
def test_run_classifies_transport_errors_and_incomplete_stream(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        if key == "k-timeout":
            raise httpx.ReadTimeout("read timed out")
        if key == "k-connect":
            raise httpx.ConnectError("refused")
        if key == "k-proto":
            raise httpx.RemoteProtocolError("peer closed connection")
        if key == "k-pool":
            raise httpx.PoolTimeout("no free connection")
        if key == "k-hang":
            return httpx.Response(200, stream=SlowStream([sse(chunk("a"))], 30))
        if key == "k-cut":  # 200 and tokens, but no [DONE]
            return httpx.Response(200, stream=SlowStream([sse(chunk("a"))], 0.01))
        # in-band error after the stream started
        parts = [sse(chunk("a")), sse({"error": {"message": "модель упала"}})]
        return httpx.Response(200, stream=SlowStream(parts, 0.01))

    respx.post(CHAT).mock(side_effect=handler)
    keys = ["k-timeout", "k-connect", "k-proto", "k-pool", "k-hang", "k-cut", "k-inband"]
    out = tmp_path / "r.jsonl"
    asyncio.run(run.run_load(config(keys, duration=0.3, request_timeout=0.4), out))
    per_user = by_user(read_records(out), len(keys))
    expect = ["timeout", "connect", "protocol", "pool", "timeout", "protocol", "stream_error"]
    for user, error in enumerate(expect):
        assert per_user[user], keys[user]
        assert {r["error"] for r in per_user[user]} == {error}, keys[user]
    assert all(r["status"] == 0 for u in range(4) for r in per_user[u])
    assert "модель упала" in per_user[6][0]["msg"]


@respx.mock
def test_run_non_stream_and_portal_traffic(tmp_path):
    respx.post(CHAT).mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": "x"}}], "usage": {"completion_tokens": 5}}))
    me = respx.get(f"{BASE}/api/me").mock(return_value=httpx.Response(200, json={}))
    models = respx.get(f"{BASE}/api/models").mock(return_value=httpx.Response(200, json=[]))
    keys = respx.get(f"{BASE}/api/keys").mock(return_value=httpx.Response(401, json={"detail": "x"}))
    out = tmp_path / "r.jsonl"
    cfg = config(["k"], stream=False, duration=0.6, portal_cookie="sess-1", portal_rps=30.0)
    asyncio.run(run.run_load(cfg, out))
    records = read_records(out)
    chat = [r for r in records if r["kind"] == "chat"]
    assert chat and all(r["tokens"] == 5 and r["ttft"] is None and r["status"] == 200 for r in chat)
    assert me.called and models.called and keys.called
    assert me.calls[0].request.headers["cookie"] == "nsullm_session=sess-1"
    assert "authorization" not in me.calls[0].request.headers
    portal = {r["kind"]: r for r in records if r["kind"].startswith("portal")}
    assert set(portal) == {"portal:/api/me", "portal:/api/models", "portal:/api/keys"}
    assert portal["portal:/api/keys"]["status"] == 401 and portal["portal:/api/me"]["status"] == 200


@respx.mock
def test_run_ramps_users_and_shares_keys(tmp_path):
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"usage": {"completion_tokens": 1}})

    respx.post(CHAT).mock(side_effect=handler)
    out = tmp_path / "r.jsonl"
    asyncio.run(run.run_load(config(["ka", "kb"], users=5, ramp=0.4, duration=0.2, stream=False), out))
    records = read_records(out)
    first = {u: min(r["start"] for r in rs) for u, rs in by_user(records, 5).items() if rs}
    assert set(first) == {0, 1, 2, 3, 4}
    assert first[4] - first[0] >= 0.25  # staggered over the ramp (user 4 starts at 0.8 * 0.4 s)
    assert set(seen) == {"Bearer ka", "Bearer kb"}


def test_stream_connections_are_reused_after_done(tmp_path):
    """Reading past [DONE] to the chunked terminator returns the socket to the pool: one user, one TCP connection."""
    accepted: list[int] = []

    async def handle(reader, writer):
        accepted.append(1)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = re.search(rb"content-length: (\d+)", head, re.I)
                await reader.readexactly(int(length.group(1)) if length else 0)
                parts = [sse(chunk("a")), sse({"choices": [], "usage": {"completion_tokens": 1}}, "[DONE]")]
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ntransfer-encoding: chunked\r\n\r\n")
                for part in parts:
                    writer.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                    await writer.drain()
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    out = tmp_path / "r.jsonl"

    async def go():
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            cfg = config(["k"], base=f"http://127.0.0.1:{port}", duration=1.0, think_min=0.02, think_max=0.02)
            await run.run_load(cfg, out)
        finally:
            server.close()

    asyncio.run(go())
    records = chat_records(read_records(out))
    assert len(records) >= 10 and all(r["error"] is None and r["tokens"] == 1 for r in records)
    assert len(accepted) == 1, f"{len(records)} requests opened {len(accepted)} connections"


@respx.mock
def test_run_records_event_loop_lag_when_the_generator_is_blocked(tmp_path):
    def handler(request):
        time.sleep(0.4)  # blocks the event loop like a saturated load generator would
        return httpx.Response(200, json={"usage": {"completion_tokens": 1}})

    respx.post(CHAT).mock(side_effect=handler)
    out = tmp_path / "r.jsonl"
    summary = asyncio.run(run.run_load(config(["k"], stream=False, duration=0.8), out, lag_every=0.05))
    lags = [r["lag"] for r in read_records(out) if r["kind"] == "loop_lag"]
    assert len(lags) >= 3 and max(lags) >= 0.25
    assert summary["max_loop_lag_s"] == pytest.approx(max(lags))
    assert summary["requests"] == len(chat_records(read_records(out)))  # lag samples are not requests


@respx.mock
def test_run_shard_runs_only_its_users_and_share_of_portal_rate(tmp_path):
    respx.post(CHAT).mock(return_value=httpx.Response(200, json={"usage": {"completion_tokens": 1}}))
    me = respx.get(f"{BASE}/api/me").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE}/api/models").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/api/keys").mock(return_value=httpx.Response(200, json=[]))
    cfg = config(["ka", "kb", "kc"], users=10, stream=False, duration=1.0, portal_cookie="c", portal_rps=30.0,
                 shard_index=1, shard_count=3)
    assert run.user_indices(cfg) == [1, 4, 7]
    assert run.portal_rate(cfg) == pytest.approx(10.0)
    assert run.connection_limit(cfg) < run.connection_limit(config(["k"], users=10, portal_rps=30.0))
    out = tmp_path / "r.jsonl"
    asyncio.run(run.run_load(cfg, out))
    records = read_records(out)
    assert {r["user"] for r in chat_records(records)} == {1, 4, 7}
    portal = [r for r in records if r["kind"].startswith("portal")]
    assert 3 <= len(portal) <= 16  # about 10 per second for 1 s, not the whole 30
    assert me.called


def test_cli_shard_option(tmp_path):
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": [{"user_id": "loadtest-0", "key": "sk-a", "token": "t"}]}))
    base = ["--base", "https://x.test", "--keys", str(keys), "--model", "m", "--out", str(tmp_path / "o")]
    cfg, _ = run.parse_args(base + ["--shard", "2/4"])
    assert (cfg.shard_index, cfg.shard_count) == (1, 4)
    cfg, _ = run.parse_args(base)
    assert (cfg.shard_index, cfg.shard_count) == (0, 1)
    for bad in ("0/4", "5/4", "x", "1/0"):
        with pytest.raises(SystemExit):
            run.parse_args(base + ["--shard", bad])


def test_resolve_transport_dials_ip_and_keeps_name():
    assert run.parse_resolve(["Host.Example:10.0.0.5"]) == {"host.example": "10.0.0.5"}
    with pytest.raises(ValueError):
        run.parse_resolve(["nonsense"])

    captured: list[httpx.Request] = []

    class Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            captured.append(request)
            return httpx.Response(200, request=request)

    async def go():
        transport = run.AsyncResolveTransport({"llm.nsu": "10.0.0.5"}, Inner())
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://llm.nsu/v1/models")
            await client.get("https://other.nsu/x")

    asyncio.run(go())
    assert captured[0].url.host == "10.0.0.5" and captured[0].headers["host"] == "llm.nsu"
    assert captured[0].extensions["sni_hostname"] == "llm.nsu"
    assert captured[1].url.host == "other.nsu"


def test_cli_loads_keys_and_sizes_connection_pool(tmp_path):
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": [{"user_id": "loadtest-0", "key": "sk-a", "token": "t"}]}))
    cfg, args = run.parse_args(
        ["--base", "https://x.test/", "--keys", str(keys), "--model", "m", "--users", "1000", "--ramp", "60",
         "--duration", "300", "--out", str(tmp_path / "o.jsonl"), "--resolve", "x.test:1.2.3.4"])
    assert cfg.base == "https://x.test" and cfg.keys == ["sk-a"] and cfg.users == 1000 and cfg.stream is False
    assert args.resolve == ["x.test:1.2.3.4"]
    assert run.connection_limit(cfg) >= 1100  # one pooled connection per user plus headroom


# ---------------------------------------------------------------- report.py


def rec(start, latency, status=200, error=None, ttft=None, tokens=None, kind="chat", **extra):
    return {"kind": kind, "user": 0, "start": start, "end": start + latency, "latency": latency, "ttft": ttft,
            "status": status, "error": error, "tokens": tokens, "retry_after": None, "msg": None, **extra}


def test_report_percentiles(tmp_path):
    assert report.percentile([], 50) is None
    assert report.percentile([5.0], 99) == 5.0
    data = [float(i) for i in range(1, 101)]
    assert (report.percentile(data, 50), report.percentile(data, 95), report.percentile(data, 99)) == (50.0, 95.0, 99.0)
    assert report.percentile([3.0, 1.0, 2.0], 50) == 2.0  # input order does not matter

    records = [rec(float(i), 0.5, ttft=i / 100, tokens=10) for i in range(1, 101)]
    records += [rec(200.0, 1.0, status=429, error="http", retry_after="5", msg="Слишком много запросов")] * 3
    records += [rec(201.0, 1.0, status=500, error="http", msg="boom")]
    records += [rec(202.0, 30.0, status=0, error="timeout", msg="ReadTimeout")]
    records += [rec(203.0, 0.1, status=0, error="connect", msg="refused")]
    records += [rec(1.0, 0.1, kind="portal:/api/me")]
    summary = report.summarize(records)
    chat = summary["chat"]
    assert chat["total"] == 106 and chat["ok"] == 100 and chat["status_429"] == 3
    assert chat["status_5xx"] == 1 and chat["timeouts"] == 1 and chat["connect_errors"] == 1
    assert chat["429_with_retry_after"] == 3
    assert chat["ttft"]["p50"] == pytest.approx(0.50) and chat["ttft"]["p95"] == pytest.approx(0.95)
    assert chat["ttft"]["p99"] == pytest.approx(0.99)
    assert chat["latency"]["p50"] == 0.5  # latency percentiles describe successful requests only
    assert summary["portal"]["total"] == 1

    jsonl = tmp_path / "r.jsonl"
    jsonl.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    md = tmp_path / "out.md"
    assert report.main([str(jsonl), "--out", str(md)]) == 0
    text = md.read_text(encoding="utf-8")
    for needle in ("p50", "p95", "p99", "429", "Слишком много запросов", "boom", "ReadTimeout"):
        assert needle in text


def test_report_throughput_and_max_concurrency():
    records = [
        rec(0.0, 10.0, ttft=1, tokens=100),  # 0..10
        rec(2.0, 10.0, ttft=1, tokens=100),  # 2..12
        rec(5.0, 1.0, status=429, error="http"),  # 5..6 counts as in flight too
        rec(11.0, 1.0, ttft=1, tokens=50),  # 11..12
    ]
    summary = report.summarize(records)
    assert summary["max_concurrency"] == 3
    assert summary["duration_s"] == pytest.approx(12.0)
    assert summary["chat"]["tokens"] == 250
    assert summary["chat"]["tokens_per_s"] == pytest.approx(250 / 12.0)
    assert report.max_concurrency([]) == 0


def lag(t, value):
    return {"kind": "loop_lag", "start": t, "end": t, "lag": value}


def test_report_marks_generator_overload_by_loop_lag(tmp_path):
    base = [rec(float(i), 0.5, ttft=0.1, tokens=10) for i in range(20)]
    calm = report.summarize(base + [lag(float(i), 0.01) for i in range(40)])
    assert calm["loop_lag"]["overloaded"] is False and calm["loop_lag"]["samples"] == 40
    assert calm["requests"] == 20 and calm["chat"]["total"] == 20  # lag samples are not requests
    assert calm["duration_s"] == pytest.approx(19.5)

    hot = report.summarize(base + [lag(float(i), 0.05) for i in range(10)] + [lag(30.0 + i, 0.6) for i in range(10)])
    assert hot["loop_lag"]["p95"] == pytest.approx(0.6) and hot["loop_lag"]["max"] == pytest.approx(0.6)
    assert hot["loop_lag"]["overloaded"] is True

    for name, summary, expected in (("calm", calm, False), ("hot", hot, True)):
        text = report.render(summary, name)
        assert ("генератор перегружен" in text) is expected, name
        assert "Лаг цикла событий" in text
    assert report.summarize(base)["loop_lag"] is None  # old results without samples still work


def test_report_merges_shard_files(tmp_path):
    a = [rec(0.0, 1.0, ttft=0.1, tokens=10), lag(0.5, 0.01)]
    b = [rec(0.5, 1.0, ttft=0.2, tokens=10), rec(0.6, 1.0, status=429, error="http", retry_after="3")]
    paths = []
    for name, records in (("a", a), ("b", b)):
        path = tmp_path / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        paths.append(str(path))
    out = tmp_path / "merged.md"
    assert report.main([*paths, "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "a.jsonl" in text and "b.jsonl" in text
    merged = report.summarize(report.load_records(paths))
    assert merged["chat"]["total"] == 3 and merged["max_concurrency"] == 3


# ---------------------------------------------------------------- prepare.py


class FakeLiteLLM:
    """Just enough of the LiteLLM admin API for prepare.py; records every call."""

    def __init__(self):
        self.keys = []  # dicts: token, key_alias, user_id, metadata
        self.users = []  # dicts: user_id, metadata
        self.models = []  # LiteLLM /model/info items
        self.calls: list[tuple[str, str, dict]] = []
        self.auth_seen: set[str] = set()
        self.server = None

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self, method):
                outer.auth_seen.add(self.headers.get("authorization", ""))
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                outer.calls.append((method, url.path, body))
                status, payload = outer.route(url.path, query, body)
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _page(items, query):
        size, page = int(query.get("size") or query.get("page_size") or 100), int(query.get("page", 1))
        total_pages = max((len(items) + size - 1) // size, 1)
        return items[(page - 1) * size: page * size], page, total_pages

    def route(self, path, query, body):
        if path == "/key/list":
            assert query.get("return_full_object") == "true"
            items, page, pages = self._page(self.keys, query)
            return 200, {"keys": items, "current_page": page, "total_pages": pages, "total_count": len(self.keys)}
        if path == "/user/list":
            items, page, pages = self._page(self.users, query)
            return 200, {"users": items, "page": page, "total_pages": pages, "total": len(self.users)}
        if path == "/key/delete":
            gone = set(body["keys"])
            self.keys = [k for k in self.keys if k["token"] not in gone]
            return 200, {"deleted_keys": list(gone)}
        if path == "/user/delete":
            gone = set(body["user_ids"])
            self.users = [u for u in self.users if u["user_id"] not in gone]
            return 200, {"deleted": list(gone)}
        if path == "/user/new":
            self.users.append({"user_id": body["user_id"], "metadata": body.get("metadata")})
            return 200, {"user_id": body["user_id"]}
        if path == "/key/generate":
            n = len(self.keys)
            self.keys.append({"token": f"hash-{n}", "key_alias": body["key_alias"], "user_id": body["user_id"],
                              "metadata": body.get("metadata")})
            return 200, {"key": f"sk-secret-{n}", "token": f"hash-{n}"}
        if path == "/model/info":
            return 200, {"data": self.models}
        if path == "/model/new":
            self.models.append({"model_name": body["model_name"], "litellm_params": body["litellm_params"],
                                "model_info": body["model_info"]})
            return 200, {}
        if path == "/model/delete":
            self.models = [m for m in self.models if m["model_info"]["id"] != body["id"]]
            return 200, {}
        return 404, {"detail": path}


@pytest.fixture
def litellm():
    fake = FakeLiteLLM()
    fake.url = fake.start()
    yield fake
    fake.stop()


MASTER = {"LITELLM_MASTER_KEY": "sk-master"}


def test_prepare_cleanup_only_loadtest_keys(litellm, capsys):
    litellm.keys = [
        {"token": "h-user", "key_alias": "ivan-main", "user_id": "u-1", "metadata": {"source": "portal"}},
        {"token": "h-none", "key_alias": "no-meta", "user_id": "u-2", "metadata": None},
        {"token": "h-alias", "key_alias": "loadtest-lookalike", "user_id": "u-3", "metadata": {"loadtest": False}},
        {"token": "h-meta-only", "key_alias": "someones-key", "user_id": "u-4", "metadata": {"loadtest": True}},
        {"token": "h-lt-0", "key_alias": "loadtest-0", "user_id": "loadtest-0", "metadata": {"loadtest": True}},
        {"token": "h-lt-1", "key_alias": "loadtest-1", "user_id": "loadtest-1", "metadata": {"loadtest": True}},
        {"token": "h-lt-2", "key_alias": "loadtest-2", "user_id": "loadtest-2", "metadata": {"loadtest": True}},
    ]
    litellm.users = [
        {"user_id": "ivan", "metadata": {}},
        {"user_id": "loadtest-lookalike", "metadata": None},
        {"user_id": "loadtest-flag-false", "metadata": {"loadtest": False}},
        {"user_id": "other-flagged", "metadata": {"loadtest": True}},
        {"user_id": "loadtest-0", "metadata": {"loadtest": True}},
        {"user_id": "loadtest-1", "metadata": {"loadtest": True}},
        {"user_id": "loadtest-2", "metadata": {"loadtest": True}},
    ]
    litellm.models = [
        {"model_name": "gemma", "model_info": {"id": "pm-1", "portal_managed": True}},
        {"model_name": "lt-fake", "model_info": {"id": "lt-1", "loadtest": True}},
    ]
    rc = prepare.main(["cleanup", "--base", litellm.url, "--page-size", "2"], env=MASTER)
    assert rc == 0
    assert {k["token"] for k in litellm.keys} == {"h-user", "h-none", "h-alias", "h-meta-only"}
    assert {u["user_id"] for u in litellm.users} == {
        "ivan", "loadtest-lookalike", "loadtest-flag-false", "other-flagged"}
    assert [m["model_info"]["id"] for m in litellm.models] == ["pm-1"]
    assert litellm.auth_seen == {"Bearer sk-master"}
    out = capsys.readouterr()
    assert "sk-master" not in out.out + out.err


def test_prepare_create_writes_keys_file_and_prints_no_secrets(litellm, tmp_path, capsys):
    env_file = tmp_path / "secrets.env"
    env_file.write_text("POSTGRES_PASSWORD=pg\nLITELLM_MASTER_KEY=sk-master\n")
    out_file = tmp_path / "keys.json"
    rc = prepare.main(
        ["create", "--base", litellm.url, "--users", "5", "--rpm", "900", "--out", str(out_file),
         "--env-file", str(env_file)], env={})
    assert rc == 0
    data = json.loads(out_file.read_text())
    assert [k["user_id"] for k in data["keys"]] == [f"loadtest-{i}" for i in range(5)]
    assert all(k["key"].startswith("sk-secret-") for k in data["keys"])
    assert {u["user_id"] for u in litellm.users} == {f"loadtest-{i}" for i in range(5)}
    gen = [body for _, path, body in litellm.calls if path == "/key/generate"]
    assert len(gen) == 5 and all(b["metadata"] == {"loadtest": True} and b["rpm_limit"] == 900 for b in gen)
    assert all(b["models"] == [] and b["user_id"] == b["key_alias"] for b in gen)
    assert all(b["metadata"] == {"loadtest": True} for _, p, b in litellm.calls if p == "/user/new")
    if os.name == "posix":
        assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    shown = capsys.readouterr()
    assert "sk-master" not in shown.out + shown.err and "sk-secret" not in shown.out + shown.err
    assert "5" in shown.out


def test_prepare_requires_master_key(tmp_path, capsys):
    rc = prepare.main(["cleanup", "--env-file", str(tmp_path / "missing.env")], env={})
    assert rc != 0
    assert "LITELLM_MASTER_KEY" in capsys.readouterr().err


def test_prepare_register_fake_backends_and_cleanup(litellm):
    urls = ["http://10.0.0.5:28100/v1", "http://10.0.0.5:28101/v1"]
    rc = prepare.main(["register-backends", "--base", litellm.url, "--model", "lt-fake",
                       "--api-base", urls[0], "--api-base", urls[1]], env=MASTER)
    assert rc == 0
    assert [m["litellm_params"]["api_base"] for m in litellm.models] == urls
    assert all(m["model_name"] == "lt-fake" and m["model_info"].get("loadtest") is True for m in litellm.models)
    assert all(not m["model_info"].get("portal_managed") for m in litellm.models)
    assert len({m["model_info"]["id"] for m in litellm.models}) == 2
    prepare.main(["cleanup", "--base", litellm.url], env=MASTER)
    assert litellm.models == []


# ---------------------------------------------------------------- fake_backends.py


def test_fake_backends_commands():
    cmds = fake_backends.build_commands(count=3, port_base=28100, host="0.0.0.0", delay_ms=50, model_id="lt-fake",
                                        fill_max_tokens=True)
    assert [c[c.index("--port") + 1] for c in cmds] == ["28100", "28101", "28102"]
    assert all(Path(c[1]).name == "openai_backend.py" and Path(c[1]).exists() for c in cmds)
    assert all("--delay-ms" in c and c[c.index("--delay-ms") + 1] == "50" for c in cmds)
    assert all("--fill-max-tokens" in c and c[c.index("--host") + 1] == "0.0.0.0" for c in cmds)


def test_pause_after_honours_retry_after_only_when_asked():
    import random

    rng = random.Random(1)
    on = config(["k"], think_min=1.0, think_max=3.0, honor_retry_after=True)
    off = config(["k"], think_min=1.0, think_max=3.0)
    limited = {"status": 429, "retry_after": "5"}
    pauses = [run.pause_after(limited, on, rng) for _ in range(200)]
    assert 4.0 <= min(pauses) and max(pauses) <= 6.0  # Retry-After with +-20 % jitter
    assert len({round(p, 3) for p in pauses}) > 50  # jittered, not one synchronized wave
    assert all(1.0 <= run.pause_after(limited, off, rng) <= 3.0 for _ in range(50))  # default: the usual think time
    for state in ({"status": 200, "retry_after": None}, {"status": 429, "retry_after": None},
                  {"status": 429, "retry_after": "soon"}, {"status": 503, "retry_after": "5"}):
        assert 1.0 <= run.pause_after(state, on, rng) <= 3.0


@respx.mock
def test_run_waits_retry_after_between_429s(tmp_path):
    respx.post(CHAT).mock(return_value=httpx.Response(429, headers={"Retry-After": "0.4"}, json={"error": {"message": "x"}}))
    out = tmp_path / "r.jsonl"
    asyncio.run(run.run_load(config(["k"], users=1, duration=1.0, stream=False, honor_retry_after=True), out))
    starts = sorted(r["start"] for r in chat_records(read_records(out)))
    assert 2 <= len(starts) <= 4  # about one request per 0.4 s (+-20 %), not one per 0.05 s of think time
    assert min(b - a for a, b in zip(starts, starts[1:])) >= 0.3


def test_cli_honor_retry_after_flag(tmp_path):
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": [{"key": "sk-1"}]}), encoding="utf-8")
    base = ["--base", BASE, "--keys", str(keys), "--model", "m", "--out", str(tmp_path / "o.jsonl")]
    assert run.parse_args(base)[0].honor_retry_after is False
    assert run.parse_args(base + ["--honor-retry-after"])[0].honor_retry_after is True
