#!/usr/bin/env python3
"""End-to-end smoke test of the LLM portal: login, catalog, key, streaming chat, usage, revoke.

Exit codes: 0 success, 1 assertion failed (stderr says which), 2 usage or connection error.
Prints one JSON line {"ttft_s", "tokens_per_s", "backends"} on success.
"""
import argparse
import json
import sys
import time

import httpx
import openai

import common
from common import Fail


def log(message):
    print(message, file=sys.stderr, flush=True)


def wait_for_backends(portal, model, expected, timeout, interval):
    deadline = time.monotonic() + timeout
    seen = "модель не найдена в /api/models"
    while True:
        models = portal.json("GET", "/api/models")
        entry = next((m for m in models if m["name"] == model), None)
        if entry is not None:
            backends = entry.get("ready_backends") or 0
            seen = f"status={entry.get('status')}, backends={backends}"
            if entry.get("status") == "ready" and backends >= expected:
                return backends
        if time.monotonic() >= deadline:
            raise Fail(f"модель {model}: ожидалось ready с backends>={expected}, получено {seen} "
                       f"за {timeout:g} с")
        time.sleep(interval)


def stream_chat(client, model):
    started = time.monotonic()
    first = None
    text = []
    chunks = 0
    completion_tokens = None
    stream = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": "Скажи одно короткое слово."}],
        max_tokens=32, stream=True, stream_options={"include_usage": True},
    )
    for event in stream:
        if event.usage is not None:
            completion_tokens = event.usage.completion_tokens
        for choice in event.choices:
            if choice.delta.content:
                if first is None:
                    first = time.monotonic()
                chunks += 1
                text.append(choice.delta.content)
    if not "".join(text).strip():
        raise Fail("потоковый ответ пуст")
    ended = time.monotonic()
    generated = completion_tokens or chunks
    span = ended - first
    return {
        "ttft_s": round(first - started, 3),
        "tokens_per_s": round(generated / span, 2) if span > 0 else None,
    }


def check_usage(client, model):
    response = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": "Ответь одним словом."}], max_tokens=8,
    )
    total = response.usage.total_tokens if response.usage else 0
    if not total or total <= 0:
        raise Fail(f"usage.total_tokens должен быть > 0, получено {total}")


def check_revoked(client, model, timeout, interval):
    deadline = time.monotonic() + timeout
    while True:
        try:
            client.chat.completions.create(model=model, messages=[{"role": "user", "content": "x"}], max_tokens=1)
        except openai.AuthenticationError:
            return
        except openai.RateLimitError:
            pass  # the gateway rate-limits the probe: that says nothing about the key, ask again
        except openai.APIStatusError as exc:
            raise Fail(f"после удаления ключ должен давать 401, получено {exc.status_code}")
        if time.monotonic() >= deadline:
            raise Fail("ключ продолжает работать после удаления")
        time.sleep(interval)


def run(args):
    portal = common.Portal.from_args(args)
    log("сессия готова")
    key_id = None
    try:
        backends = wait_for_backends(portal, args.model, args.expect_backends, args.timeout, args.poll_interval)
        log(f"{args.model}: ready, backends={backends}")
        alias = f"e2e-smoke-{int(time.time())}"
        created = portal.json("POST", "/api/keys", json={"alias": alias, "expires_days": None})
        key_id = created["id"]
        log(f"ключ создан: {created.get('masked')}")
        http_client = httpx.Client(transport=portal.transport, timeout=120)
        client = openai.OpenAI(base_url=f"{portal.base}/v1", api_key=created["key"],
                               http_client=http_client, max_retries=0)
        try:
            metrics = stream_chat(client, args.model)
            log("потоковый чат: ок")
            check_usage(client, args.model)
            log("usage.total_tokens > 0: ок")
            portal.request("DELETE", f"/api/keys/{key_id}")
            key_id = None
            # each LiteLLM worker caches keys for user_api_key_cache_ttl (15 s)
            check_revoked(client, args.model, 25, 2.0)
            log("удалённый ключ даёт 401: ок")
        finally:
            http_client.close()
    except openai.APIConnectionError as exc:
        raise httpx.ConnectError(str(exc.__cause__ or exc)) from exc
    finally:
        if key_id is not None:
            try:
                portal.request("DELETE", f"/api/keys/{key_id}", expect=(204, 404))
            except (Fail, httpx.HTTPError) as exc:
                log(f"не удалось удалить ключ {key_id}: {exc}")
        portal.close()
    print(json.dumps({**metrics, "backends": backends}, ensure_ascii=False))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common.add_connection_args(parser)
    parser.add_argument("--model", default="gemma-4-e4b")
    parser.add_argument("--expect-backends", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=900, help="seconds to wait for the model to be ready")
    parser.add_argument("--poll-interval", type=float, default=5)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    common.validate_connection_args(parser, args)
    return common.run_guarded(lambda: run(args))


if __name__ == "__main__":
    sys.exit(main())
