"""Contract check: every key the web UI's msw fixtures use exists in the real portal response.

Fixtures live in `web/src/mocks/fixtures/<method>__<path>.json`. Each one is compared with the live response of the
matching endpoint (seeded with representative data): keys must exist at every depth and the JSON types must agree
(null matches anything). Entries of `KNOWN_UI_MISMATCHES` are documented differences that live on the UI side.
"""

import json
from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.catalog.gguf import GgufMeta
from app.catalog.hf import HfFile
from app.litellm_client import Activity, DayUsage
from app.models import ControllerTimer, ModelDeployment, NodeAssignment, NodeWake, PortalFlag, StandaloneNode
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.fakes.mirror_admin import FakeMirrorAdmin, cached_file
from tests.fakes.progress import FakeProgress
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

FIXTURES = Path(__file__).resolve().parents[2] / "web" / "src" / "mocks" / "fixtures"
GiB = 2**30
REPO, FILE = "org/model-GGUF", "model-Q4_K_M.gguf"
META = GgufMeta("gemma4", 42, 8, 2, 256, 2560, 131072)
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}

# fixture file -> {json path: reason}; the portal side is the contract, these are for the UI to follow
KNOWN_UI_MISMATCHES: dict[str, dict[str, str]] = {
    # one list holds int, float and bool settings, so `value` and `default` have no single type
    "get__api_admin_settings": {"$[].value": "mixed types", "$[].default": "mixed types"},
}


@pytest.fixture(autouse=True)
def _disk_reserve(monkeypatch):
    """Uploads keep 15 % of the volume free: the test host's own disk may be fuller than that."""
    from app.localmodels.store import DISK_RESERVE, LocalStore

    monkeypatch.setattr(LocalStore, "reserve_bytes", lambda self: DISK_RESERVE)


@pytest.fixture(autouse=True)
def _registry_of_main():
    """The image registry of main (task 2-30) answers from a fake: /api/admin/versions lists its tags."""
    import httpx

    from app.nodes import versions
    from tests.test_node_versions import D1, D2, FakeRegistry

    fake = FakeRegistry()
    fake.tags = {"0.2.1-cpu": D1, "0.2.1-gpu": D2}
    versions.REGISTRY.reset()
    versions.REGISTRY.transport = httpx.MockTransport(fake)
    yield
    versions.REGISTRY.reset()
    versions.REGISTRY.transport = None


@pytest.fixture
def settings(settings):
    settings.hf_endpoint = "http://127.0.0.1:9"
    settings.main_advertise_ip = "192.168.3.21"
    return settings


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(NodeAssignment))
        await db.execute(delete(StandaloneNode))
        await db.execute(delete(ModelDeployment))
        await db.execute(delete(PortalFlag))
        await db.execute(delete(NodeWake))
        await db.execute(delete(ControllerTimer))
        await db.commit()
    app.state.hf = FakeHf(
        files={REPO: [HfFile(FILE, 4 * GiB), HfFile("model-Q8_0.gguf", 8 * GiB)]},
        metas={(REPO, FILE): META, (REPO, "model-Q8_0.gguf"): META},
    )
    k8s = app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()
    app.state.backend_status = {"at": "t", "backends": []}
    app.state.progress = FakeProgress()  # no mirror, no pod answers: the not-ready pod is "starting"
    k8s.add_node("llm-main", accel="cpu", internal_ip="192.168.3.21", mem_mib=32768, is_main=True)
    k8s.add_node("pc01", accel="cpu", internal_ip="192.168.3.41", mem_mib=16384)
    for i in (1, 2):  # a split group over VRAM and RAM
        k8s.add_node(f"gpu0{i}", accel="nvidia", internal_ip=f"192.168.3.5{i}", vram_mib=12282, mem_mib=32000, ram_mib=28000)
    k8s.add_pod("llm-m1-cpu-abc", "pc01", "inference", "m1", ready=False, restarts=1, message="Загрузка модели")
    k8s.logs["llm-m1-cpu-abc"] = "loading tensors 1/2\nloading tensors 2/2"
    k8s.agent_token = "K10abc::node:agent-test"
    async with app.state.sessionmaker() as db:
        db.add(StandaloneNode(name="win-07", url="http://192.168.3.77:18080", accel="cpu", ram_mib=16384, version="0.1.0"))
        # Wake-on-LAN: win-07 reported its NIC and the controller is waking it
        db.add(NodeWake(node_name="win-07", mac="00:1a:2b:3c:4d:5e", ip="192.168.3.77", prefix=24))
        db.add(ControllerTimer(key="wake:win-07", since=1762171080.0, members="2"))
        await db.commit()


def _kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    return {str: "string", list: "array", dict: "object"}[type(value)]


def contract_mismatches(fixture, actuals: list, path: str = "$") -> list[str]:
    """Differences between a fixture value and the real values found at the same path (lists are flattened)."""
    flat = [x for a in actuals for x in (a if isinstance(a, list) else [a])]
    if isinstance(fixture, list):
        if not fixture:
            return []
        if not flat:
            return [f"{path}: the real response has no items to compare with"]
        problems: list[str] = []
        for item in fixture:
            problems += contract_mismatches(item, flat, f"{path}[]")
        return list(dict.fromkeys(problems))
    kind = _kind(fixture)
    problems = []
    for actual in flat:
        got = _kind(actual)
        if kind != "null" and got not in ("null", kind):
            problems.append(f"{path}: the fixture holds {kind}, the portal returns {got}")
    if isinstance(fixture, dict):
        dicts = [a for a in flat if isinstance(a, dict)]
        for key, value in fixture.items():
            if not any(key in d for d in dicts):
                problems.append(f"{path}.{key}: the key is missing in the portal response")
                continue
            problems += contract_mismatches(value, [d[key] for d in dicts if key in d], f"{path}.{key}")
    return list(dict.fromkeys(problems))


async def _as(client, app, claims) -> dict:
    return csrf_headers(await login(client, app, claims))


async def _seed_deployment(client, h) -> int:
    r = await client.post(
        "/api/admin/deployments",
        json={
            "name": "m1", "repo": REPO, "file": FILE, "nodes": ["pc01"], "auto_assign": True,
            "schedule": {"days": [1, 2, 3, 4, 5], "start": "08:00", "end": "20:00"},
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _local_models(client, app, h) -> None:
    """Own models: a ready file uploaded through the portal, a sharded one and a broken one from the folder, an upload
    in progress."""
    from app.localmodels import service as lm
    from tests.test_local_store import gguf_bytes, later_shard_bytes, put

    local = app.state.local_models.local_dir
    put(local / "big" / "big-Q2_K-00001-of-00002.gguf", gguf_bytes())
    put(local / "big" / "big-Q2_K-00002-of-00002.gguf", later_shard_bytes())
    put(local / "small" / "broken.gguf", b"not a model" * 10)
    content = gguf_bytes()
    r = await client.post(
        "/api/admin/local-models/uploads",
        json={"model": "small", "file": "small-Q8_0.gguf", "size_bytes": len(content)},
        headers=h,
    )
    up = r.json()["id"]
    await client.put(f"/api/admin/local-models/uploads/{up}?offset=0", content=content, headers=h)
    await client.post(f"/api/admin/local-models/uploads/{up}/finish", headers=h)
    async with app.state.sessionmaker() as db:
        await lm.verify_pending(db, app.state.local_models)
        await lm.scan(db, app.state.local_models, app.state.settings)
    r = await client.post(
        "/api/admin/local-models/uploads", json={"model": "next", "file": "next.gguf", "size_bytes": 10_000}, headers=h
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/admin/deployments",
        json={"name": "big", "repo": "local/big", "file": "big-Q2_K-00001-of-00002.gguf", "ctx_per_request": 2048},
        headers=h,
    )
    assert r.status_code == 201, r.text


async def _mirror(app) -> None:
    """The cache of the mirror: a file of the seeded deployment, a sharded model nobody uses, a download in progress and
    the background downloads (queued, one asked twice is one job)."""
    mirror = app.state.mirror_admin = FakeMirrorAdmin([
        cached_file(REPO, FILE, 4 * GiB),
        cached_file("org/big-GGUF", "Q4_K_M/big-Q4_K_M-00001-of-00002.gguf", 40 * GiB),
        cached_file("org/big-GGUF", "Q4_K_M/big-Q4_K_M-00002-of-00002.gguf", 20 * GiB, busy=True),
        cached_file("org/new-GGUF", "new-Q8_0.gguf", 3 * GiB, total=8 * GiB, partial=True, downloading=True, written=3 * GiB,
                    mtime=None, last_served=None),
    ])
    await mirror.prefetch_add("org/new-GGUF", None, ["new-Q8_0.gguf", "mmproj-F16.gguf"])
    mirror.jobs[0].update(state="running", size=8 * GiB, written=3 * GiB, speed_bps=50.0 * 2**20, started=1.0)


async def _key(client, app, h) -> None:
    r = await client.post("/api/keys", json={"alias": "курсовая", "expires_days": 30}, headers=h)
    assert r.status_code == 201, r.text
    app.state.litellm.activity[next(iter(app.state.litellm.users))] = Activity(
        prompt_tokens=1, completion_tokens=1, requests=1, by_day=[DayUsage(date.today(), 2, 1)]
    )


async def _metrics(client, app) -> None:
    """Monitoring: a pushed node sample, a model sample of the controller and the minute rows of a node and of a deployment."""
    from datetime import UTC, datetime, timedelta

    from app.controllers.metrics import model_data
    from app.metrics import live
    from app.metrics.gateway_models import ModelMinute
    from app.metrics.schemas import NodeMetricsIn
    from app.metrics.scrape import Scrape
    from app.models import MetricSample

    now = datetime.now(UTC)
    gpu = {"util_pct": 40, "vram_used_mib": 6000, "vram_total_mib": 12282, "temp_c": 61, "power_w": 120.5}
    node = NodeMetricsIn.model_validate(
        {"gpus": [gpu], "ram_used_mib": 8000, "ram_total_mib": 16000, "cpu_pct": 12.5, "models_bytes": 5, "disk_free_bytes": 9}
    ).sample(now)
    cpu_node = NodeMetricsIn.model_validate({"ram_used_mib": 1, "ram_total_mib": 2, "cpu_pct": 3}).sample(now)
    await live.put(app.state.redis, live.NODE_KEY, "gpu01", node, live.NODE_LIVE_TTL_S)
    await live.put(app.state.redis, live.NODE_KEY, "pc01", cpu_node, live.NODE_LIVE_TTL_S)
    minute = ModelMinute(requests=4, e429=1, e5xx=1)
    minute.ttft[2] = 3
    model = model_data(Scrape(backends=2, up=2, tok_s_gen=40.0, tok_s_prompt=500.0, slots_busy=1, queue=0), minute, True)
    await live.put(app.state.redis, live.MODEL_KEY, "m1", {"at": now.isoformat(), **model}, live.MODEL_LIVE_TTL_S)
    node_row = {k: v for k, v in node.items() if k != "at"}
    async with app.state.sessionmaker() as db:
        for i in range(3):
            at = now - timedelta(minutes=3 - i)
            db.add(MetricSample(at=at, kind="node", name="gpu01", data=node_row))
            db.add(MetricSample(at=at, kind="model", name="m1", data=model))
        await db.commit()


def _cases():
    """fixture file -> (who, "METHOD url"); `{id}` is the seeded deployment."""
    return {
        "get__api_me": ("user", "GET /api/me"),
        "get__api_me--admin": ("admin", "GET /api/me"),
        "get__api_public_status": ("user", "GET /api/public/status"),
        "get__api_models": ("user", "GET /api/models"),
        "get__api_keys": ("user", "GET /api/keys"),
        "post__api_keys": ("user", "POST /api/keys"),
        "get__api_usage": ("user", "GET /api/usage?days=7"),
        "get__api_admin_deployments": ("admin", "GET /api/admin/deployments"),
        "get__api_admin_deployments_id": ("admin", "GET /api/admin/deployments/{id}"),
        "get__api_admin_deployments_id_status": ("admin", "GET /api/admin/deployments/{id}/status"),
        "get__api_admin_deployments_id_command": ("admin", "GET /api/admin/deployments/{id}/command"),
        "get__api_admin_hf_files": ("admin", f"GET /api/admin/hf/files?repo={REPO}"),
        "get__api_admin_hf_estimate": (
            "admin",
            f"GET /api/admin/hf/estimate?repo={REPO}&file={FILE}&ctx_per_request=8192&parallel=1&nodes=pc01,llm-main",
        ),
        "get__api_admin_hf_estimate--split": (
            "admin",
            f"GET /api/admin/hf/estimate?repo={REPO}&file={FILE}&ctx_per_request=8192&parallel=1&group_size=2"
            "&mode=split&nodes=gpu01,gpu02",
        ),
        "get__api_admin_nodes": ("admin", "GET /api/admin/nodes"),
        "get__api_admin_join": ("admin", "GET /api/admin/join"),
        "get__api_admin_status": ("admin", "GET /api/admin/status"),
        "get__api_admin_versions": ("admin", "GET /api/admin/versions"),
        "get__api_admin_users": ("admin", "GET /api/admin/users"),
        "get__api_admin_settings": ("admin", "GET /api/admin/settings"),
        "get__api_admin_users_id_keys": ("admin", "GET /api/admin/users/{uid}/keys"),
        "get__api_admin_audit": ("admin", "GET /api/admin/audit"),
        "get__api_admin_pods_name_logs": ("admin", "GET /api/admin/pods/llm-m1-cpu-abc/logs"),
        "get__api_admin_local_models": ("admin", "GET /api/admin/local-models"),
        "get__api_admin_cache": ("admin", "GET /api/admin/cache"),
        "get__api_admin_prefetch": ("admin", "GET /api/admin/prefetch"),
        "get__api_admin_metrics_live": ("admin", "GET /api/admin/metrics/live"),
        "get__api_admin_metrics_names": ("admin", "GET /api/admin/metrics/names"),
        "get__api_admin_metrics_history": ("admin", "GET /api/admin/metrics/history?kind=node&name=gpu01"),
        "get__api_admin_metrics_history--model": ("admin", "GET /api/admin/metrics/history?kind=model&name=m1"),
    }


CASES = _cases()


def test_every_fixture_has_a_case():
    if not FIXTURES.is_dir():
        pytest.skip("web fixtures are not present")
    assert {p.stem for p in FIXTURES.glob("*.json")} == set(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
async def test_fixture_matches_portal_response(name, client, app):
    path = FIXTURES / f"{name}.json"
    if not path.is_file():
        pytest.skip(f"no fixture {path.name}")
    fixture = json.loads(path.read_text(encoding="utf-8"))
    who, request = CASES[name]
    method, url = request.split(" ", 1)

    if who == "admin":
        h = await _as(client, app, ADMIN)
        dep_id = await _seed_deployment(client, h)
        url = url.replace("{id}", str(dep_id))
    else:
        h = await _as(client, app, USER)
        # the catalog needs a deployment; create it through the repository layer, not the admin API
        async with app.state.sessionmaker() as db:
            db.add(ModelDeployment(name="m1", repo=REPO, file=FILE, ctx_per_request=8192, parallel=2, auto_assign=False))
            await db.commit()
    if name in ("get__api_keys", "get__api_usage", "get__api_admin_users_id_keys"):
        await _key(client, app, h)
    if "{uid}" in url:  # the keys of the admin who is logged in
        url = url.replace("{uid}", (await client.get("/api/me")).json()["id"])
    if name == "get__api_admin_local_models":
        await _local_models(client, app, h)
    if name in ("get__api_admin_cache", "get__api_admin_prefetch"):
        await _mirror(app)
    if name.startswith("get__api_admin_metrics"):
        await _metrics(client, app)

    if method == "POST":
        response = await client.post(url, json={"alias": "диплом", "expires_days": 90}, headers=h)
        assert response.status_code == 201, response.text
    else:
        response = await client.get(url, headers=h)
        assert response.status_code == 200, response.text

    problems = contract_mismatches(fixture, [response.json()])
    known = KNOWN_UI_MISMATCHES.get(name, {})
    unexpected = [p for p in problems if p.split(":", 1)[0] not in known]
    assert not unexpected, f"{name}: " + "; ".join(unexpected)
