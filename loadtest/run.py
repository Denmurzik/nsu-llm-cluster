#!/usr/bin/env python3
"""Load generator: N virtual users against the gateway (and optionally the portal) from one asyncio process.

Each virtual user loops: chat completion (streaming or not) -> think time. All users share one
httpx.AsyncClient (HTTP/1.1 keep-alive, one pooled connection per user), so 1000 concurrent streams
need 1000 sockets and no threads. One JSON line per request goes to --out; `report.py` summarises it.

Record fields: kind ("chat" | "portal:<path>"), user, start/end (epoch s), latency (s), ttft (s to the
first SSE data chunk, streaming chat only), status (HTTP status, 0 when none arrived), error (None |
"http" | "timeout" | "connect" | "protocol" | "pool" | "stream_error" | "other" | "client_overload"),
tokens (completion tokens), retry_after (429 header), msg (short error text).
Besides requests the file holds `{"kind": "loop_lag", "lag": s}` samples of the generator's own event-loop lag.
"""

import argparse
import asyncio
import contextlib
import json
import random
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

COOKIE_NAME = "nsullm_session"
PORTAL_PATHS = ("/api/me", "/api/models", "/api/keys")
MAX_PORTAL_IN_FLIGHT = 256
CONNECTION_HEADROOM = 100
WORDS = ("alpha beta gamma delta river stone cloud forest light table window garden bridge market planet silver "
         "orange winter summer engine paper violin mountain ocean harbor castle meadow thunder candle ribbon").split()


@dataclass
class Config:
    base: str
    keys: list[str]
    model: str
    users: int = 1000
    ramp: float = 60.0  # users start evenly spread over this many seconds
    duration: float = 300.0  # seconds at full load after the ramp; in-flight requests are awaited
    prompt_tokens: int = 64  # approximated by that many one-token words
    max_tokens: int = 64
    stream: bool = False
    think_min: float = 1.0
    think_max: float = 3.0
    request_timeout: float = 120.0  # hard limit for one request, queueing included
    honor_retry_after: bool = False  # after a 429 wait Retry-After (+-20 %) instead of the think time, like the OpenAI SDK
    portal_cookie: str | None = None
    portal_rps: float = 0.0  # portal requests per second, all users together (split between shards)
    seed: int = 0
    shard_index: int = 0  # this process runs users index, index + count, ... of --users
    shard_count: int = 1


# ------------------------------------------------------------------ transport


def parse_resolve(items) -> dict[str, str]:
    """['host:ip', ...] -> {host: ip}."""
    mapping = {}
    for item in items or []:
        host, sep, ip = item.partition(":")
        if not sep or not host or not ip:
            raise ValueError(f"--resolve ожидает host:ip, получено {item!r}")
        mapping[host.lower()] = ip
    return mapping


class AsyncResolveTransport(httpx.AsyncBaseTransport):
    """Dials a fixed IP for selected hosts while Host header and TLS SNI/verification keep the name."""

    def __init__(self, mapping: dict[str, str], inner: httpx.AsyncBaseTransport):
        self.mapping = mapping
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        ip = self.mapping.get(request.url.host.lower())
        if ip is None:
            return await self.inner.handle_async_request(request)
        dialed = httpx.Request(
            request.method,
            request.url.copy_with(host=ip),
            headers=request.headers,  # Host stays the real name
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        return await self.inner.handle_async_request(dialed)

    async def aclose(self) -> None:
        await self.inner.aclose()


def user_indices(cfg: Config) -> list[int]:
    """Global indices of the virtual users this shard runs (keys and ramp offsets use the global index)."""
    return list(range(cfg.shard_index, cfg.users, cfg.shard_count))


def portal_rate(cfg: Config) -> float:
    return cfg.portal_rps / cfg.shard_count


def connection_limit(cfg: Config) -> int:
    return len(user_indices(cfg)) + (MAX_PORTAL_IN_FLIGHT if cfg.portal_rps > 0 else 0) + CONNECTION_HEADROOM


def make_transport(cfg: Config, resolve=(), cacert=None) -> httpx.AsyncBaseTransport:
    limit = connection_limit(cfg)
    limits = httpx.Limits(max_connections=limit, max_keepalive_connections=limit, keepalive_expiry=60.0)
    verify = ssl.create_default_context(cafile=cacert) if cacert else True
    inner = httpx.AsyncHTTPTransport(verify=verify, limits=limits, http1=True, http2=False)
    mapping = parse_resolve(resolve)
    return AsyncResolveTransport(mapping, inner) if mapping else inner


def raise_file_limit(needed: int) -> None:
    """Every connection is a file descriptor; lift the soft limit when the platform allows it."""
    try:
        import resource
    except ImportError:  # Windows has no RLIMIT_NOFILE
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = needed + 200
    if soft >= wanted:
        return
    target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < wanted:
        print(f"Предупреждение: лимит открытых файлов {soft} ниже нужных {wanted}; выполните `ulimit -n {wanted}`.",
              file=sys.stderr)


# ------------------------------------------------------------------ requests


def classify_exception(exc: BaseException) -> str:
    if isinstance(exc, httpx.PoolTimeout):
        return "pool"  # client side: no free connection, the limit is too low
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "connect"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(exc, (httpx.ProtocolError, httpx.ReadError, httpx.WriteError, httpx.CloseError)):
        return "protocol"
    return "other"


def build_prompt(n_words: int, rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(max(n_words, 1)))


def short_error(body: bytes | str) -> str:
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip()[:200]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:200]
        if isinstance(err, str):
            return err[:200]
        if data.get("detail"):
            return str(data["detail"])[:200]
    return text.strip()[:200]


class Recorder:
    def __init__(self, path: Path):
        self.file = open(path, "w", encoding="utf-8", buffering=1 << 16)
        self.total = self.ok = self.rejected = self.failed = 0
        self.in_flight = 0
        self.max_lag = 0.0

    def write_lag(self, lag: float) -> None:
        """Event-loop lag sample (not a request, not counted in totals)."""
        now = time.time()
        self.max_lag = max(self.max_lag, lag)
        self.file.write(json.dumps({"kind": "loop_lag", "start": now, "end": now, "lag": lag}) + "\n")

    def write(self, **rec) -> None:
        self.file.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.total += 1
        status, error = rec["status"], rec["error"]
        if error is None and 200 <= status < 300:
            self.ok += 1
        elif status == 429:
            self.rejected += 1
        else:
            self.failed += 1

    def close(self) -> None:
        self.file.close()


async def timed_request(rec: Recorder, kind: str, user: int, send) -> dict:
    """Runs `send(state)` under the request timeout and writes one record, whatever happens.

    `send` fills state: status, ttft, tokens, retry_after, msg, error (error set only for in-band failures).
    """
    state = {"status": 0, "ttft": None, "tokens": None, "retry_after": None, "msg": None, "error": None}
    started_epoch, started = time.time(), time.perf_counter()
    rec.in_flight += 1
    try:
        await send(state)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is a data point
        state["error"] = classify_exception(exc)
        state["msg"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        rec.in_flight -= 1
    rec.write(kind=kind, user=user, start=started_epoch, end=time.time(), latency=time.perf_counter() - started, **state)
    return state


def pause_after(state: dict, cfg: Config, rng: random.Random) -> float:
    """Seconds before the user's next request: Retry-After of a 429 (+-20 % jitter) with --honor-retry-after,
    otherwise the think time."""
    if cfg.honor_retry_after and state.get("status") == 429:
        try:
            retry_after = float(state.get("retry_after") or "")
        except ValueError:
            retry_after = 0.0
        if retry_after > 0:
            return retry_after * rng.uniform(0.8, 1.2)
    return rng.uniform(cfg.think_min, cfg.think_max)


async def chat_once(client: httpx.AsyncClient, cfg: Config, key: str, prompt: str, state: dict) -> None:
    body = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": cfg.max_tokens,
        "stream": cfg.stream,
    }
    if cfg.stream:
        body["stream_options"] = {"include_usage": True}
    t0 = time.perf_counter()
    async with asyncio.timeout(cfg.request_timeout):
        async with client.stream(
            "POST", "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}
        ) as resp:
            state["status"] = resp.status_code
            if not resp.is_success:
                state["error"] = "http"
                state["msg"] = short_error(await resp.aread())
                state["retry_after"] = resp.headers.get("retry-after")
                return
            if not cfg.stream:
                data = json.loads(await resp.aread())
                state["tokens"] = int((data.get("usage") or {}).get("completion_tokens") or 0)
                return
            await read_sse(resp, state, t0)


async def read_sse(resp: httpx.Response, state: dict, t0: float) -> None:
    content_chunks, usage_tokens, done = 0, None, False
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        if state["ttft"] is None:
            state["ttft"] = time.perf_counter() - t0
        payload = line[5:].strip()
        if payload == "[DONE]":
            # Keep reading to the end of the response: closing before the chunked terminator makes httpcore
            # drop the socket instead of returning it to the keep-alive pool (a new TCP+TLS handshake per request).
            done = True
            continue
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("error"):
            state["error"] = "stream_error"
            state["msg"] = short_error(json.dumps(event, ensure_ascii=False))
            break
        if event.get("usage"):
            usage_tokens = int(event["usage"].get("completion_tokens") or 0)
        for choice in event.get("choices") or []:
            if (choice.get("delta") or {}).get("content"):
                content_chunks += 1
    state["tokens"] = usage_tokens if usage_tokens is not None else content_chunks
    if state["error"] is None and not done:
        state["error"] = "protocol"
        state["msg"] = "поток закончился без [DONE]"


async def portal_once(client: httpx.AsyncClient, cfg: Config, path: str, state: dict) -> None:
    async with asyncio.timeout(cfg.request_timeout):
        resp = await client.get(path, headers={"Cookie": f"{COOKIE_NAME}={cfg.portal_cookie}"})
    state["status"] = resp.status_code
    if not resp.is_success:
        state["error"] = "http"
        state["msg"] = short_error(resp.content)


# ------------------------------------------------------------------ virtual users


async def user_loop(i: int, client, cfg: Config, rec: Recorder, t0: float) -> None:
    rng = random.Random(cfg.seed * 100003 + i)
    key = cfg.keys[i % len(cfg.keys)]
    stop_at = t0 + cfg.ramp + cfg.duration
    await asyncio.sleep(max(t0 + i * cfg.ramp / max(cfg.users, 1) - time.monotonic(), 0))
    while time.monotonic() < stop_at:
        prompt = build_prompt(cfg.prompt_tokens, rng)
        state = await timed_request(rec, "chat", i, lambda state: chat_once(client, cfg, key, prompt, state))
        await asyncio.sleep(pause_after(state, cfg, rng))


async def portal_loop(client, cfg: Config, rec: Recorder, t0: float) -> None:
    """Open loop: one request every 1/rps seconds, never waiting for the previous one."""
    stop_at = t0 + cfg.ramp + cfg.duration
    slots = asyncio.Semaphore(MAX_PORTAL_IN_FLIGHT)
    tasks: set[asyncio.Task] = set()
    interval, tick = 1.0 / portal_rate(cfg), 0

    async def one(path: str) -> None:
        try:
            await timed_request(rec, f"portal:{path}", -1, lambda state: portal_once(client, cfg, path, state))
        finally:
            slots.release()

    next_at = time.monotonic()
    while next_at < stop_at:
        path = PORTAL_PATHS[tick % len(PORTAL_PATHS)]
        tick += 1
        if slots.locked():  # the load host cannot keep up with the requested rate: say so, do not queue
            now = time.time()
            rec.write(kind=f"portal:{path}", user=-1, start=now, end=now, latency=0.0, ttft=None, status=0,
                      error="client_overload", tokens=None, retry_after=None, msg="слишком много запросов в полёте")
        else:
            await slots.acquire()
            task = asyncio.create_task(one(path))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        next_at += interval
        await asyncio.sleep(max(next_at - time.monotonic(), 0))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def progress_loop(rec: Recorder, t0: float, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        print(f"[{time.monotonic() - t0:6.0f}s] в полёте {rec.in_flight:5d}  запросов {rec.total:7d}  "
              f"ok {rec.ok}  429 {rec.rejected}  ошибок {rec.failed}  лаг цикла макс. {rec.max_lag * 1000:.0f} мс",
              file=sys.stderr, flush=True)


async def lag_loop(rec: Recorder, every: float) -> None:
    """Saturation signal of the load generator itself: how late a plain sleep wakes up.

    A busy event loop (too many streams, TLS, JSON) delays every timer; that delay is added to every TTFT
    and latency this process measures, so report.py flags a run whose p95 lag is high.
    """
    while True:
        before = time.monotonic()
        await asyncio.sleep(every)
        rec.write_lag(max(time.monotonic() - before - every, 0.0))


async def run_load(cfg: Config, out_path, transport: httpx.AsyncBaseTransport | None = None,
                   progress_every: float = 10.0, lag_every: float = 0.5) -> dict:
    rec = Recorder(Path(out_path))
    client = httpx.AsyncClient(
        base_url=cfg.base, transport=transport or make_transport(cfg),
        timeout=httpx.Timeout(cfg.request_timeout, connect=min(cfg.request_timeout, 30.0)),
    )
    t0 = time.monotonic()
    wall_start = time.time()
    tasks = [asyncio.create_task(user_loop(i, client, cfg, rec, t0)) for i in user_indices(cfg)]
    if cfg.portal_rps > 0 and cfg.portal_cookie:
        tasks.append(asyncio.create_task(portal_loop(client, cfg, rec, t0)))
    background = []
    if progress_every:
        background.append(asyncio.create_task(progress_loop(rec, t0, progress_every)))
    if lag_every:
        background.append(asyncio.create_task(lag_loop(rec, lag_every)))
    try:
        await asyncio.gather(*tasks)
    finally:
        everything = tasks + background
        for task in everything:
            task.cancel()
        await asyncio.gather(*everything, return_exceptions=True)
        await client.aclose()
        rec.close()
    return {"requests": rec.total, "ok": rec.ok, "rejected_429": rec.rejected, "failed": rec.failed,
            "started": wall_start, "elapsed_s": time.monotonic() - t0, "max_loop_lag_s": rec.max_lag}


# ------------------------------------------------------------------ CLI


def load_keys(path: str) -> list[str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    keys = [item["key"] for item in data["keys"]]
    if not keys:
        raise ValueError(f"в {path} нет ключей")
    return keys


def read_cookie(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    return value.removeprefix(f"{COOKIE_NAME}=")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Нагрузочный тест шлюза моделей: до 1000 виртуальных пользователей")
    p.add_argument("--base", required=True, help="адрес шлюза, например https://llm.nsu.ru")
    p.add_argument("--resolve", action="append", default=[], metavar="HOST:IP",
                   help="подключаться к IP вместо DNS для HOST (SNI и Host остаются HOST); можно повторять")
    p.add_argument("--cacert", help="файл CA для проверки сертификата")
    p.add_argument("--keys", required=True, help="keys.json от prepare.py create")
    p.add_argument("--model", required=True, help="имя модели в запросах")
    p.add_argument("--users", type=int, default=1000)
    p.add_argument("--ramp", type=float, default=60.0, help="за сколько секунд стартуют все пользователи")
    p.add_argument("--duration", type=float, default=300.0, help="секунд под полной нагрузкой после разгона")
    p.add_argument("--prompt-tokens", type=int, default=64)
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--stream", action="store_true", help="потоковые ответы (SSE), меряется TTFT")
    p.add_argument("--think-min", type=float, default=1.0)
    p.add_argument("--think-max", type=float, default=3.0)
    p.add_argument("--request-timeout", type=float, default=120.0)
    p.add_argument("--honor-retry-after", action="store_true",
                   help="после 429 ждать Retry-After (±20%%) вместо паузы --think-min..--think-max, как клиент OpenAI SDK")
    p.add_argument("--portal-cookie-file", help=f"файл со значением cookie {COOKIE_NAME} для трафика портала")
    p.add_argument("--portal-rps", type=float, default=0.0, help="запросов портала в секунду (/api/me, /api/models, /api/keys)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--shard", default="1/1", metavar="I/N",
                   help="запустить только часть пользователей: процесс I из N (одинаковые --users, --ramp, --keys "
                        "и свой --out у всех); долю портала --portal-rps тоже делят поровну")
    p.add_argument("--out", required=True, help="results.jsonl")
    return p


def parse_shard(text: str) -> tuple[int, int]:
    """'2/4' -> (1, 4): zero-based index and count."""
    index, sep, count = text.partition("/")
    if not (sep and index.isdigit() and count.isdigit() and 1 <= int(index) <= int(count)):
        raise ValueError(f"--shard ожидает I/N, где 1 <= I <= N, получено {text!r}")
    return int(index) - 1, int(count)


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.users < 1 or args.think_min < 0 or args.think_max < args.think_min:
        parser.error("--users >= 1 и 0 <= --think-min <= --think-max")
    if args.portal_rps > 0 and not args.portal_cookie_file:
        parser.error("--portal-rps требует --portal-cookie-file")
    try:
        parse_resolve(args.resolve)
        shard_index, shard_count = parse_shard(args.shard)
        keys = load_keys(args.keys)
        cookie = read_cookie(args.portal_cookie_file) if args.portal_cookie_file else None
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    cfg = Config(
        base=args.base.rstrip("/"), keys=keys, model=args.model, users=args.users, ramp=args.ramp,
        duration=args.duration, prompt_tokens=args.prompt_tokens, max_tokens=args.max_tokens, stream=args.stream,
        think_min=args.think_min, think_max=args.think_max, request_timeout=args.request_timeout,
        honor_retry_after=args.honor_retry_after,
        portal_cookie=cookie, portal_rps=args.portal_rps, seed=args.seed,
        shard_index=shard_index, shard_count=shard_count,
    )
    return cfg, args


def main(argv=None) -> int:
    cfg, args = parse_args(argv)
    raise_file_limit(connection_limit(cfg))
    transport = make_transport(cfg, args.resolve, args.cacert)
    shard = f" (часть {cfg.shard_index + 1}/{cfg.shard_count} от {cfg.users})" if cfg.shard_count > 1 else ""
    print(f"{len(user_indices(cfg))} пользователей{shard}, разгон {cfg.ramp:.0f} с, нагрузка {cfg.duration:.0f} с, "
          f"{'поток' if cfg.stream else 'без потока'}, модель {cfg.model}", file=sys.stderr)
    try:
        summary = asyncio.run(run_load(cfg, args.out, transport))
    except KeyboardInterrupt:
        print("Прервано; записанные результаты сохранены.", file=sys.stderr)
        return 130
    print(f"Готово за {summary['elapsed_s']:.0f} с: запросов {summary['requests']}, ok {summary['ok']}, "
          f"429 {summary['rejected_429']}, ошибок {summary['failed']}, лаг цикла макс. {summary['max_loop_lag_s'] * 1000:.0f} мс. Отчёт: python report.py {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
