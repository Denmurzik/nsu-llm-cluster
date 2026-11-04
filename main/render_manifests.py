"""Рендер общих манифестов главного сервера: manifests/common-*.yaml.tmpl -> DIR/common-*.yaml.

Подстановка через string.Template.substitute: отсутствующая переменная -> KeyError
с её именем, литеральный `$` пишется `$$`.

Кроме секретов (VARIABLES) читается LITELLM_WORKERS (по умолчанию 4, от 1 до 16): число воркеров
LiteLLM. Из него считается пул соединений каждого воркера с Postgres, чтобы все клиенты базы
помещались в max_connections.

Зеркало Hugging Face (common-40-hf-mirror): HF_MIRROR_MAX_GIB (лимит кэша, по умолчанию 500), HF_MIRROR_PORT
(127.0.0.1:<порт>, по умолчанию 8090), HF_ENDPOINT (откуда зеркало качает, по умолчанию https://huggingface.co) и
прокси HTTPS_PROXY / HTTP_PROXY / NO_PROXY.

NVIDIA device plugin (common-45): по умолчанию из GPU-образа ноды NODE_IMAGE_REPO:NODE_IMAGE_TAG-gpu (тот же, что у
подов инференса; на ноде он уже есть, не скачивается); DEVICE_PLUGIN_IMAGE: образ из зеркала реестра.

Реестр образов для класса (common-50-registry): REGISTRY_IMAGE (по умолчанию registry:2.8.3 с закреплённым digest) и
REGISTRY_PORT (127.0.0.1:<порт>, по умолчанию 5000).

`--ca FILE` вместо общих манифестов пишет common-15-ca.yaml: ConfigMap llm-ca с корневым сертификатом главного
(ca.crt) и системным набором сертификатов вместе с ним (bundle.crt). Поды нод проверяют по нему https-адрес
зеркала (entrypoint.sh вызывает это, когда у Caddy появился корневой сертификат).
"""
import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from string import Template

MANIFESTS_DIR = Path(__file__).resolve().parent / "manifests"
OVERLOAD_HOOK = Path(__file__).resolve().parent / "litellm" / "overload_hook.py"
SYSTEM_PROMPT_HOOK = Path(__file__).resolve().parent / "litellm" / "system_prompt_hook.py"
HF_MIRROR = Path(__file__).resolve().parent / "hf_mirror.py"
SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
BACKUP_SCRIPT = Path(__file__).resolve().parent / "backup" / "pg-backup.sh"
VARIABLES = (
    "POSTGRES_PASSWORD",
    "LITELLM_DB_PASSWORD",
    "PORTAL_DB_PASSWORD",
    "LITELLM_MASTER_KEY",
    "LITELLM_SALT_KEY",
    "BACKEND_API_KEY",
    "HF_MIRROR_TOKEN",
)
# Необязательные секреты: пустое значение допустимо (тогда портал не ссылается на ключ из подов).
OPTIONAL_VARIABLES = ("HF_TOKEN",)
# Значения попадают в YAML в двойных кавычках и в userinfo URL базы:
# разрешены только символы, безопасные и там и там.
_SAFE = re.compile(r"[A-Za-z0-9._~-]+")

DEFAULT_LITELLM_WORKERS = 4
MAX_LITELLM_WORKERS = 16
POSTGRES_MAX_CONNECTIONS = 300
# Портал: PORTAL_WORKERS (не больше 4) процессов API и процесс контроллеров, у каждого
# DB_POOL_SIZE + DB_MAX_OVERFLOW не больше 30 (по умолчанию 10 + 20); оба предела проверяет entrypoint.sh.
PORTAL_DB_CONNECTIONS = 5 * 30
LITELLM_DB_CONNECTIONS = 120  # на все воркеры LiteLLM вместе
LITELLM_DB_POOL_LIMIT_MAX = 60
DEFAULT_HF_MIRROR_MAX_GIB = 500
DEFAULT_HF_MIRROR_PORT = 8090
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
_URL_SAFE = re.compile(r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")


# Копии Postgres (backup/pg-backup.sh в CronJob postgres-backup): сколько наборов хранить и когда запускать.
# Расписание в UTC (контроллер работает в контейнере без часового пояса): 21:17 UTC это 04:17 в Новосибирске.
DEFAULT_BACKUP_KEEP = 14
MAX_BACKUP_KEEP = 365
DEFAULT_BACKUP_SCHEDULE = "17 21 * * *"
# пять полей cron из цифр, `*`, `/`, `,`, `-`: ничего, что вышло бы за кавычки значения в YAML
_CRON = re.compile(r"[0-9*/,-]+( [0-9*/,-]+){4}")


def _backup_settings(env: Mapping[str, str]) -> tuple[str, str]:
    keep = env.get("BACKUP_KEEP", str(DEFAULT_BACKUP_KEEP))
    if not re.fullmatch(r"[0-9]+", keep) or not 1 <= int(keep) <= MAX_BACKUP_KEEP:
        raise ValueError(f"BACKUP_KEEP: нужно целое число от 1 до {MAX_BACKUP_KEEP}, получено {keep!r}")
    schedule = env.get("BACKUP_SCHEDULE", DEFAULT_BACKUP_SCHEDULE)
    if not _CRON.fullmatch(schedule):
        raise ValueError(f"BACKUP_SCHEDULE: нужны пять полей cron (минута час день месяц день_недели), получено {schedule!r}")
    return str(int(keep)), schedule


def litellm_db_pool_limit(workers: int) -> int:
    """database_connection_pool_limit одного воркера LiteLLM (у каждого воркера свой движок Prisma)."""
    return min(LITELLM_DB_POOL_LIMIT_MAX, LITELLM_DB_CONNECTIONS // workers)


def _litellm_workers(env: Mapping[str, str]) -> int:
    raw = env.get("LITELLM_WORKERS", str(DEFAULT_LITELLM_WORKERS))
    if not re.fullmatch(r"[0-9]+", raw) or not 1 <= int(raw) <= MAX_LITELLM_WORKERS:
        raise ValueError(f"LITELLM_WORKERS: нужно целое число от 1 до {MAX_LITELLM_WORKERS}, получено {raw!r}")
    return int(raw)


def _int_setting(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name) or str(default)
    if not re.fullmatch(r"[0-9]+", raw) or not low <= int(raw) <= high:
        raise ValueError(f"{name}: нужно целое число от {low} до {high}, получено {raw!r}")
    return int(raw)


def _mirror_values(env: Mapping[str, str]) -> dict[str, str]:
    upstream = (env.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?", upstream):
        raise ValueError(f"HF_ENDPOINT: нужен адрес http(s)://хост[:порт][/путь], получено {upstream!r}")
    proxy_lines = []
    for name in PROXY_VARIABLES:
        value = env.get(name, "")
        if not value:
            continue
        if not _URL_SAFE.fullmatch(value):
            raise ValueError(f"{name}: недопустимые символы в адресе прокси")
        proxy_lines.append(f"            - {{name: {name}, value: {json.dumps(value)}}}")
    return {
        "HF_MIRROR_MAX_GIB": str(_int_setting(env, "HF_MIRROR_MAX_GIB", DEFAULT_HF_MIRROR_MAX_GIB, 1, 1_000_000)),
        "HF_MIRROR_PORT": str(_int_setting(env, "HF_MIRROR_PORT", DEFAULT_HF_MIRROR_PORT, 1024, 65535)),
        "HF_MIRROR_UPSTREAM": upstream,
        "HF_TOKEN_FINGERPRINT": hashlib.sha256(env["HF_TOKEN"].encode()).hexdigest()[:12] if env.get("HF_TOKEN") else "",
        "HF_MIRROR_PROXY_ENV": "\n".join(proxy_lines),
        "HF_MIRROR_PY": _indented(HF_MIRROR.read_text(encoding="utf-8")),
    }


DEFAULT_NODE_IMAGE_REPO = "nsu-llm-node"
DEFAULT_NODE_IMAGE_TAG = "0.1.0"
# registry[:port]/path[:tag][@sha256:digest]: nothing that would leave the quoted YAML value
_IMAGE = re.compile(r"[a-z0-9]([a-z0-9._/:-]*[a-z0-9])?(@sha256:[0-9a-f]{64})?")


def _device_plugin_values(env: Mapping[str, str]) -> dict[str, str]:
    """Image of the NVIDIA device plugin: the GPU node image (already on every GPU node, never pulled) or a mirror."""
    mirror = env.get("DEVICE_PLUGIN_IMAGE", "").strip()
    if mirror:
        if not _IMAGE.fullmatch(mirror):
            raise ValueError(f"DEVICE_PLUGIN_IMAGE: нужен образ вида реестр/путь:тег, получено {mirror!r}")
        return {"DEVICE_PLUGIN_IMAGE": mirror, "DEVICE_PLUGIN_PULL_POLICY": "IfNotPresent"}
    repo = env.get("NODE_IMAGE_REPO") or DEFAULT_NODE_IMAGE_REPO
    tag = env.get("NODE_IMAGE_TAG") or DEFAULT_NODE_IMAGE_TAG
    image = f"{repo}:{tag}-gpu"
    if not _IMAGE.fullmatch(image):
        raise ValueError(f"NODE_IMAGE_REPO / NODE_IMAGE_TAG: недопустимый образ {image!r}")
    return {"DEVICE_PLUGIN_IMAGE": image, "DEVICE_PLUGIN_PULL_POLICY": "Never"}


# Реестр образов для класса: образ закреплён версией и digest индекса (проверено docker buildx imagetools inspect registry:2.8.3).
DEFAULT_REGISTRY_IMAGE = "registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
DEFAULT_REGISTRY_PORT = 5000


def _registry_values(env: Mapping[str, str]) -> dict[str, str]:
    image = (env.get("REGISTRY_IMAGE") or DEFAULT_REGISTRY_IMAGE).strip()
    if not _IMAGE.fullmatch(image):
        raise ValueError(f"REGISTRY_IMAGE: нужен образ вида реестр/путь:тег[@sha256:...], получено {image!r}")
    return {
        "REGISTRY_IMAGE": image,
        "REGISTRY_PORT": str(_int_setting(env, "REGISTRY_PORT", DEFAULT_REGISTRY_PORT, 1024, 65535)),
    }


# HF_MIRROR_ENABLED, прочитанная как её читает pydantic в настройках портала (без учёта регистра); по умолчанию true
_FALSE = ("false", "0", "no", "off", "f", "n")


def mirror_enabled(env: Mapping[str, str]) -> bool:
    return env.get("HF_MIRROR_ENABLED", "").strip().lower() not in _FALSE


def _indented(text: str, prefix: str = "    ") -> str:
    lines = text.rstrip("\n").split("\n")
    return "\n".join(prefix + line if line else "" for line in lines)


def render(env: Mapping[str, str], out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    for name in VARIABLES:
        if name not in env:
            raise KeyError(name)
        if not _SAFE.fullmatch(env[name]):
            raise ValueError(
                f"{name}: допустимы только символы A-Za-z0-9 . _ ~ - (непусто)"
            )
    optional = {name: env.get(name, "") for name in OPTIONAL_VARIABLES}
    for name, value in optional.items():
        if value and not _SAFE.fullmatch(value):
            raise ValueError(f"{name}: допустимы только символы A-Za-z0-9 . _ ~ -")
    workers = _litellm_workers(env)
    backup_keep, backup_schedule = _backup_settings(env)
    values = {
        **env,
        **optional,
        # secret llm-node-secrets (его читают kubelet нод): настоящий токен HF нужен подам нод только без зеркала
        "NODE_HF_TOKEN": "" if mirror_enabled(env) else optional["HF_TOKEN"],
        "LITELLM_WORKERS": str(workers),
        "LITELLM_DB_POOL_LIMIT": str(litellm_db_pool_limit(workers)),
        "POSTGRES_MAX_CONNECTIONS": str(POSTGRES_MAX_CONNECTIONS),
        "OVERLOAD_HOOK_PY": _indented(OVERLOAD_HOOK.read_text(encoding="utf-8")),
        "SYSTEM_PROMPT_HOOK_PY": _indented(SYSTEM_PROMPT_HOOK.read_text(encoding="utf-8")),
        **_mirror_values(env),
        **_device_plugin_values(env),
        **_registry_values(env),
        "BACKUP_SH": _indented(BACKUP_SCRIPT.read_text(encoding="utf-8")),
        "BACKUP_KEEP": backup_keep,
        "BACKUP_SCHEDULE": backup_schedule,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for tmpl in sorted(MANIFESTS_DIR.glob("common-*.yaml.tmpl")):
        template = Template(tmpl.read_text(encoding="utf-8"))
        # $CONFIG_HASH: hash of the file rendered without it, so pods that read a config only at start
        # (LiteLLM) are re-created when the config changes
        digest = hashlib.sha256(template.substitute({**values, "CONFIG_HASH": ""}).encode()).hexdigest()[:16]
        text = template.substitute({**values, "CONFIG_HASH": digest})
        target = out_dir / tmpl.name.removesuffix(".tmpl")
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)
    return written


CA_TEMPLATE = """apiVersion: v1
kind: Namespace
metadata:
  name: llm
  labels:
    app.kubernetes.io/part-of: nsu-llm
---
# Корневой сертификат главного для подов нод: по bundle.crt они проверяют https://<PUBLIC_HOSTNAME>/hf/ (зеркало HF).
apiVersion: v1
kind: ConfigMap
metadata:
  name: llm-ca
  namespace: llm
  labels:
    app.kubernetes.io/part-of: nsu-llm
data:
  ca.crt: |
{ca}
  bundle.crt: |
{bundle}
"""


def render_ca(ca_file: Path, out_dir: Path, system_bundle: Path = SYSTEM_CA_BUNDLE) -> Path:
    """common-15-ca.yaml: ConfigMap llm-ca with the main CA and the system CAs followed by it."""
    ca = Path(ca_file).read_text(encoding="utf-8")
    if "-----BEGIN CERTIFICATE-----" not in ca:
        raise ValueError(f"{ca_file}: это не сертификат в формате PEM")
    ca = ca if ca.endswith("\n") else ca + "\n"
    try:
        system = Path(system_bundle).read_text(encoding="utf-8")
    except OSError:
        system = ""
    if system and not system.endswith("\n"):
        system += "\n"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "common-15-ca.yaml"
    text = CA_TEMPLATE.format(ca=_indented(ca), bundle=_indented(system + ca))
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(target)
    return target


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path, help="каталог для готовых манифестов")
    ap.add_argument("--ca", type=Path, help="записать только ConfigMap llm-ca с этим корневым сертификатом")
    args = ap.parse_args(argv)
    if args.ca is not None:
        try:
            print(render_ca(args.ca, args.out))
        except (OSError, ValueError) as e:
            print(f"не удалось прочитать корневой сертификат: {e}", file=sys.stderr)
            return 2
        return 0
    try:
        files = render(os.environ, args.out)
    except KeyError as e:
        print(f"не задана переменная окружения {e.args[0]}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    for f in files:
        print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
