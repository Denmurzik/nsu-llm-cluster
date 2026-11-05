"""Cleanup of model weights on class nodes by the workload controller (task 2-26): which nodes are cleaned, when and of
what, how the answer is waited for, and that the pauses outlive a restart of the controllers."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import fakeredis
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.catalog.gguf import GgufMeta
from app.catalog.hf import HfFile
from app.controllers import workloads as workloads_module
from app.controllers.workloads import WorkloadController
from app.deployments import schedule, service
from app.deployments.service import DeploymentIn
from app.models import ControllerTimer, ModelDeployment, NodeAssignment, NodeModels, NodeWake, PortalFlag, StandaloneNode
from app.nodes import cleanup
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s

pytestmark = pytest.mark.docker

GiB = 2**30
REPO, FILE = "org/model-GGUF", "model-Q4_K_M.gguf"
REPO2, FILE2 = "org/other-GGUF", "other-Q4_K_M.gguf"
# 2026-11-04 is a Wednesday (ISO day 3); 12:00 in Novosibirsk
NOON = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
IDLE_S = 600
RESULT = 'RESULT {"freed_bytes": 7000, "models_bytes": 1234, "removed": ["org__old"], "skipped": [], "dry_run": false}'


def make_hf() -> FakeHf:
    meta = GgufMeta("llama", 32, 32, 8, 128, 4096, 32768)
    return FakeHf(
        files={REPO: [HfFile(FILE, 4 * GiB)], REPO2: [HfFile(FILE2, 4 * GiB)]},
        metas={(REPO, FILE): meta, (REPO2, FILE2): meta},
    )


@pytest.fixture
def settings(settings):
    settings.hf_endpoint = "http://127.0.0.1:9"
    settings.model_cache_idle_s = IDLE_S
    return settings


@pytest_asyncio.fixture(autouse=True)
async def clean_slate(app):
    async with app.state.sessionmaker() as db:
        for table in (NodeModels, NodeWake, ControllerTimer, NodeAssignment, StandaloneNode, ModelDeployment, PortalFlag):
            await db.execute(delete(table))
        await db.commit()


@pytest.fixture
def k8s():
    return FakeK8s()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(workloads_module, "_now", c)
    monkeypatch.setattr(schedule, "now_utc", lambda: NOON)
    return c


@pytest.fixture
def state(app, k8s, settings):
    redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    return SimpleNamespace(settings=settings, sessionmaker=app.state.sessionmaker, k8s=k8s, hf=make_hf(), redis=redis)


@pytest.fixture
def controller(state):
    return WorkloadController(state)


async def at(controller, clock, t):
    clock.now = t
    await controller.reconcile_once()


def add_nodes(k8s, *names, ready=True):
    k8s.add_node("llm-main", is_main=True)
    for n in names:
        k8s.add_node(n, accel="cpu", internal_ip="10.1.2.30", ready=ready)


async def deploy(db_session, name="m1", nodes=("pc01",), repo=REPO, file=FILE, **kw):
    return await service.create(db_session, make_hf(), DeploymentIn(name=name, repo=repo, file=file, nodes=list(nodes), **kw))


async def change(db_session, dep, **fields):
    """Edits the stored deployment directly (the pods are not the point of these tests)."""
    row = await db_session.get(ModelDeployment, dep.id, populate_existing=True)
    for k, v in fields.items():
        setattr(row, k, v)
    await db_session.commit()


def purges(k8s) -> list[tuple[str, list[str]]]:
    """(node, arguments of models-cleanup.sh) of every cleanup Job created."""
    result = []
    for job in k8s.jobs:
        if job["metadata"]["labels"].get("llm.nsu/role") != "purge":
            continue
        pod = job["spec"]["template"]["spec"]
        result.append((pod["nodeName"], pod["containers"][0]["command"][1:]))
    return result


async def timers(app) -> dict[str, tuple[float, str]]:
    async with app.state.sessionmaker() as db:
        return {t.key: (t.since, t.members) for t in (await db.scalars(select(ControllerTimer))).all()}


# ---- a node without a deployment ----------------------------------------------------------------------------------------


async def test_idle_node_is_cleaned_of_everything_after_the_idle_time(app, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    await at(controller, clock, 1000)  # seen without a deployment
    await at(controller, clock, 1000 + IDLE_S - 1)
    assert purges(k8s) == []
    await at(controller, clock, 1000 + IDLE_S)
    assert purges(k8s) == [("pc01", [])]  # no --keep, no --keep-rpc: everything goes
    job = k8s.jobs[0]
    assert job["spec"]["template"]["spec"]["volumes"][0]["hostPath"]["path"] == "/var/lib/llm-models"
    await at(controller, clock, 1000 + IDLE_S + 10)
    assert len(purges(k8s)) == 1  # the Job is running: not issued again


async def test_finished_cleanup_is_remembered_and_not_repeated(app, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    await at(controller, clock, 1000)
    await at(controller, clock, 1000 + IDLE_S)
    k8s.finish_job(k8s.job_names[0], ok=True, log="removed org__old 7000\n" + RESULT)
    await at(controller, clock, 1700)
    for t in (1800, 5000, 90000):
        await at(controller, clock, t)
    assert len(purges(k8s)) == 1
    keys = await timers(app)
    assert "cache:pc01" in keys and "purge:pc01" not in keys
    async with app.state.sessionmaker() as db:  # what the cleanup left is the new size of the volume
        assert (await db.get(NodeModels, "pc01")).models_bytes == 1234


async def test_idle_time_survives_a_restart_of_the_controller(app, k8s, state, clock):
    add_nodes(k8s, "pc01")
    await at(WorkloadController(state), clock, 1000)
    assert (await timers(app))["purge:pc01"][0] == 1000
    fresh = WorkloadController(state)  # the controllers were restarted: the time is in the database
    await at(fresh, clock, 1000 + IDLE_S - 1)
    assert purges(k8s) == []
    await at(fresh, clock, 1000 + IDLE_S)
    assert len(purges(k8s)) == 1


async def test_a_job_handed_over_before_a_restart_is_not_issued_twice(app, k8s, state, clock):
    add_nodes(k8s, "pc01")
    await at(WorkloadController(state), clock, 1000)
    await at(WorkloadController(state), clock, 1000 + IDLE_S)
    await at(WorkloadController(state), clock, 1000 + IDLE_S + 5)
    assert len(purges(k8s)) == 1
    k8s.finish_job(k8s.job_names[0], ok=True, log=RESULT)
    await at(WorkloadController(state), clock, 1000 + IDLE_S + 10)
    await at(WorkloadController(state), clock, 1000 + IDLE_S + 20)
    assert len(purges(k8s)) == 1


async def test_a_node_that_reported_an_empty_volume_needs_no_job(app, db_session, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    db_session.add(NodeModels(node_name="pc01", models_bytes=0))
    await db_session.commit()
    await at(controller, clock, 1000)
    await at(controller, clock, 1000 + IDLE_S)
    assert purges(k8s) == []
    assert "cache:pc01" in await timers(app)


async def test_deployment_deleted_leaves_the_node_idle(db_session, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    dep = await deploy(db_session)
    await at(controller, clock, 1000)  # first sight with a deployment: remembered, nothing to clean
    assert purges(k8s) == []
    await service.delete(db_session, dep.id)
    await at(controller, clock, 1100)
    await at(controller, clock, 1100 + IDLE_S - 1)
    assert purges(k8s) == []
    await at(controller, clock, 1100 + IDLE_S)
    assert purges(k8s) == [("pc01", [])]


async def test_deployment_switched_off_leaves_the_node_idle(db_session, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    dep = await deploy(db_session)
    await at(controller, clock, 1000)
    await change(db_session, dep, enabled=False)
    await at(controller, clock, 1100)
    await at(controller, clock, 1100 + IDLE_S)
    assert purges(k8s) == [("pc01", [])]


async def test_node_taken_off_its_deployment_is_idle_and_back_on_it_is_not(db_session, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    dep = await deploy(db_session)
    await at(controller, clock, 1000)
    await service.assign(db_session, "pc01", None)
    await at(controller, clock, 1100)
    await at(controller, clock, 1100 + IDLE_S - 5)
    await service.assign(db_session, "pc01", dep.id)  # assigned again before the time was up: the files are needed
    await at(controller, clock, 1100 + IDLE_S)
    await at(controller, clock, 99999)
    assert purges(k8s) == []


async def test_a_node_outside_the_working_window_is_not_idle(db_session, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    await deploy(db_session, schedule={"days": [1, 2], "start": "09:00", "end": "18:00"})  # not on Wednesday
    await at(controller, clock, 1000)
    await at(controller, clock, 1000 + 10 * IDLE_S)
    assert purges(k8s) == []


async def test_a_node_on_maintenance_is_not_idle(db_session, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    await deploy(db_session)
    await at(controller, clock, 1000)
    await service.set_maintenance(db_session, "pc01", True)
    await at(controller, clock, 1000 + 10 * IDLE_S)
    assert purges(k8s) == []


async def test_a_node_that_is_down_waits_and_is_cleaned_when_it_is_back(k8s, controller, clock):
    add_nodes(k8s, "pc01", ready=False)
    await at(controller, clock, 1000)
    await at(controller, clock, 1000 + 5 * IDLE_S)
    assert purges(k8s) == []
    k8s.nodes["pc01"].ready = True
    await at(controller, clock, 1000 + 5 * IDLE_S + 10)
    assert len(purges(k8s)) == 1


async def test_a_failed_cleanup_is_tried_again_later(app, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    await at(controller, clock, 1000)
    await at(controller, clock, 1000 + IDLE_S)
    k8s.finish_job(k8s.job_names[0], ok=False, log="models-cleanup: LLAMA_CACHE must be an absolute path")
    await at(controller, clock, 1700)
    await at(controller, clock, 1700 + 60)
    assert len(purges(k8s)) == 1
    assert "cache:pc01" not in await timers(app)
    await at(controller, clock, 1700 + 600)
    assert len(purges(k8s)) == 2


async def test_a_job_killed_at_its_deadline_is_not_remembered_as_cleaned(app, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    await at(controller, clock, 1000)
    await at(controller, clock, 1000 + IDLE_S)
    k8s.kill_job(k8s.job_names[0])  # the image never came: the deadline ends it, status.failed stays 0
    await at(controller, clock, 1700)
    assert "cache:pc01" not in await timers(app)
    await at(controller, clock, 1700 + 600)
    assert len(purges(k8s)) == 2  # tried again, not marked clean


async def test_a_cluster_that_refuses_the_job_is_tried_again_later(k8s, controller, clock, monkeypatch):
    add_nodes(k8s, "pc01")
    create = k8s.create_job

    async def refuse(manifest):
        raise cleanup.K8sUnavailable("fake: refused")

    monkeypatch.setattr(k8s, "create_job", refuse)
    await at(controller, clock, 1000)
    await at(controller, clock, 1000 + IDLE_S)
    monkeypatch.setattr(k8s, "create_job", create)
    await at(controller, clock, 1000 + IDLE_S + 10)
    assert purges(k8s) == []
    await at(controller, clock, 1000 + IDLE_S + 300)
    assert len(purges(k8s)) == 1


async def test_state_of_a_node_that_is_gone_is_dropped(app, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    await at(controller, clock, 1000)
    assert "purge:pc01" in await timers(app)
    del k8s.nodes["pc01"]
    await at(controller, clock, 1100)
    assert not [k for k in await timers(app) if k.startswith(("purge:", "cache:"))]


async def test_the_main_node_is_never_cleaned(k8s, controller, clock):
    k8s.add_node("llm-main", is_main=True)
    await at(controller, clock, 1000)
    await at(controller, clock, 100000)
    assert purges(k8s) == []


# ---- a node whose deployment changed --------------------------------------------------------------------------------------


async def test_a_node_seen_first_with_a_deployment_is_not_cleaned(app, db_session, k8s, controller, clock):
    """Nothing says what it had before: only the signature is remembered (the button covers files from earlier times)."""
    add_nodes(k8s, "pc01")
    await deploy(db_session)
    await at(controller, clock, 1000)
    await at(controller, clock, 100000)
    assert purges(k8s) == []
    assert "cache:pc01" in await timers(app)


async def test_changed_model_cleans_down_to_the_new_one_at_once(db_session, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    dep = await deploy(db_session)
    await at(controller, clock, 1000)
    await change(db_session, dep, repo=REPO2, file=FILE2)
    await at(controller, clock, 1010)
    assert purges(k8s) == [("pc01", ["--keep", f"{REPO2}:{FILE2}"])]
    k8s.finish_job(k8s.job_names[0], ok=True, log=RESULT)
    await at(controller, clock, 1020)
    await at(controller, clock, 1030)
    assert len(purges(k8s)) == 1


async def test_changed_quant_of_the_same_repository_is_cleaned_too(db_session, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    dep = await deploy(db_session)
    await at(controller, clock, 1000)
    await change(db_session, dep, file="model-Q8_0.gguf")
    await at(controller, clock, 1010)
    assert purges(k8s) == [("pc01", ["--keep", f"{REPO}:model-Q8_0.gguf"])]


async def test_a_draft_model_and_a_projector_are_kept_with_the_model(db_session, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    dep = await deploy(db_session)
    await at(controller, clock, 1000)
    await change(db_session, dep, draft_repo="org/draft-GGUF", draft_file="draft.gguf", mmproj_file="mmproj-F16.gguf")
    await at(controller, clock, 1010)
    ((node, args),) = purges(k8s)
    assert node == "pc01"
    assert args == ["--keep", "org/draft-GGUF:draft.gguf", "--keep", f"{REPO}:mmproj-F16.gguf", "--keep", f"{REPO}:{FILE}"]


async def test_a_change_of_something_else_cleans_nothing(db_session, k8s, controller, clock):
    add_nodes(k8s, "pc01")
    dep = await deploy(db_session)
    await at(controller, clock, 1000)
    await change(db_session, dep, ctx_per_request=4096, parallel=1, kv_cache_type="q4_0")
    await at(controller, clock, 1010)
    assert purges(k8s) == []


async def test_a_lent_node_keeps_the_files_of_the_deployment_it_is_lent_to(db_session, k8s, controller, clock):
    """Outside the working window of its deployment the node runs another one: that one's files are in use."""
    add_nodes(k8s, "pc01", "pc02")
    mine = await deploy(db_session, name="mine", schedule={"days": [1, 2], "start": "09:00", "end": "18:00"})
    await deploy(db_session, name="lender", nodes=("pc02",), repo=REPO2, file=FILE2, auto_assign=True)
    await at(controller, clock, 1000)
    await change(db_session, mine, file="model-Q8_0.gguf")
    await at(controller, clock, 1010)
    ((_, args),) = [p for p in purges(k8s) if p[0] == "pc01"]
    assert args == ["--keep", f"{REPO}:model-Q8_0.gguf", "--keep", f"{REPO2}:{FILE2}"]  # its own files and the lender's, sorted


# ---- the tensor cache of split groups --------------------------------------------------------------------------------------


async def split(db_session, k8s, **kw):
    add_nodes(k8s, "pc01", "pc02")
    return await deploy(db_session, name="sp", nodes=("pc01", "pc02"), mode="split", group_size=2, accel="cpu", **kw)


async def test_the_tensor_cache_stays_while_the_node_is_in_a_split_group_of_the_same_model(db_session, k8s, controller, clock):
    dep = await split(db_session, k8s)
    await at(controller, clock, 1000)
    await change(db_session, dep, mmproj_file="mmproj-F16.gguf")  # the files change, the split model does not
    await at(controller, clock, 1010)
    nodes = {n for n, _ in purges(k8s)}
    assert nodes == {"pc01", "pc02"}
    for _, args in purges(k8s):
        assert "--keep-rpc" in args


async def test_a_different_split_model_takes_the_tensor_cache_away(db_session, k8s, controller, clock):
    dep = await split(db_session, k8s)
    await at(controller, clock, 1000)
    await change(db_session, dep, repo=REPO2, file=FILE2)
    await at(controller, clock, 1010)
    assert sorted(purges(k8s)) == [("pc01", ["--keep", f"{REPO2}:{FILE2}"]), ("pc02", ["--keep", f"{REPO2}:{FILE2}"])]


async def test_a_node_that_leaves_split_loses_the_tensor_cache(db_session, k8s, controller, clock):
    dep = await split(db_session, k8s)
    await at(controller, clock, 1000)
    await change(db_session, dep, mode="replicas")
    await at(controller, clock, 1010)
    for _, args in purges(k8s):
        assert "--keep-rpc" not in args and args == ["--keep", f"{REPO}:{FILE}"]


async def test_a_node_that_goes_idle_loses_the_tensor_cache_too(db_session, k8s, controller, clock):
    dep = await split(db_session, k8s)
    await at(controller, clock, 1000)
    await service.delete(db_session, dep.id)
    await at(controller, clock, 1100)
    await at(controller, clock, 1100 + IDLE_S)
    assert sorted(purges(k8s)) == [("pc01", []), ("pc02", [])]


# ---- standalone nodes ----------------------------------------------------------------------------------------------------


def standalone(name="win-07"):
    return StandaloneNode(name=name, url="http://10.0.0.7:18080", accel="cpu", version="t")


async def orders(state, name="win-07") -> list[dict]:
    return [json.loads(x) for x in await state.redis.lrange(cleanup.ORDER_PREFIX + name, 0, -1)]


async def test_idle_standalone_node_gets_an_order_for_its_next_heartbeat(app, db_session, k8s, state, controller, clock):
    k8s.add_node("llm-main", is_main=True)
    db_session.add(standalone())
    await db_session.commit()
    await at(controller, clock, 1000)
    await at(controller, clock, 1000 + IDLE_S - 1)
    assert await orders(state) == []
    await at(controller, clock, 1000 + IDLE_S)
    (order,) = await orders(state)
    assert (order["keep"], order["keep_rpc"], order["keep_vllm"]) == ([], False, False)
    assert k8s.jobs == []
    await at(controller, clock, 1000 + IDLE_S + 15)
    assert len(await orders(state)) == 1  # not queued again while it waits for the answer
    # the node ran it and reported the result with a heartbeat
    await cleanup.store_result(state.redis, "win-07", {"id": order["id"], "freed_bytes": 5, "models_bytes": 77, "removed": ["x"]})
    await at(controller, clock, 1000 + IDLE_S + 30)
    await at(controller, clock, 1000 + IDLE_S + 60)
    assert len(await orders(state)) == 1
    assert "cache:win-07" in await timers(app)
    async with app.state.sessionmaker() as db:
        assert (await db.get(NodeModels, "win-07")).models_bytes == 77


async def test_a_standalone_order_nobody_answers_is_given_up_and_repeated(db_session, k8s, state, controller, clock):
    k8s.add_node("llm-main", is_main=True)
    db_session.add(standalone())
    await db_session.commit()
    await at(controller, clock, 1000)
    await at(controller, clock, 1000 + IDLE_S)
    await state.redis.delete(cleanup.ORDER_PREFIX + "win-07")  # the order expired unread
    await at(controller, clock, 1000 + IDLE_S + cleanup.GIVE_UP_S + 1)
    assert await orders(state) == []  # failed: waits before the next try
    await at(controller, clock, 1000 + IDLE_S + cleanup.GIVE_UP_S + 1 + 600)
    assert len(await orders(state)) == 1


async def test_changed_model_of_a_standalone_node_is_ordered_at_once(db_session, k8s, state, controller, clock):
    k8s.add_node("llm-main", is_main=True)
    db_session.add(standalone())
    await db_session.commit()
    dep = await deploy(db_session, nodes=("win-07",))
    await at(controller, clock, 1000)
    await change(db_session, dep, repo=REPO2, file=FILE2)
    await at(controller, clock, 1010)
    (order,) = await orders(state)
    assert order["keep"] == [f"{REPO2}:{FILE2}"]
