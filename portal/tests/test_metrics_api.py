"""Monitoring API (task 2-33): the node ingest, the live view, history, names and the retention."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.metrics import live
from app.models import MetricSample, SystemSetting
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

AUTH = {"Authorization": "Bearer agent-test"}
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}
GPU = {"util_pct": 40, "vram_used_mib": 6000, "vram_total_mib": 12282, "temp_c": 61, "power_w": 120.5}
BODY = {"gpus": [GPU], "ram_used_mib": 8000, "ram_total_mib": 16000, "cpu_pct": 12.5, "llama_up": True}


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(MetricSample))
        await db.execute(delete(SystemSetting))
        await db.commit()


async def admin_headers(client, app):
    return csrf_headers(await login(client, app, ADMIN))


# ---- the node's push ---------------------------------------------------------------------------------


async def test_a_node_pushes_its_sample_with_the_agent_token(client, app):
    assert (await client.post("/api/nodes/pc01/metrics", json=BODY)).status_code == 401
    assert (await client.post("/api/nodes/pc01/metrics", json=BODY, headers={"Authorization": "Bearer nope"})).status_code == 401
    r = await client.post("/api/nodes/pc01/metrics", json=BODY, headers=AUTH)
    assert r.status_code == 204, r.text
    samples = await live.read_all(app.state.redis, live.NODE_KEY)
    assert set(samples) == {"pc01"}
    assert samples["pc01"]["gpu_util"] == 40 and samples["pc01"]["vram_total_mib"] == 12282 and samples["pc01"]["llama_up"] is True
    assert 0 < await app.state.redis.ttl(live.NODE_KEY + "pc01") <= live.NODE_LIVE_TTL_S


async def test_the_legacy_cluster_token_still_pushes(client, app, settings):
    settings.node_legacy_token = True  # an install upgraded from before the agent token (main's entrypoint sets it)
    r = await client.post("/api/nodes/pc01/metrics", json=BODY, headers={"Authorization": "Bearer tok-test"})
    assert r.status_code == 204


@pytest.mark.parametrize("name", ["PC01", "-pc", "pc_1", "a" * 64])
async def test_a_bad_node_name_is_refused(client, name):
    assert (await client.post(f"/api/nodes/{name}/metrics", json=BODY, headers=AUTH)).status_code == 422


@pytest.mark.parametrize(
    "patch", [{"cpu_pct": 140}, {"gpus": [{**GPU, "util_pct": -1}]}, {"ram_total_mib": "lots"}, {"gpus": "none"}]
)
async def test_a_wrong_payload_is_refused_and_keeps_the_old_sample(client, app, patch):
    assert (await client.post("/api/nodes/pc01/metrics", json=BODY, headers=AUTH)).status_code == 204
    await app.state.redis.delete(live.RATE_KEY + "pc01")
    r = await client.post("/api/nodes/pc01/metrics", json={**BODY, **patch}, headers=AUTH)
    assert r.status_code == 422
    assert (await live.read_all(app.state.redis, live.NODE_KEY))["pc01"]["cpu_pct"] == 12.5


async def test_a_node_that_pushes_too_fast_gets_429_with_retry_after(client, app):
    assert (await client.post("/api/nodes/pc01/metrics", json=BODY, headers=AUTH)).status_code == 204
    r = await client.post("/api/nodes/pc01/metrics", json={**BODY, "cpu_pct": 99}, headers=AUTH)
    assert r.status_code == 429 and r.headers["retry-after"] == "5"
    assert (await live.read_all(app.state.redis, live.NODE_KEY))["pc01"]["cpu_pct"] == 12.5  # the early one is dropped
    # another node has its own gap
    assert (await client.post("/api/nodes/pc02/metrics", json=BODY, headers=AUTH)).status_code == 204
    await app.state.redis.delete(live.RATE_KEY + "pc01")
    assert (await client.post("/api/nodes/pc01/metrics", json={**BODY, "cpu_pct": 99}, headers=AUTH)).status_code == 204


async def test_a_redis_that_is_down_answers_503(client, app, monkeypatch):
    async def down(*a, **k):
        raise OSError("redis is down")

    monkeypatch.setattr(app.state.redis, "set", down)
    r = await client.post("/api/nodes/pc01/metrics", json=BODY, headers=AUTH)
    assert r.status_code == 503


# ---- live ---------------------------------------------------------------------------------------------


async def test_live_is_for_admins_only(client, app):
    assert (await client.get("/api/admin/metrics/live")).status_code == 401
    h = csrf_headers(await login(client, app, USER))
    assert (await client.get("/api/admin/metrics/live", headers=h)).status_code == 403


async def test_live_shows_the_latest_node_and_model_samples(client, app):
    h = await admin_headers(client, app)
    empty = (await client.get("/api/admin/metrics/live", headers=h)).json()
    assert empty["available"] is True and empty["nodes"] == [] and empty["models"] == []
    await client.post("/api/nodes/pc02/metrics", json=BODY, headers=AUTH)
    await client.post("/api/nodes/pc01/metrics", json={**BODY, "cpu_pct": 7}, headers=AUTH)
    await live.put(
        app.state.redis, live.MODEL_KEY, "gemma", {"at": datetime.now(UTC).isoformat(), "rpm": 3, "tok_s_gen": 41.5}, 150
    )
    data = (await client.get("/api/admin/metrics/live", headers=h)).json()
    assert [n["name"] for n in data["nodes"]] == ["pc01", "pc02"]
    pc01 = data["nodes"][0]
    assert pc01["cpu_pct"] == 7 and pc01["gpu_util"] == 40 and pc01["age_s"] in (0, 1, 2) and "at" in pc01
    assert data["models"][0]["name"] == "gemma" and data["models"][0]["tok_s_gen"] == 41.5


async def test_live_says_when_redis_cannot_be_read(client, app, monkeypatch):
    h = await admin_headers(client, app)

    def broken(*a, **k):
        raise OSError("redis is down")

    monkeypatch.setattr(app.state.redis, "scan_iter", broken)
    data = (await client.get("/api/admin/metrics/live", headers=h)).json()
    assert data["available"] is False and data["nodes"] == [] and data["models"] == []


# ---- history ------------------------------------------------------------------------------------------


async def seed(app, kind, name, start, values, field="gpu_util", step=timedelta(minutes=1)):
    async with app.state.sessionmaker() as db:
        for i, v in enumerate(values):
            db.add(MetricSample(at=start + i * step, kind=kind, name=name, data={field: v, "e5xx": 0}))
        await db.commit()


async def test_history_returns_minute_points_of_the_last_hour_by_default(client, app):
    h = await admin_headers(client, app)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    await seed(app, "node", "pc01", now - timedelta(minutes=10), [10, 20, 30])
    await seed(app, "node", "pc02", now - timedelta(minutes=10), [99])
    await seed(app, "node", "pc01", now - timedelta(hours=3), [77])  # older than the default hour
    r = await client.get("/api/admin/metrics/history", params={"kind": "node", "name": "pc01"}, headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["kind"] == "node" and data["name"] == "pc01" and data["step"] == 60
    assert [p["gpu_util"] for p in data["points"]] == [10, 20, 30]
    assert data["points"][0]["t"].startswith((now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M"))


async def test_history_downsamples_to_the_asked_step_and_range(client, app):
    h = await admin_headers(client, app)
    start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    await seed(app, "model", "gemma", start, list(range(60)), field="tok_s_gen")
    params = {
        "kind": "model", "name": "gemma", "from": start.isoformat(), "to": (start + timedelta(hours=1)).isoformat(), "step": 1200,
    }
    data = (await client.get("/api/admin/metrics/history", params=params, headers=h)).json()
    assert data["step"] == 1200 and len(data["points"]) == 3
    assert [p["tok_s_gen"] for p in data["points"]] == [9.5, 29.5, 49.5]  # averages of 20 minutes each


async def test_history_picks_a_step_that_keeps_a_week_drawable(client, app):
    h = await admin_headers(client, app)
    end = datetime(2026, 11, 8, tzinfo=UTC)
    params = {"kind": "node", "name": "pc01", "from": (end - timedelta(days=7)).isoformat(), "to": end.isoformat()}
    data = (await client.get("/api/admin/metrics/history", params=params, headers=h)).json()
    assert data["step"] == 2040 and data["points"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "disk", "name": "x"},
        {"kind": "node"},
        {"kind": "node", "name": ""},
        {"kind": "node", "name": "x", "step": 30},
        {"kind": "node", "name": "x", "step": 999999},
        {"kind": "node", "name": "x", "from": "2026-11-02T00:00:00Z", "to": "2026-11-01T00:00:00Z"},
        {"kind": "node", "name": "x", "from": "2020-01-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"},
        {"kind": "node", "name": "x", "from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z", "step": 60},
    ],
)
async def test_history_refuses_bad_questions(client, app, params):
    h = await admin_headers(client, app)
    assert (await client.get("/api/admin/metrics/history", params=params, headers=h)).status_code == 422


async def test_history_is_for_admins_only(client, app):
    q = {"kind": "node", "name": "pc01"}
    assert (await client.get("/api/admin/metrics/history", params=q)).status_code == 401
    h = csrf_headers(await login(client, app, USER))
    assert (await client.get("/api/admin/metrics/history", params=q, headers=h)).status_code == 403


async def test_names_lists_what_has_history_within_the_retention(client, app):
    assert (await client.get("/api/admin/metrics/names")).status_code == 401
    h = await admin_headers(client, app)
    now = datetime.now(UTC)
    await seed(app, "node", "pc02", now - timedelta(hours=1), [1])
    await seed(app, "node", "pc01", now - timedelta(hours=2), [1, 2])
    await seed(app, "model", "gemma", now - timedelta(hours=1), [1])
    await seed(app, "node", "gone", now - timedelta(days=30), [1])  # beyond the default 7 days
    r = await client.get("/api/admin/metrics/names", headers=h)
    assert r.json() == {"node": ["pc01", "pc02"], "model": ["gemma"]}
