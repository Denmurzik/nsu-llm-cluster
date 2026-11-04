#!/usr/bin/env python3
"""Prepares and cleans LiteLLM for a load test. Runs inside the main container (stdlib only):

    docker cp loadtest llm-main:/opt/llm/loadtest
    docker exec llm-main python3 /opt/llm/loadtest/prepare.py create --users 1000 --rpm 600 --out /tmp/keys.json
    docker exec llm-main python3 /opt/llm/loadtest/prepare.py register-backends --model lt-fake \\
        --api-base http://<load-host>:28100/v1 --api-base http://<load-host>:28101/v1
    docker exec llm-main python3 /opt/llm/loadtest/prepare.py cleanup

The master key comes from $LITELLM_MASTER_KEY or the secrets file (default /var/lib/llm/secrets.env).
Everything created here is marked: users `loadtest-<i>` and keys with alias `loadtest-<i>` carry
metadata `loadtest=true`, fake backends carry model_info `loadtest=true`. `cleanup` removes only
objects that match BOTH the name prefix and the metadata flag. Secrets are written to --out (mode 600)
and never printed.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:4000"
DEFAULT_ENV_FILE = "/var/lib/llm/secrets.env"
PREFIX = "loadtest-"
META = {"loadtest": True}
HTTP_TIMEOUT = 60
WORKERS = 8


class AdminError(Exception):
    pass


class Admin:
    def __init__(self, base: str, master_key: str):
        self.base = base.rstrip("/")
        self.master_key = master_key

    def call(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {self.master_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").replace(self.master_key, "***")[:300]
            raise AdminError(f"{method} {path}: LiteLLM ответил {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise AdminError(f"{method} {path}: LiteLLM недоступен ({exc})") from None
        return json.loads(raw) if raw else None

    def pages(self, path: str, field: str, page_size: int, extra: dict | None = None):
        """Yields the items of a paged listing (`page`, `size`/`page_size`, `total_pages`)."""
        page = 1
        while True:
            data = self.call("GET", path, query={"page": page, "page_size": page_size, "size": page_size, **(extra or {})})
            yield from (data or {}).get(field) or []
            if page >= int((data or {}).get("total_pages") or 1):
                return
            page += 1


def master_key(env: dict, env_file: str) -> str:
    key = env.get("LITELLM_MASTER_KEY")
    if not key:
        try:
            for line in Path(env_file).read_text(encoding="utf-8").splitlines():
                name, sep, value = line.partition("=")
                if sep and name.strip() == "LITELLM_MASTER_KEY":
                    key = value.strip()
        except OSError:
            pass
    if not key:
        raise AdminError(f"не найден LITELLM_MASTER_KEY: задайте переменную окружения или файл {env_file}")
    return key


def is_loadtest(metadata) -> bool:
    return isinstance(metadata, dict) and metadata.get("loadtest") is True


def _in_pool(fn, items):
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(fn, items))


# ------------------------------------------------------------------ commands


def cmd_create(admin: Admin, args) -> int:
    ids = [f"{PREFIX}{i}" for i in range(args.users)]

    def make_user(user_id: str) -> None:
        admin.call("POST", "/user/new", {
            "user_id": user_id, "user_email": f"{user_id}@loadtest.invalid", "user_role": "internal_user",
            "auto_create_key": False, "metadata": META})

    def make_key(user_id: str) -> dict:
        body = {"user_id": user_id, "key_alias": user_id, "models": [], "key_type": "llm_api",
                "rpm_limit": args.rpm, "tpm_limit": args.tpm, "metadata": META}
        data = admin.call("POST", "/key/generate", body)
        return {"user_id": user_id, "key": data["key"], "token": data["token"]}

    _in_pool(make_user, ids)
    keys = _in_pool(make_key, ids)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"keys": keys}, fh)
    print(f"Создано пользователей и ключей: {len(keys)}. Ключи записаны в {args.out}")
    return 0


def cmd_cleanup(admin: Admin, args) -> int:
    key_tokens = [
        k["token"] for k in admin.pages("/key/list", "keys", args.page_size, {"return_full_object": "true"})
        if isinstance(k, dict) and str(k.get("key_alias") or "").startswith(PREFIX) and is_loadtest(k.get("metadata"))
    ]
    user_ids = [
        u["user_id"] for u in admin.pages("/user/list", "users", args.page_size)
        if isinstance(u, dict) and str(u.get("user_id") or "").startswith(PREFIX) and is_loadtest(u.get("metadata"))
    ]
    model_ids = [
        m["model_info"]["id"] for m in (admin.call("GET", "/model/info") or {}).get("data") or []
        if (m.get("model_info") or {}).get("loadtest") is True
    ]
    for start in range(0, len(key_tokens), 100):
        admin.call("POST", "/key/delete", {"keys": key_tokens[start:start + 100]})
    for start in range(0, len(user_ids), 100):
        admin.call("POST", "/user/delete", {"user_ids": user_ids[start:start + 100]})
    for model_id in model_ids:
        admin.call("POST", "/model/delete", {"id": model_id})
    print(f"Удалено: ключей {len(key_tokens)}, пользователей {len(user_ids)}, тестовых бэкендов {len(model_ids)}")
    return 0


def cmd_register_backends(admin: Admin, args) -> int:
    for api_base in args.api_base:
        admin.call("POST", "/model/new", {
            "model_name": args.model,
            "litellm_params": {"model": f"openai/{args.model}", "api_base": api_base, "api_key": "none"},
            "model_info": {"id": "lt-" + hashlib.sha1(f"{args.model}|{api_base}".encode()).hexdigest()[:12],
                           "loadtest": True},
        })
    print(f"Зарегистрировано бэкендов для модели {args.model}: {len(args.api_base)}")
    return 0


# ------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Подготовка LiteLLM к нагрузочному тесту")
    p.add_argument("--base", default=DEFAULT_BASE, help="адрес LiteLLM (внутри контейнера main)")
    p.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="файл секретов с LITELLM_MASTER_KEY")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):  # allow the shared options after the subcommand as well
        sp.add_argument("--base", default=argparse.SUPPRESS)
        sp.add_argument("--env-file", default=argparse.SUPPRESS)
        return sp

    c = common(sub.add_parser("create", help="создать пользователей loadtest-<i> и по ключу каждому"))
    c.add_argument("--users", type=int, required=True)
    c.add_argument("--rpm", type=int, default=600, help="лимит запросов в минуту на ключ")
    c.add_argument("--tpm", type=int, default=10_000_000, help="лимит токенов в минуту на ключ")
    c.add_argument("--out", required=True, help="keys.json (права 600)")
    c.set_defaults(func=cmd_create)

    d = common(sub.add_parser("cleanup", help="удалить всё, что создал этот скрипт"))
    d.add_argument("--page-size", type=int, default=100)
    d.set_defaults(func=cmd_cleanup)

    r = common(sub.add_parser("register-backends", help="зарегистрировать тестовые OpenAI-бэкенды под одной моделью"))
    r.add_argument("--model", default="lt-fake")
    r.add_argument("--api-base", action="append", required=True, metavar="URL", help="http://host:port/v1, можно повторять")
    r.set_defaults(func=cmd_register_backends)
    return p


def main(argv=None, env=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        key = master_key(os.environ if env is None else env, args.env_file)
        return args.func(Admin(args.base, key), args)
    except AdminError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
