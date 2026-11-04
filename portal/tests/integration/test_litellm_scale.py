"""Overload behaviour of the real gateway: LiteLLM v1.106.6 with the rendered production config (2 workers,
Redis, overload hook) + Postgres + Redis + a slow counting backend, all in one network namespace as on
llm-main (hostNetwork: everything on 127.0.0.1).

Checks what the portal relies on: `max_parallel_requests` bounds the backend per worker, a saturated model
answers 429 in Russian with Retry-After (never 5xx), the 429s put no deployment into cooldown, and a model
registered through one worker is served by all of them.
"""

import asyncio
import json
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import yaml
from testcontainers.core.container import DockerContainer

from app.litellm_client import LiteLLMClient
from tests.integration.stack import LITELLM_IMAGE, _wait_http

pytestmark = pytest.mark.docker

HERE = Path(__file__).resolve().parent
MAIN = HERE.parents[2] / "main"
WORKERS = 2
MASTER_KEY = "sk-it-20b-master"
SALT_KEY = "sk-it-20b-salt-0123456789"
BACKEND = "http://127.0.0.1:18080/v1"
SATURATED = "Модель slow перегружена: все слоты и очередь заняты. Повторите запрос через 5 с."


def _rendered_litellm_config(out: Path) -> list[str]:
    """config.yaml and overload_hook.py exactly as the ConfigMap ships them; returns the container args."""
    sys.path.insert(0, str(MAIN))
    try:
        import render_manifests
    finally:
        sys.path.remove(str(MAIN))
    env = {
        "POSTGRES_PASSWORD": "pg-root",
        "LITELLM_DB_PASSWORD": "ll-db",
        "PORTAL_DB_PASSWORD": "portal-db",
        "LITELLM_MASTER_KEY": MASTER_KEY,
        "LITELLM_SALT_KEY": SALT_KEY,
        "BACKEND_API_KEY": "it20bbackendkey",
        "HF_MIRROR_TOKEN": "hf_" + "0" * 40,
        "LITELLM_WORKERS": str(WORKERS),
    }
    manifests = out / "manifests"
    render_manifests.render(env, manifests)
    docs = list(yaml.safe_load_all((manifests / "common-30-litellm.yaml").read_text(encoding="utf-8")))
    (cm,) = [d for d in docs if d and d["kind"] == "ConfigMap"]
    (dep,) = [d for d in docs if d and d["kind"] == "Deployment"]
    config = out / "config"
    config.mkdir()
    for name, text in cm["data"].items():
        (config / name).write_text(text, encoding="utf-8", newline="\n")
    args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
    # the published port of a container reaches its eth0, not loopback: listen on all addresses here
    return [a if a != "127.0.0.1" else "0.0.0.0" for a in args]


@contextmanager
def scale_stack() -> Iterator[tuple[str, str]]:
    tmp = Path(tempfile.mkdtemp(prefix="it-20b-"))
    args = _rendered_litellm_config(tmp)
    suffix = uuid.uuid4().hex[:6]
    owner_name = f"it-t2-20b-redis-{suffix}"
    netns = {"network_mode": f"container:{owner_name}"}
    # the Redis container owns the shared network namespace and publishes LiteLLM's and the backend's ports
    redis = (
        DockerContainer("redis:7-alpine")
        .with_name(owner_name)
        .with_exposed_ports(4000, 18080)
        .with_command("--bind 127.0.0.1 --port 6379 --save '' --appendonly no")
    )
    pg = (
        DockerContainer("postgres:17-alpine")
        .with_name(f"it-t2-20b-pg-{suffix}")
        .with_kwargs(**netns)
        .with_env("POSTGRES_USER", "litellm")
        .with_env("POSTGRES_PASSWORD", "litellm-pw")
        .with_env("POSTGRES_DB", "litellm")
        .with_command("-c max_connections=300")
    )
    backend = (
        DockerContainer("python:3.12-slim")
        .with_name(f"it-t2-20b-backend-{suffix}")
        .with_kwargs(**netns)
        .with_volume_mapping(str(HERE), "/it", "ro")
        .with_command(
            'sh -c "pip install -q --disable-pip-version-check starlette uvicorn'
            ' && python /it/slow_backend.py --port 18080 --delay-ms 100 --words 20"'
        )
    )
    litellm = (
        DockerContainer(LITELLM_IMAGE)
        .with_name(f"it-t2-20b-litellm-{suffix}")
        .with_kwargs(**netns)
        .with_volume_mapping(str(tmp / "config"), "/etc/litellm", "ro")
        .with_env("DATABASE_URL", "postgresql://litellm:litellm-pw@127.0.0.1:5432/litellm")
        .with_env("LITELLM_MASTER_KEY", MASTER_KEY)
        .with_env("LITELLM_SALT_KEY", SALT_KEY)
        .with_env("STORE_MODEL_IN_DB", "True")
        .with_env("DISABLE_ADMIN_UI", "True")
        .with_env("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        .with_env("LITELLM_TELEMETRY_DISABLED", "true")
        .with_command(" ".join(args))
    )
    with redis:
        host = "http://127.0.0.1"
        with pg, backend:
            backend_url = f"{host}:{redis.get_exposed_port(18080)}"
            _wait_http(backend_url + "/health", backend, 240, "slow backend")
            with litellm:
                url = f"{host}:{redis.get_exposed_port(4000)}"
                _wait_http(url + "/health/readiness", litellm, 420, "LiteLLM")
                yield url, backend_url


@pytest.fixture(scope="module")
def stack():
    with scale_stack() as s:
        yield s


async def _request(http: httpx.AsyncClient, key: str, model: str = "slow") -> tuple[int, dict, str]:
    """One streamed chat request: status, headers and the whole body."""
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": True}
    async with http.stream(
        "POST", "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}
    ) as resp:
        text = (await resp.aread()).decode("utf-8", errors="replace")
        return resp.status_code, dict(resp.headers), text


async def test_saturated_model_answers_russian_429_and_recovers(stack):
    url, backend_url = stack
    client = LiteLLMClient(url, MASTER_KEY)
    try:
        user = f"it-20b-{uuid.uuid4().hex[:6]}"
        await client.ensure_user(user, f"{user}@g.nsu.ru")
        key = (await client.generate_key(user, f"{user}:k", None, None, None, {})).key
        await client.add_model("pm-scale-1", "slow", BACKEND, "k8s", "n1", 4096, max_parallel_requests=1)
        (m,) = [m for m in await client.list_managed_models() if m.id == "pm-scale-1"]
        assert m.max_parallel_requests == 1

        async with httpx.AsyncClient(base_url=url, timeout=120) as http, httpx.AsyncClient(
            base_url=backend_url
        ) as backend:
            # the entry was written through one worker; the others load it on the Redis change notice (a
            # worker without it answers 400). One request at a time: no slot is ever contended here.
            deadline = time.monotonic() + 60
            while True:
                results = [await _request(http, key) for _ in range(6)]
                if all(status == 200 for status, _, _ in results) or time.monotonic() > deadline:
                    break
                await asyncio.sleep(1)
            assert [status for status, _, _ in results] == [200] * 6, results

            await backend.post("/stats/reset")
            burst = await asyncio.gather(*(_request(http, key) for _ in range(16)))
            stats = (await backend.get("/stats")).json()

            statuses = sorted(status for status, _, _ in burst)
            assert set(statuses) <= {200, 429}, burst
            assert 200 in statuses and 429 in statuses, statuses
            for status, headers, text in burst:
                if status == 200:
                    assert text.rstrip().endswith("data: [DONE]"), text[-200:]
                else:
                    assert headers.get("retry-after") == "5", headers
                    assert json.loads(text)["error"]["message"] == SATURATED, text
            # LiteLLM counts max_parallel_requests per worker: at most workers x 1 reach the backend at once
            assert stats["max_in_flight"] <= WORKERS, stats

            # the 429s cooled nothing down: the model answers right after the burst
            status, _, text = await _request(http, key)
            assert status == 200, text
    finally:
        await client.delete_model("pm-scale-1")
        await client.aclose()
