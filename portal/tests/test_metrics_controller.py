"""The monitoring controller (task 2-33): the per-minute rows of nodes and deployments, and the retention."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.controllers import runner
from app.controllers.backends import BackendController
from app.controllers.local_models import LocalModelsController
from app.controllers.metrics import MetricsController
from app.controllers.workloads import WorkloadController
from app.metrics import gateway_models, live
from app.metrics.scrape import ModelScraper
from app.models import MetricSample, ModelDeployment, SystemSetting

pytestmark = pytest.mark.docker

NOW = datetime(2026, 11, 4, 10, 30, 20, tzinfo=UTC)
MINUTE = int(NOW.timestamp() // 60)
SPEC = {"repo": "o/r", "file": "f.gguf", "ctx_per_request": 4096, "parallel": 1}


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(MetricSample))
        await db.execute(delete(SystemSetting))
        await db.execute(delete(ModelDeployment))
        db.add(ModelDeployment(name="gemma", **SPEC))
        db.add(ModelDeployment(name="idle", **SPEC))
        await db.commit()
    app.state.backend_status = {"at": "t", "backends": []}


class Clock:
    def __init__(self, t: datetime = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t.timestamp()

    def add(self, **kw):
        self.t += timedelta(**kw)


def controller(app, clock, answers=None):
    ctl = MetricsController(app.state, clock=clock, monotonic=lambda: clock())
    answers = answers if answers is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        text = answers.get(str(request.url))
        return httpx.Response(200, text=text) if text is not None else httpx.Response(503)

    ctl._scraper = ModelScraper("k", transport=httpx.MockTransport(handler))
    return ctl, answers


async def all_rows(app, kind=None):
    async with app.state.sessionmaker() as db:
        q = select(MetricSample).order_by(MetricSample.at, MetricSample.name)
        if kind:
            q = q.where(MetricSample.kind == kind)
        return list((await db.scalars(q)).all())


def llama(gen_tokens, gen_s, busy=0, deferred=0):
    return (
        f"llamacpp:prompt_tokens_total 0\nllamacpp:prompt_seconds_total 0\nllamacpp:tokens_predicted_total {gen_tokens}\n"
        f"llamacpp:tokens_predicted_seconds_total {gen_s}\nllamacpp:requests_processing {busy}\n"
        f"llamacpp:requests_deferred {deferred}\n"
    )


async def put_node(app, name, at, **extra):
    sample = {"at": at.isoformat(), "gpu_util": 40.0, "vram_used_mib": 6000, "ram_used_mib": 100, **extra}
    await live.put(app.state.redis, live.NODE_KEY, name, sample, 120)


async def test_a_row_per_node_with_a_fresh_sample(app):
    clock = Clock()
    ctl, _ = controller(app, clock)
    await put_node(app, "pc01", NOW - timedelta(seconds=15))
    await put_node(app, "pc02", NOW - timedelta(seconds=100))  # silent for too long: no row
    await ctl.reconcile_once()
    rows = await all_rows(app, "node")
    assert [r.name for r in rows] == ["pc01"]
    assert rows[0].at == datetime(2026, 11, 4, 10, 30, tzinfo=UTC)  # the start of the minute
    assert rows[0].data["gpu_util"] == 40.0 and "at" not in rows[0].data


async def test_the_work_is_done_once_per_clock_minute(app):
    clock = Clock()
    ctl, _ = controller(app, clock)
    await put_node(app, "pc01", NOW)
    await ctl.reconcile_once()
    clock.add(seconds=15)  # the controller loop runs every 10 s
    await ctl.reconcile_once()
    clock.add(seconds=15)
    await ctl.reconcile_once()
    assert len(await all_rows(app)) == 1
    clock.add(seconds=30)  # the next minute
    await put_node(app, "pc01", clock.t)
    await ctl.reconcile_once()
    rows = await all_rows(app, "node")
    assert [r.at.minute for r in rows] == [30, 31]


async def test_a_deployment_row_joins_the_scrape_with_the_traffic_of_the_previous_minute(app):
    clock = Clock()
    ctl, answers = controller(app, clock)
    app.state.backend_status = {"at": "t", "backends": [{"model_name": "gemma", "api_base": "http://10.0.0.5:18080/v1"}]}
    answers["http://10.0.0.5:18080/metrics"] = llama(100, 10)
    await ctl.reconcile_once()  # the first scrape has no speed yet
    first = (await all_rows(app, "model"))[0]
    assert first.name == "gemma" and first.data["tok_s_gen"] is None and first.data["backends_up"] == 1

    # the gateway counted the minute that has just ended
    clock.add(minutes=1)
    redis = app.state.redis
    key = gateway_models.bucket_key(MINUTE)
    await redis.hset(key, mapping={"gemma:req": 30, "gemma:429": 2, "gemma:5xx": 1, "gemma:t500": 8, "gemma:t1000": 2})
    await redis.hset(key, mapping={"idle:req": 4, "unknown-model:req": 9})
    answers["http://10.0.0.5:18080/metrics"] = llama(700, 20, busy=2, deferred=3)
    await ctl.reconcile_once()
    rows = await all_rows(app, "model")
    assert [(r.name, r.at.minute) for r in rows] == [("gemma", 30), ("gemma", 31), ("idle", 31)]
    d = rows[1].data
    assert d["tok_s_gen"] == 60.0 and d["slots_busy"] == 2 and d["queue"] == 3
    assert d["rpm"] == 30 and d["e429"] == 2 and d["e5xx"] == 1
    assert 250 <= d["ttft_p50_ms"] <= 500 and 500 < d["ttft_p95_ms"] <= 1000
    assert rows[2].data["rpm"] == 4 and rows[2].data["backends"] == 0 and rows[2].data["tok_s_gen"] is None
    # a name nobody deployed (a client's wrong model in a failed request) has no row
    assert all(r.name != "unknown-model" for r in rows)
    # the live view gets the same numbers
    sample = (await live.read_all(redis, live.MODEL_KEY))["gemma"]
    assert sample["rpm"] == 30 and sample["tok_s_gen"] == 60.0 and sample["at"].startswith("2026-11-04T10:31")


async def test_a_backend_that_does_not_answer_still_gets_a_row_with_nothing_up(app):
    clock = Clock()
    ctl, answers = controller(app, clock)
    app.state.backend_status = {"at": "t", "backends": [{"model_name": "gemma", "api_base": "http://10.0.0.5:18080/v1"}]}
    await ctl.reconcile_once()  # the backend answers 503
    (row,) = await all_rows(app, "model")
    assert row.data["backends"] == 1 and row.data["backends_up"] == 0 and row.data["tok_s_gen"] is None
    assert row.data["rpm"] == 0 and row.data["ttft_p50_ms"] is None  # a minute without traffic is a real zero


async def test_without_redis_counters_the_traffic_fields_stay_empty(app, monkeypatch):
    clock = Clock()
    ctl, answers = controller(app, clock)
    app.state.backend_status = {"at": "t", "backends": [{"model_name": "gemma", "api_base": "http://10.0.0.5:18080/v1"}]}

    async def down(*a, **k):
        raise OSError("redis is down")

    monkeypatch.setattr(app.state.redis, "hgetall", down)
    await ctl.reconcile_once()
    (row,) = await all_rows(app, "model")
    assert row.data["rpm"] is None and row.data["e5xx"] is None and row.data["backends"] == 1


async def test_samples_older_than_the_retention_are_deleted(app):
    clock = Clock()
    ctl, _ = controller(app, clock)
    async with app.state.sessionmaker() as db:
        for days in (1, 6, 8, 30):
            db.add(MetricSample(at=NOW - timedelta(days=days), kind="node", name="pc01", data={}))
        await db.commit()
    await ctl.reconcile_once()
    assert sorted(round((NOW - r.at).total_seconds() / 86400) for r in await all_rows(app)) == [1, 6]


async def test_the_retention_follows_the_setting(app):
    clock = Clock()
    ctl, _ = controller(app, clock)
    async with app.state.sessionmaker() as db:
        db.add(SystemSetting(key="metrics_retention_days", value=2))
        for days in (1, 3):
            db.add(MetricSample(at=NOW - timedelta(days=days), kind="model", name="gemma", data={}))
        await db.commit()
    await ctl.reconcile_once()
    assert [round((NOW - r.at).total_seconds() / 86400) for r in await all_rows(app)] == [1]


async def test_the_prune_runs_every_ten_minutes_not_every_minute(app):
    clock = Clock()
    ctl, _ = controller(app, clock)
    await ctl.reconcile_once()
    async with app.state.sessionmaker() as db:
        db.add(MetricSample(at=NOW - timedelta(days=30), kind="node", name="old", data={}))
        await db.commit()
    clock.add(minutes=2)
    await ctl.reconcile_once()
    assert "old" in {r.name for r in await all_rows(app)}
    clock.add(minutes=9)
    await ctl.reconcile_once()
    assert "old" not in {r.name for r in await all_rows(app)}


async def test_the_controller_is_registered_with_the_others(app, monkeypatch):
    async def idle(self):
        return None

    for cls in (WorkloadController, BackendController, LocalModelsController, MetricsController):
        monkeypatch.setattr(cls, "reconcile_once", idle)
    runner.start(app)
    try:
        assert [t.get_name() for t in app.state.controller_tasks].count("controller-metrics") == 1
    finally:
        await runner.stop(app)


async def test_every_live_node_gets_its_row(app):
    clock = Clock()
    ctl, _ = controller(app, clock)
    for name in ("pc01", "pc02", "pc03"):
        await put_node(app, name, NOW)
    await ctl.reconcile_once()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(MetricSample).where(MetricSample.kind == "node")) == 3


async def test_a_restart_inside_the_minute_does_not_write_the_minute_twice(app):
    clock = Clock()
    await put_node(app, "pc01", NOW)
    first, _ = controller(app, clock)
    await first.reconcile_once()
    second, _ = controller(app, clock)  # a new process of the controllers, the same clock minute
    await second.reconcile_once()
    assert [r.name for r in await all_rows(app, "node")] == ["pc01"]


async def test_a_failed_write_is_tried_again_on_the_next_tick(app, monkeypatch):
    from app.metrics import history

    clock = Clock()
    await put_node(app, "pc01", NOW)
    ctl, _ = controller(app, clock)
    real = history.add_rows
    calls = []

    async def broken(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db down")
        return await real(*args, **kwargs)

    monkeypatch.setattr(history, "add_rows", broken)
    with pytest.raises(RuntimeError):
        await ctl.reconcile_once()
    assert await all_rows(app) == []
    clock.add(seconds=10)  # the next tick of the same minute
    await ctl.reconcile_once()
    assert [r.name for r in await all_rows(app, "node")] == ["pc01"]
