"""Real LiteLLM v1.106.6 + Postgres + fake OpenAI backend on a private Docker network.

Container names are unique per run (`it-t5-*-<random>`, so concurrent runs never clash) and everything is removed on exit.
"""

import tempfile
import uuid
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

LITELLM_IMAGE = "ghcr.io/berriai/litellm:v1.106.6"
MASTER_KEY = "sk-it-t5-master"
SALT_KEY = "sk-it-t5-salt-0123456789"
FAKES_DIR = Path(__file__).resolve().parents[1] / "fakes"
HOOK_FILE = Path(__file__).resolve().parents[3] / "main" / "litellm" / "system_prompt_hook.py"

# mirrors the ConfigMap in main/manifests/common-30-litellm.yaml.tmpl
LITELLM_CONFIG = """\
model_list: []
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: true
  background_health_checks: false
router_settings:
  routing_strategy: least-busy
  num_retries: 2
  timeout: 600
  allowed_fails: 1
  cooldown_time: 30
litellm_settings:
  drop_params: true
  callbacks: ["system_prompt_hook.handler"]
"""


@dataclass
class Stack:
    url: str
    master_key: str
    backend_alias: str  # reachable from inside the Docker network
    backend_port: int
    litellm: DockerContainer
    backend_url: str = ""  # the fake backend as seen from the host


def _wait_http(url: str, container: DockerContainer, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    logs = container.get_logs()
    tail = "".join(part.decode(errors="replace") for part in logs if part)[-3000:]
    raise TimeoutError(f"{what} not ready after {timeout}s\n{tail}")


def _host_port(container: DockerContainer, port: int) -> str:
    return f"http://127.0.0.1:{container.get_exposed_port(port)}"


@contextmanager
def start_stack() -> Iterator[Stack]:
    sfx = uuid.uuid4().hex[:6]
    pg_name, backend_name, litellm_name = (f"it-t5-{n}-{sfx}" for n in ("pg", "backend", "litellm"))
    config_dir = Path(tempfile.mkdtemp(prefix="it-t5-litellm-"))
    (config_dir / "config.yaml").write_text(LITELLM_CONFIG, encoding="utf-8")
    # the hook exactly as the ConfigMap ships it (main/manifests/common-30-litellm.yaml.tmpl)
    (config_dir / HOOK_FILE.name).write_text(HOOK_FILE.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    with Network() as net:
        pg = (
            DockerContainer("postgres:17-alpine")
            .with_name(pg_name)
            .with_network(net)
            .with_network_aliases(pg_name)
            .with_env("POSTGRES_USER", "litellm")
            .with_env("POSTGRES_PASSWORD", "litellm-pw")
            .with_env("POSTGRES_DB", "litellm")
        )
        backend = (
            DockerContainer("python:3.12-slim")
            .with_name(backend_name)
            .with_network(net)
            .with_network_aliases(backend_name)
            .with_exposed_ports(18080)
            .with_volume_mapping(str(FAKES_DIR), "/fakes", "ro")
            .with_command(
                'sh -c "pip install -q --disable-pip-version-check fastapi starlette uvicorn'
                ' && python /fakes/openai_backend.py --host 0.0.0.0 --port 18080 --model-id test-model"'
            )
        )
        litellm = (
            DockerContainer(LITELLM_IMAGE)
            .with_name(litellm_name)
            .with_network(net)
            .with_network_aliases(litellm_name)
            .with_exposed_ports(4000)
            .with_volume_mapping(str(config_dir), "/etc/litellm", "ro")
            .with_env("DATABASE_URL", f"postgresql://litellm:litellm-pw@{pg_name}:5432/litellm")
            .with_env("LITELLM_MASTER_KEY", MASTER_KEY)
            .with_env("LITELLM_SALT_KEY", SALT_KEY)
            .with_env("STORE_MODEL_IN_DB", "True")
            .with_env("DISABLE_ADMIN_UI", "True")
            .with_env("LITELLM_LOCAL_MODEL_COST_MAP", "True")
            .with_env("LITELLM_TELEMETRY_DISABLED", "true")
            .with_command("--config /etc/litellm/config.yaml --host 0.0.0.0 --port 4000 --num_workers 1")
        )
        with pg, backend:
            _wait_http(_host_port(backend, 18080) + "/health", backend, 240, "fake backend")
            with litellm:
                url = _host_port(litellm, 4000)
                _wait_http(url + "/health/readiness", litellm, 420, "LiteLLM")
                yield Stack(url, MASTER_KEY, backend_name, 18080, litellm, _host_port(backend, 18080))
