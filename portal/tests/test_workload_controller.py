import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from kubernetes_asyncio.client.exceptions import ApiException
from sqlalchemy import delete, select
from sqlalchemy import update as sql_update

from app.catalog import placement as pl
from app.catalog.gguf import GgufLayout, GgufMeta
from app.catalog.hf import HfFile, VllmInfo
from app.controllers import workloads as workloads_module
from app.controllers.workloads import WorkloadController
from app.deployments import service
from app.deployments.service import DeploymentIn, DeploymentPatch
from app.models import ModelDeployment, NodeAssignment, PortalFlag, StandaloneNode
from app.workloads import builders
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM

pytestmark = pytest.mark.docker

GiB = 2**30
REPO = "org/model-GGUF"
FILE = "model-Q4_K_M.gguf"
VLLM_REPO = "org/vllm-model"


def make_hf() -> FakeHf:
    meta = GgufMeta("llama", 32, 32, 8, 128, 4096, 32768)
    return FakeHf(
        files={REPO: [HfFile(FILE, 4 * GiB)]},
        metas={(REPO, FILE): meta},
        vllm={VLLM_REPO: VllmInfo(10 * GiB, GgufMeta("gemma4", 32, 32, 8, 128, 4096, 16384))},
    )


def dep_in(**kw) -> DeploymentIn:
    return DeploymentIn(**{"name": "m1", "repo": REPO, "file": FILE, **kw})


@pytest.fixture
def settings(settings):
    settings.hf_endpoint = "http://127.0.0.1:9"
    return settings


@pytest_asyncio.fixture(autouse=True)
async def clean_slate(app):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(NodeAssignment))
        await db.execute(delete(StandaloneNode))
        await db.execute(delete(ModelDeployment))
        await db.execute(delete(PortalFlag))
        await db.commit()


@pytest.fixture
def k8s():
    return FakeK8s()


@pytest.fixture(autouse=True)
def rpc_templates(k8s, monkeypatch):
    """An rpc pod a test adds without a spec hash runs the rpc DaemonSet's current template, as one the DaemonSet
    made: the builder records the template hash the controller wants for it."""
    build = builders.rpc_daemonset

    def recording(dep, accel, s, memory_limit_mib=None):
        manifest = build(dep, accel, s, memory_limit_mib)
        k8s.rpc_templates[(dep.name, accel)] = manifest["spec"]["template"]["metadata"]["annotations"]["llm.nsu/spec-hash"]
        return manifest

    monkeypatch.setattr(builders, "rpc_daemonset", recording)


@pytest.fixture
def trace(k8s):
    """Every mutation on the fake cluster in order: ("apply", name), ("delete", name), ("label", node, value),
    ("delete-pod", name), ("force-delete-pod", name)."""
    events: list[tuple] = []
    apply, delete_, patch, delete_pod = k8s.apply, k8s.delete, k8s.patch_node_labels, k8s.delete_pod

    async def traced_apply(manifest):
        events.append(("apply", manifest["metadata"]["name"]))
        await apply(manifest)

    async def traced_delete(kind, name):
        events.append(("delete", name))
        await delete_(kind, name)

    async def traced_patch(name, labels):
        events.append(("label", name, labels["node-restriction.kubernetes.io/llm-deployment"]))
        await patch(name, labels)

    async def traced_delete_pod(name, grace_period_seconds=None):
        events.append(("force-delete-pod" if grace_period_seconds == 0 else "delete-pod", name))
        await delete_pod(name, grace_period_seconds)

    k8s.apply, k8s.delete, k8s.patch_node_labels = traced_apply, traced_delete, traced_patch
    k8s.delete_pod = traced_delete_pod
    return events


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(workloads_module, "_now", c)
    return c


@pytest.fixture
def controller(app, k8s, settings):
    state = SimpleNamespace(settings=settings, sessionmaker=app.state.sessionmaker, k8s=k8s, hf=make_hf())
    return WorkloadController(state)


async def create(db_session, **kw):
    return await service.create(db_session, make_hf(), dep_in(**kw))


def names(k8s) -> set[str]:
    return {name for _, name in k8s.workloads}


def env(k8s, kind, name) -> dict[str, str]:
    container = k8s.workloads[(kind, name)]["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in container["env"] if "value" in e}


def spec_hash(k8s, kind, name) -> str:
    return k8s.workloads[(kind, name)]["spec"]["template"]["metadata"]["annotations"]["llm.nsu/spec-hash"]


async def test_auto_assigns_and_labels_nodes(db_session, k8s, controller):
    dep = await create(db_session, auto_assign=True)
    k8s.add_node("llm-main", is_main=True)
    k8s.add_node("c1", accel="cpu")
    k8s.add_node("g1", accel="nvidia", vram_mib=12000)
    k8s.add_node("down", accel="cpu", ready=False)
    k8s.add_node("bare")  # not labeled with an accelerator yet
    db_session.add(StandaloneNode(name="s1", url="http://10.0.0.7:18080", accel="cpu", version="t"))
    await db_session.commit()

    await controller.reconcile_once()

    rows = {r.node_name: r for r in (await db_session.scalars(select(NodeAssignment).execution_options(populate_existing=True))).all()}
    assert {n: r.deployment_id for n, r in rows.items()} == {"c1": dep.id, "g1": dep.id, "s1": dep.id}
    assert {n: k8s.nodes[n].deployment for n in k8s.nodes} == {
        "llm-main": None, "c1": "m1", "g1": "m1", "down": None, "bare": None,
    }


async def test_creates_daemonset_per_present_accel(db_session, k8s, controller):
    await create(db_session, nodes=["c1", "c2"])
    k8s.add_node("c1", accel="cpu")
    k8s.add_node("c2", accel="cpu")
    await controller.reconcile_once()
    assert names(k8s) == {"llm-m1-cpu"}

    k8s.add_node("g1", accel="nvidia")
    await service.assign(db_session, "g1", (await service.get_by_name(db_session, "m1")).id)
    await controller.reconcile_once()
    assert names(k8s) == {"llm-m1-cpu", "llm-m1-gpu"}
    gpu = k8s.workloads[("DaemonSet", "llm-m1-gpu")]
    assert gpu["spec"]["template"]["spec"]["runtimeClassName"] == "nvidia"

    await service.assign(db_session, "g1", None)  # the last nvidia node leaves: its DaemonSet goes
    await controller.reconcile_once()
    assert names(k8s) == {"llm-m1-cpu"}


async def test_vllm_engine_gets_vllm_daemonset(db_session, k8s, controller):
    await service.create(
        db_session, make_hf(), DeploymentIn(name="v1", engine="vllm", repo=VLLM_REPO, accel="nvidia", nodes=["g1"])
    )
    k8s.add_node("g1", accel="nvidia")
    await controller.reconcile_once()
    assert names(k8s) == {"llm-v1-vllm"}
    args = k8s.workloads[("DaemonSet", "llm-v1-vllm")]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--tool-call-parser" in args  # arch gemma4 comes from the stored meta


async def test_ctx_change_reapplies_with_new_hash(db_session, k8s, controller):
    dep = await create(db_session, nodes=["c1"])
    k8s.add_node("c1", accel="cpu")
    await controller.reconcile_once()
    before = spec_hash(k8s, "DaemonSet", "llm-m1-cpu")
    assert env(k8s, "DaemonSet", "llm-m1-cpu")["CTX_SIZE"] == "16384"

    await service.update(db_session, make_hf(), dep.id, DeploymentPatch(ctx_per_request=4096))
    await controller.reconcile_once()
    assert spec_hash(k8s, "DaemonSet", "llm-m1-cpu") != before
    assert env(k8s, "DaemonSet", "llm-m1-cpu")["CTX_SIZE"] == "8192"
    assert [m["metadata"]["name"] for m in k8s.applied] == ["llm-m1-cpu", "llm-m1-cpu"]


async def test_unchanged_spec_is_not_reapplied(db_session, k8s, controller, trace):
    await create(db_session, nodes=["c1"])
    k8s.add_node("c1", accel="cpu")
    await controller.reconcile_once()
    assert trace == [("label", "c1", "m1"), ("apply", "llm-m1-cpu")]
    trace.clear()
    await controller.reconcile_once()
    await controller.reconcile_once()
    assert trace == []


def add_rpc_pods(k8s, deployment, *nodes, ready=True):
    for n in nodes:
        k8s.add_pod(f"rpc-{n}", n, "rpc", deployment, ready=ready, port=18050)


async def split_setup(db_session, k8s, node_names, group_size=2, accel="cpu", **kw):
    """A split deployment `sp` on cpu nodes; by default a lost member breaks its group at once (`regroup_grace_s=0`).

    accel `any` is written to the database directly: the API refuses it for split today, the controller must still
    match spare nodes to the accel of a group's members, never to the deployment's own."""
    kw.setdefault("regroup_grace_s", 0)
    kw.setdefault("name", "sp")
    dep = await service.create(
        db_session,
        make_hf(),
        dep_in(mode="split", accel="cpu" if accel == "any" else accel, group_size=group_size, nodes=list(node_names), **kw),
    )
    if accel == "any":
        dep.accel = "any"
        await db_session.commit()
    for i, n in enumerate(node_names):
        k8s.add_node(n, accel="cpu", internal_ip=f"10.0.0.{i + 1}")


# ---------- stale standalone records ----------


def standalone(name, seen_s_ago=0.0, accel="cpu"):
    return StandaloneNode(
        name=name, url="http://10.0.0.7:18080", accel=accel, version="t",
        last_seen_at=datetime.now(UTC) - timedelta(seconds=seen_s_ago),
    )


async def test_a_standalone_record_shadowed_by_a_ready_cluster_node_is_purged(db_session, k8s, controller, caplog):
    """A PC moved from standalone to k3s keeps its name: the old record must not make the portal treat the k3s node as
    standalone (a split deployment was refused with «Standalone-нода поддерживает только replicas»)."""
    dep_id = (await create(db_session, nodes=["pc"])).id  # assigned while the PC ran standalone
    db_session.add(standalone("pc", seen_s_ago=3600))
    await db_session.execute(sql_update(NodeAssignment).values(source="standalone"))
    db_session.add(standalone("away", seen_s_ago=3600))  # NotReady twin: only Ready nodes purge at once
    db_session.add(standalone("solo"))
    await db_session.commit()
    k8s.add_node("pc", accel="cpu")
    k8s.add_node("away", accel="cpu", ready=False)
    caplog.set_level(logging.INFO)

    await controller.reconcile_once()

    db_session.expire_all()
    assert {s.name for s in (await db_session.scalars(select(StandaloneNode))).all()} == {"away", "solo"}
    assert (await db_session.get(NodeAssignment, "pc")).source == "k8s"
    assert any("pc" in r.getMessage() and "standalone" in r.getMessage() for r in caplog.records)
    await service.update(db_session, make_hf(), dep_id, DeploymentPatch(mode="split", group_size=2, accel="cpu"))


async def test_a_standalone_record_silent_past_the_purge_age_is_removed(db_session, k8s, controller, settings):
    settings.standalone_purge_s = 600
    db_session.add_all([standalone("old", seen_s_ago=601), standalone("recent", seen_s_ago=599)])
    db_session.add(NodeAssignment(node_name="old", deployment_id=None, source="standalone", manual=True))
    await db_session.commit()

    await controller.reconcile_once()

    db_session.expire_all()
    assert {s.name for s in (await db_session.scalars(select(StandaloneNode))).all()} == {"recent"}
    assert (await db_session.get(NodeAssignment, "old")) is not None  # it registers again and keeps its model


async def test_split_creates_rpc_then_leaders_from_ready_rpc_pods(db_session, k8s, controller):
    await split_setup(db_session, k8s, ["a", "b", "c"])
    await controller.reconcile_once()
    assert names(k8s) == {"llm-sp-rpc-cpu"}  # no rpc pod is ready yet: no leaders

    add_rpc_pods(k8s, "sp", "a", "b")
    add_rpc_pods(k8s, "sp", "c", ready=False)
    await controller.reconcile_once()
    assert names(k8s) == {"llm-sp-rpc-cpu", "llm-sp-g0"}
    leader = k8s.workloads[("Deployment", "llm-sp-g0")]
    terms = leader["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert terms["nodeSelectorTerms"][0]["matchFields"][0]["values"] == ["a"]
    assert env(k8s, "Deployment", "llm-sp-g0")["RPC_SERVERS"] == "127.0.0.1:18050,10.0.0.2:18050"


async def test_split_on_gpu_nodes_places_layers_over_vram_and_ram(db_session, k8s, controller):
    hf = make_hf()
    hf.layouts[(REPO, FILE)] = GgufLayout([GiB] * 32, GiB // 2, GiB // 4, GiB // 2, 1000)
    dep = await service.create(
        db_session, hf,
        dep_in(name="big", mode="split", accel="nvidia", nodes=["g1", "g2"], ctx_per_request=1024, parallel=1),
    )
    for i, n in enumerate(["g1", "g2"]):
        k8s.add_node(n, accel="nvidia", internal_ip=f"10.0.0.{i + 1}", vram_mib=12282, mem_mib=32000, ram_mib=28000)
    await controller.reconcile_once()
    rpc = k8s.workloads[("DaemonSet", "llm-big-rpc-gpu")]["spec"]["template"]["spec"]["containers"][0]
    nodes = [pl.NodeMemory(n, "nvidia", 12282, 28000) for n in ("g1", "g2")]
    limit = pl.rpc_memory_limit_mib(dep.meta, nodes, "nvidia", True, 70)
    assert env(k8s, "DaemonSet", "llm-big-rpc-gpu")["RPC_DEVICES"] == "CUDA0,CPU"
    assert rpc["resources"]["limits"] == {"nvidia.com/gpu": 1, "memory": f"{limit}Mi"}

    add_rpc_pods(k8s, "big", "g1", "g2")
    await controller.reconcile_once()
    plan = pl.plan_split(dep.meta, nodes, 1024, True, 70)
    assert plan.fits and plan.ram_bytes > 0  # 32 GiB of layers do not fit two 12 GB cards
    assert env(k8s, "Deployment", "llm-big-g0")["TENSOR_SPLIT"] == ",".join(map(str, plan.tensor_split))


async def test_split_on_any_accel_puts_a_gpu_node_and_cpu_nodes_in_one_group(db_session, k8s, controller):
    """accel `any`: both rpc DaemonSets run for the deployment (CUDA0,CPU on the GPU node, CPU on the others), the group
    takes the GPU node and the CPU nodes together and the placement goes device by device in --rpc order."""
    hf = make_hf()
    hf.layouts[(REPO, FILE)] = GgufLayout([GiB] * 32, GiB // 2, GiB // 4, GiB // 2, 1000)
    dep = await service.create(
        db_session, hf,
        dep_in(name="mix", mode="split", accel="any", group_size=3, nodes=["g1", "c1", "c2"], ctx_per_request=1024, parallel=1),
    )
    k8s.add_node("g1", accel="nvidia", internal_ip="10.0.0.1", vram_mib=8188, mem_mib=16000, ram_mib=9943)
    k8s.add_node("c1", accel="cpu", internal_ip="10.0.0.2", mem_mib=32000, ram_mib=30000)
    k8s.add_node("c2", accel="cpu", internal_ip="10.0.0.3", mem_mib=32000, ram_mib=30000)
    await controller.reconcile_once()
    assert names(k8s) == {"llm-mix-rpc-gpu", "llm-mix-rpc-cpu"}
    assert env(k8s, "DaemonSet", "llm-mix-rpc-gpu")["RPC_DEVICES"] == "CUDA0,CPU"
    assert env(k8s, "DaemonSet", "llm-mix-rpc-cpu")["RPC_DEVICES"] == "CPU"

    add_rpc_pods(k8s, "mix", "g1", "c1", "c2")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-mix-g0": "g1,c1,c2"}
    assert env(k8s, "Deployment", "llm-mix-g0")["RPC_SERVERS"] == "127.0.0.1:18050,10.0.0.2:18050,10.0.0.3:18050"
    nodes = [pl.NodeMemory("g1", "nvidia", 8188, 9943), pl.NodeMemory("c1", "cpu", None, 30000), pl.NodeMemory("c2", "cpu", None, 30000)]
    plan = pl.plan_split(dep.meta, nodes, 1024, True, 70)
    assert plan.fits and len(plan.tensor_split) == 4  # CUDA0 and CPU of g1, CPU of c1 and of c2
    assert env(k8s, "Deployment", "llm-mix-g0")["TENSOR_SPLIT"] == ",".join(map(str, plan.tensor_split))
    leader = k8s.workloads[("Deployment", "llm-mix-g0")]["spec"]["template"]["spec"]
    assert leader["runtimeClassName"] == "nvidia" and leader["containers"][0]["image"].endswith("-gpu")


@pytest.mark.parametrize(
    ("lost", "spare", "accel", "waits"),
    [("g1", "g2", "nvidia", False), ("g1", "c4", "cpu", True), ("c3", "c4", "cpu", False), ("c3", "g2", "nvidia", True)],
)
async def test_a_lost_member_of_a_mixed_group_is_replaced_at_once_only_by_a_free_node_of_its_accel(
    db_session, k8s, controller, trace, clock, lost, spare, accel, waits
):
    await split_setup(db_session, k8s, ["g1", "c1", "c2", "c3", spare], regroup_grace_s=600, accel="any")
    k8s.nodes["g1"].accel = "nvidia"
    k8s.nodes[spare].accel = accel
    add_rpc_pods(k8s, "sp", "g1", "c1", "c2", "c3")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "c1,c2", "llm-sp-g1": "g1,c3"}  # one accel first, then the rest together

    k8s.pods[f"rpc-{lost}"].info.ready = False
    add_rpc_pods(k8s, "sp", spare)
    trace.clear()
    await controller.reconcile_once()
    if waits:  # a node of the other accel is no stand-in for the lost member: the group waits out the grace
        assert trace == []
    else:
        survivor = "c3" if lost == "g1" else "g1"
        assert trace == [("delete", "llm-sp-g1"), ("delete-pod", f"rpc-{survivor}")]


async def test_split_without_known_memory_leaves_the_placement_to_llama_cpp(db_session, k8s, controller, caplog):
    await service.create(
        db_session, make_hf(), dep_in(name="big", mode="split", accel="nvidia", nodes=["g1", "g2"], use_node_ram=False)
    )
    for i, n in enumerate(["g1", "g2"]):
        k8s.add_node(n, accel="nvidia", internal_ip=f"10.0.0.{i + 1}")  # no llm.nsu/vram-mib label
    add_rpc_pods(k8s, "big", "g1", "g2")
    await controller.reconcile_once()
    await controller.reconcile_once()
    assert "TENSOR_SPLIT" not in env(k8s, "Deployment", "llm-big-g0")
    rpc = k8s.workloads[("DaemonSet", "llm-big-rpc-gpu")]["spec"]["template"]["spec"]["containers"][0]
    assert env(k8s, "DaemonSet", "llm-big-rpc-gpu")["RPC_DEVICES"] == "CUDA0"
    assert rpc["resources"]["limits"] == {"nvidia.com/gpu": 1}  # no RAM device: no memory limit
    assert sum("no layer placement" in r.message for r in caplog.records) == 1  # logged once, not every pass


async def test_a_group_with_ram_that_does_not_fit_gets_no_leader_and_the_status_says_why(db_session, k8s, controller, caplog):
    hf = make_hf()
    hf.layouts[(REPO, FILE)] = GgufLayout([10 * GiB] * 32, GiB, GiB // 4, GiB, 1000)  # 320 GiB: no two PCs hold it
    dep = await service.create(db_session, hf, dep_in(name="big", mode="split", accel="cpu", nodes=["a", "b"]))
    for i, n in enumerate(["a", "b"]):
        k8s.add_node(n, accel="cpu", internal_ip=f"10.0.0.{i + 1}", mem_mib=32000, ram_mib=28000)
    add_rpc_pods(k8s, "big", "a", "b")
    await controller.reconcile_once()
    await controller.reconcile_once()
    assert names(k8s) == {"llm-big-rpc-cpu"}  # no leader: it would fill the RAM until the OOM killer stops it
    assert sum("leader not started" in r.message for r in caplog.records) == 1
    status = await service.deployment_status(db_session, k8s, FakeLiteLLM(), dep)
    (problem,) = status["placement_problems"]
    assert problem.startswith("Группа a, b не запущена: модель не помещается в память нод (нужно ")


async def test_the_status_lists_the_group_in_the_ram_order_the_controller_leads_it_by(db_session, k8s, controller):
    """The node with the most RAM leads a new group (the last GPU node's RAM holds nothing), so a model that does not fit
    the groups in name order can still fit the group the controller forms: the status page must show that group and no
    «не помещается»."""
    hf = make_hf()
    hf.layouts[(REPO, FILE)] = GgufLayout([GiB * 5 // 4] * 32, GiB, GiB // 4, GiB, 1000)  # 40 GiB of layers
    dep = await service.create(
        db_session, hf, dep_in(name="big", mode="split", accel="nvidia", nodes=["a", "b"], ctx_per_request=1024, parallel=1)
    )
    k8s.add_node("a", accel="nvidia", internal_ip="10.0.0.1", vram_mib=12282, mem_mib=32000, ram_mib=20000)
    k8s.add_node("b", accel="nvidia", internal_ip="10.0.0.2", vram_mib=12282, mem_mib=64000, ram_mib=60000)
    by_name = [pl.NodeMemory(n, "nvidia", 12282, r) for n, r in (("a", 20000), ("b", 60000))]
    assert not pl.plan_split(dep.meta, by_name, 1024, True, 70).fits  # a leading: b's RAM holds nothing
    assert pl.plan_split(dep.meta, by_name[::-1], 1024, True, 70).fits
    status = await service.deployment_status(db_session, k8s, FakeLiteLLM(), dep)  # no leader yet: the groups are planned
    assert status["groups"] == [["b", "a"]]
    assert status["placement_problems"] == []
    add_rpc_pods(k8s, "big", "a", "b")
    await controller.reconcile_once()
    await controller.reconcile_once()
    assert env(k8s, "Deployment", "llm-big-g0")["RPC_SERVERS"] == "127.0.0.1:18050,10.0.0.1:18050"  # b leads, a is remote
    status = await service.deployment_status(db_session, k8s, FakeLiteLLM(), dep)
    assert status["groups"] == [["b", "a"]] and status["placement_problems"] == []


async def test_the_rpc_memory_limit_follows_every_node_of_the_accel_not_the_members(db_session, k8s, controller):
    hf = make_hf()
    hf.layouts[(REPO, FILE)] = GgufLayout([GiB] * 32, GiB // 2, GiB // 4, GiB // 2, 1000)
    dep = await service.create(db_session, hf, dep_in(name="big", mode="split", accel="nvidia", nodes=["g1", "g2"]))
    for i, n in enumerate(["g1", "g2"]):
        k8s.add_node(n, accel="nvidia", internal_ip=f"10.0.0.{i + 1}", vram_mib=12282, mem_mib=32000, ram_mib=28000)
    k8s.add_node("g3", accel="nvidia", internal_ip="10.0.0.3", vram_mib=12282, mem_mib=64000, ram_mib=60000)  # not assigned
    await controller.reconcile_once()
    rpc = k8s.workloads[("DaemonSet", "llm-big-rpc-gpu")]["spec"]["template"]["spec"]["containers"][0]
    every = [pl.NodeMemory(n, "nvidia", 12282, r) for n, r in (("g1", 28000), ("g2", 28000), ("g3", 60000))]
    assert rpc["resources"]["limits"]["memory"] == f"{pl.rpc_memory_limit_mib(dep.meta, every, 'nvidia', True, 70)}Mi"
    before = rpc["resources"]["limits"]["memory"]
    await service.assign(db_session, "g3", dep.id)  # g3 joins: the DaemonSet (and every rpc pod) stays as it is
    await controller.reconcile_once()
    rpc = k8s.workloads[("DaemonSet", "llm-big-rpc-gpu")]["spec"]["template"]["spec"]["containers"][0]
    assert rpc["resources"]["limits"]["memory"] == before


def groups(k8s) -> dict[str, str]:
    """Leader name -> its recorded members."""
    return {
        name: m["spec"]["template"]["metadata"]["annotations"]["llm.nsu/group-members"]
        for (kind, name), m in k8s.workloads.items()
        if kind == "Deployment"
    }


async def test_split_regroups_when_node_lost(db_session, k8s, controller, trace):
    await split_setup(db_session, k8s, ["a", "b", "c", "d"])
    add_rpc_pods(k8s, "sp", "a", "b", "c", "d")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}
    g0_hash = k8s.workloads[("Deployment", "llm-sp-g0")]["spec"]["template"]["metadata"]["annotations"]["llm.nsu/group-hash"]

    k8s.pods["rpc-d"].info.ready = False  # d drops out: its group goes, the survivor c gets a fresh rpc server
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("delete", "llm-sp-g1"), ("delete-pod", "rpc-c")]
    add_rpc_pods(k8s, "sp", "c")  # the DaemonSet recreated it

    k8s.pods["rpc-a"].info.ready = False  # a drops out: g0 goes, b is recycled and waits for the next pass
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("delete", "llm-sp-g0"), ("delete-pod", "rpc-b")]
    add_rpc_pods(k8s, "sp", "b")

    trace.clear()
    await controller.reconcile_once()  # b and c are free: they form a new group under the lowest free index
    assert trace == [("apply", "llm-sp-g0")]
    assert groups(k8s) == {"llm-sp-g0": "b,c"}
    leader = k8s.workloads[("Deployment", "llm-sp-g0")]
    assert leader["spec"]["template"]["metadata"]["annotations"]["llm.nsu/group-hash"] != g0_hash
    assert env(k8s, "Deployment", "llm-sp-g0")["RPC_SERVERS"] == "127.0.0.1:18050,10.0.0.3:18050"


async def test_split_one_node_leaving_leaves_the_other_groups_alone(db_session, k8s, controller, trace):
    await split_setup(db_session, k8s, list("abcdef"))
    add_rpc_pods(k8s, "sp", *"abcdef")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d", "llm-sp-g2": "e,f"}

    k8s.pods["rpc-a"].info.ready = False  # a PC is switched off
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("delete", "llm-sp-g0"), ("delete-pod", "rpc-b")]  # g1 and g2 keep running untouched
    assert groups(k8s) == {"llm-sp-g1": "c,d", "llm-sp-g2": "e,f"}

    k8s.pods["rpc-a"].info.ready = True  # it is back, and b has its fresh rpc server
    add_rpc_pods(k8s, "sp", "b")
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("apply", "llm-sp-g0")]
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d", "llm-sp-g2": "e,f"}

    k8s.add_node("g", accel="cpu", internal_ip="10.0.0.7")  # a new node joins: it waits for a partner
    await service.assign(db_session, "g", (await service.get_by_name(db_session, "sp")).id)
    add_rpc_pods(k8s, "sp", "g")
    trace.clear()
    await controller.reconcile_once()
    assert [e for e in trace if e[0] != "label"] == []


async def test_split_membership_change_recycles_rpc_pods_of_all_survivors(db_session, k8s, controller, trace):
    await split_setup(db_session, k8s, ["a", "b", "c", "d"], group_size=3)
    add_rpc_pods(k8s, "sp", "a", "b", "c", "d")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b,c"}

    k8s.pods["rpc-b"].info.ready = False  # the rpc servers of a and c may still wait on the dead connection
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("delete", "llm-sp-g0"), ("delete-pod", "rpc-a"), ("delete-pod", "rpc-c")]
    assert "rpc-d" in k8s.pods  # d was not in the group: its rpc server stays

    add_rpc_pods(k8s, "sp", "a", "c")
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("apply", "llm-sp-g0")] and groups(k8s) == {"llm-sp-g0": "a,c,d"}


async def test_not_ready_node_keeps_its_label_for_the_grace_then_loses_it(db_session, k8s, controller, trace, clock):
    dep = await create(db_session, nodes=["c1", "c2"])
    k8s.add_node("c1", accel="cpu")
    k8s.add_node("c2", accel="cpu")
    await controller.reconcile_once()

    k8s.nodes["c1"].ready = False  # the PC was switched off: a labelled NotReady node stalls the DaemonSet rollout
    trace.clear()
    await controller.reconcile_once()
    clock.now += 119
    await controller.reconcile_once()
    assert trace == [] and k8s.nodes["c1"].deployment == "m1"  # a short outage changes nothing
    clock.now += 1
    await controller.reconcile_once()
    assert trace == [("label", "c1", None)]
    assert names(k8s) == {"llm-m1-cpu"}
    row = await db_session.get(NodeAssignment, "c1", populate_existing=True)
    assert row.deployment_id == dep.id  # the assignment stays: the node gets its model back on return

    k8s.nodes["c1"].ready = True
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("label", "c1", "m1")]

    k8s.nodes["c1"].ready = False  # a new outage starts a new grace period
    clock.now += 60
    trace.clear()
    await controller.reconcile_once()
    clock.now += 60
    await controller.reconcile_once()
    assert trace == [] and k8s.nodes["c1"].deployment == "m1"


async def test_not_ready_node_is_never_given_a_new_label(db_session, k8s, controller, trace, clock):
    first = await create(db_session, nodes=["c1"])
    second = await create(db_session, name="m2")
    k8s.add_node("c1", accel="cpu")
    k8s.add_node("c2", accel="cpu", ready=False)
    await service.assign(db_session, "c2", first.id)
    await controller.reconcile_once()
    assert k8s.nodes["c2"].deployment is None  # assigned while switched off: labelled once it is Ready

    k8s.nodes["c1"].ready = False
    await service.assign(db_session, "c1", second.id)  # reassigned while switched off
    trace.clear()
    await controller.reconcile_once()
    assert trace[0] == ("label", "c1", None)  # the old label goes at once, the new one waits for Ready
    k8s.nodes["c1"].ready = True
    k8s.nodes["c2"].ready = True
    trace.clear()
    await controller.reconcile_once()
    assert ("label", "c1", "m2") in trace and ("label", "c2", "m1") in trace


async def test_disable_and_delete_prune_objects_and_labels(db_session, k8s, controller):
    dep = await create(db_session, nodes=["c1"])
    other = await create(db_session, name="m2", nodes=["c2"])
    k8s.add_node("c1", accel="cpu")
    k8s.add_node("c2", accel="cpu")
    await controller.reconcile_once()
    assert names(k8s) == {"llm-m1-cpu", "llm-m2-cpu"}
    assert (k8s.nodes["c1"].deployment, k8s.nodes["c2"].deployment) == ("m1", "m2")

    await service.update(db_session, make_hf(), dep.id, DeploymentPatch(enabled=False))
    await controller.reconcile_once()
    assert names(k8s) == {"llm-m2-cpu"}
    assert (k8s.nodes["c1"].deployment, k8s.nodes["c2"].deployment) == (None, "m2")

    await service.delete(db_session, other.id)
    await controller.reconcile_once()
    assert names(k8s) == set()
    assert k8s.nodes["c2"].deployment is None


async def test_reassign_node_moves_label(db_session, k8s, controller, trace):
    first = await create(db_session, nodes=["c1"])
    second = await create(db_session, name="m2")
    k8s.add_node("c1", accel="cpu")
    await controller.reconcile_once()
    assert k8s.nodes["c1"].deployment == "m1"

    await service.assign(db_session, "c1", second.id)
    trace.clear()
    await controller.reconcile_once()
    assert k8s.nodes["c1"].deployment == "m2"
    assert names(k8s) == {"llm-m2-cpu"}
    # the old object goes before the new one appears, so the host port is free for the next pod
    assert trace.index(("delete", "llm-m1-cpu")) < trace.index(("apply", "llm-m2-cpu"))
    assert first.id != second.id


async def test_k8s_unavailable_no_changes(db_session, k8s, controller, trace):
    dep = await create(db_session, nodes=["c1"])
    k8s.add_node("c1", accel="cpu")
    await controller.reconcile_once()
    trace.clear()

    k8s.unavailable = True
    await service.update(db_session, make_hf(), dep.id, DeploymentPatch(enabled=False))
    await controller.reconcile_once()  # does not raise
    assert trace == [] and names(k8s) == {"llm-m1-cpu"} and k8s.nodes["c1"].deployment == "m1"

    k8s.unavailable = False

    async def forbidden():
        raise ApiException(status=403, reason="Forbidden")

    k8s.list_owned = forbidden
    await controller.reconcile_once()  # an apiserver error is no reason to delete anything either
    assert [e for e in trace if e[0] == "delete"] == []
    assert names(k8s) == {"llm-m1-cpu"}


async def test_failing_object_does_not_block_the_rest(db_session, k8s, controller):
    await create(db_session, nodes=["c1"])
    await create(db_session, name="m2", nodes=["c2"])
    k8s.add_node("c1", accel="cpu")
    k8s.add_node("c2", accel="cpu")
    apply = k8s.apply

    async def picky(manifest):
        if manifest["metadata"]["name"] == "llm-m1-cpu":
            raise ApiException(status=422, reason="Unprocessable")
        await apply(manifest)

    k8s.apply = picky
    await controller.reconcile_once()
    assert names(k8s) == {"llm-m2-cpu"}


# ---------- operation of the class: regroup grace, maintenance, spare nodes, schedule ----------


async def test_split_group_waits_for_a_lost_member_during_the_regroup_grace(db_session, k8s, controller, trace, clock):
    await split_setup(db_session, k8s, list("abcd"), regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abcd")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}

    k8s.pods["rpc-d"].info.ready = False  # the PC of d reboots
    trace.clear()
    await controller.reconcile_once()
    clock.now += 599
    await controller.reconcile_once()
    assert trace == []  # the group waits for it
    k8s.pods["rpc-d"].info.ready = True  # back within the pause: nothing was rebuilt
    clock.now += 1
    await controller.reconcile_once()
    assert trace == [] and groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}

    k8s.pods["rpc-d"].info.ready = False  # a new outage gets a full pause of its own, then the group breaks up
    await controller.reconcile_once()
    clock.now += 599
    await controller.reconcile_once()
    assert trace == []
    clock.now += 1
    await controller.reconcile_once()
    assert trace == [("delete", "llm-sp-g1"), ("delete-pod", "rpc-c")]
    assert groups(k8s) == {"llm-sp-g0": "a,b"}


async def test_a_spare_that_turns_up_during_the_grace_ends_the_wait(db_session, k8s, controller, trace, clock):
    await split_setup(db_session, k8s, list("abcde"), regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abcd")  # e has no rpc server yet
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}

    k8s.pods["rpc-d"].info.ready = False  # no free node: g1 waits for d
    trace.clear()
    await controller.reconcile_once()
    assert trace == []
    add_rpc_pods(k8s, "sp", "e")  # meanwhile e is ready and forms no group of its own: it takes the place of d
    clock.now += 60
    await controller.reconcile_once()
    assert trace == [("delete", "llm-sp-g1"), ("delete-pod", "rpc-c")]
    add_rpc_pods(k8s, "sp", "c")
    trace.clear()
    await controller.reconcile_once()
    # the old leader and its pods are gone: the index is free again
    assert trace == [("apply", "llm-sp-g1")] and groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,e"}


async def test_a_pair_that_turns_up_during_the_grace_forms_a_group_of_its_own(db_session, k8s, controller, trace, clock):
    await split_setup(db_session, k8s, list("abcdef"), regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abcd")  # e and f have no rpc server yet
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}

    k8s.pods["rpc-d"].info.ready = False
    trace.clear()
    await controller.reconcile_once()
    add_rpc_pods(k8s, "sp", "e", "f")  # a new group, not a spare: g1 keeps waiting for d
    clock.now += 60
    await controller.reconcile_once()
    assert trace == [("apply", "llm-sp-g2")]
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d", "llm-sp-g2": "e,f"}


async def test_split_leader_back_after_an_outage_restarts_the_other_rpc_servers(db_session, k8s, controller, trace, clock):
    await split_setup(db_session, k8s, list("abc"), group_size=3, regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abc")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b,c"}

    k8s.pods["rpc-a"].info.ready = False  # the leader's PC reboots: b and c may still wait on its old connection
    trace.clear()
    await controller.reconcile_once()
    assert trace == []
    k8s.pods["rpc-a"].info.ready = True
    clock.now += 60
    await controller.reconcile_once()
    assert trace == [("delete-pod", "rpc-b"), ("delete-pod", "rpc-c")]  # the group itself stays

    trace.clear()
    await controller.reconcile_once()  # their fresh rpc servers are starting: the group waits, as for any member
    add_rpc_pods(k8s, "sp", "b", "c")
    await controller.reconcile_once()
    assert trace == [] and groups(k8s) == {"llm-sp-g0": "a,b,c"}


async def test_split_node_keeps_its_label_for_the_split_default_grace(db_session, k8s, controller, trace, clock):
    await split_setup(db_session, k8s, ["a", "b"], regroup_grace_s=None)  # the split default: 600 s
    add_rpc_pods(k8s, "sp", "a", "b")
    await controller.reconcile_once()

    k8s.nodes["a"].ready = False
    k8s.pods["rpc-a"].info.ready = False
    trace.clear()
    await controller.reconcile_once()
    clock.now += 599
    await controller.reconcile_once()
    assert trace == [] and k8s.nodes["a"].deployment == "sp"
    clock.now += 1
    await controller.reconcile_once()
    assert trace == [("label", "a", None), ("delete", "llm-sp-g0"), ("delete-pod", "rpc-b")]


async def test_replicas_node_label_follows_the_deployment_grace(db_session, k8s, controller, trace, clock):
    await create(db_session, nodes=["c1"], regroup_grace_s=300)
    k8s.add_node("c1", accel="cpu")
    await controller.reconcile_once()
    k8s.nodes["c1"].ready = False
    trace.clear()
    await controller.reconcile_once()
    clock.now += 299
    await controller.reconcile_once()
    assert trace == []
    clock.now += 1
    await controller.reconcile_once()
    assert trace == [("label", "c1", None)]


async def test_maintenance_takes_the_node_off_at_once_and_back_afterwards(db_session, k8s, controller, trace):
    await split_setup(db_session, k8s, list("abcd"), regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abcd")
    await controller.reconcile_once()

    await service.set_maintenance(db_session, "d", True)  # no pause: the admin took it off on purpose
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("label", "d", None), ("delete", "llm-sp-g1"), ("delete-pod", "rpc-c")]
    row = await db_session.get(NodeAssignment, "d", populate_existing=True)
    assert row.deployment_id is not None and row.maintenance

    await service.set_maintenance(db_session, "d", False)
    add_rpc_pods(k8s, "sp", "c")
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("label", "d", "sp"), ("apply", "llm-sp-g1")]
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}


async def test_spare_nodes_rebuild_a_broken_group_in_the_same_pass(db_session, k8s, controller, trace):
    await split_setup(db_session, k8s, list("abcdef"), spare_nodes=2)
    add_rpc_pods(k8s, "sp", *"abcdef")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}  # e and f are held in reserve

    k8s.pods["rpc-a"].info.ready = False
    trace.clear()
    await controller.reconcile_once()
    # the spare pair forms a new group under a fresh index: the old leader object is not rewritten
    assert trace == [("delete", "llm-sp-g0"), ("delete-pod", "rpc-b"), ("apply", "llm-sp-g2")]
    assert groups(k8s) == {"llm-sp-g1": "c,d", "llm-sp-g2": "e,f"}  # g1 was not touched

    k8s.pods["rpc-a"].info.ready = True  # a and b are back: they become the new reserve
    add_rpc_pods(k8s, "sp", "b")
    trace.clear()
    await controller.reconcile_once()
    assert trace == [] and groups(k8s) == {"llm-sp-g1": "c,d", "llm-sp-g2": "e,f"}


async def test_spare_nodes_replace_a_node_taken_off_for_maintenance(db_session, k8s, controller, trace):
    await split_setup(db_session, k8s, list("abcdef"), spare_nodes=2)
    add_rpc_pods(k8s, "sp", *"abcdef")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}

    # the node still belongs to the deployment's roster: the number of groups stays, the reserve covers it
    await service.set_maintenance(db_session, "a", True)
    trace.clear()
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g1": "c,d", "llm-sp-g2": "e,f"}
    assert ("delete-pod", "rpc-b") in trace


def add_leader_pod(k8s, index, node, ready=True, message=None, deployment="sp") -> str:
    """The pod of the leader Deployment `llm-<deployment>-g<index>`, named the way a ReplicaSet names it."""
    name = f"llm-{deployment}-g{index}-7c9d8f-{node}x"
    workload = f"llm-{deployment}-g{index}"
    k8s.add_pod(name, node, "split-leader", deployment, ready=ready, message=message, workload=workload)
    return name


async def test_a_lost_leader_on_a_down_node_is_force_deleted_and_its_group_rebuilt_under_a_fresh_index(
    db_session, k8s, controller, trace
):
    await split_setup(db_session, k8s, list("abcdef"), spare_nodes=2)
    add_rpc_pods(k8s, "sp", *"abcdef")
    await controller.reconcile_once()
    old_leader = add_leader_pod(k8s, 0, "a")
    add_leader_pod(k8s, 1, "c")

    k8s.nodes["a"].ready = False  # the leader's PC is switched off: its pod would hang in Terminating
    k8s.pods["rpc-a"].info.ready = False
    trace.clear()
    await controller.reconcile_once()
    assert trace == [
        ("label", "a", None),
        ("delete", "llm-sp-g0"),
        ("force-delete-pod", old_leader),
        ("delete-pod", "rpc-b"),
        ("apply", "llm-sp-g2"),  # a new leader object: never a rewrite stuck behind the old pod (Recreate)
    ]
    assert old_leader not in k8s.pods
    assert groups(k8s) == {"llm-sp-g1": "c,d", "llm-sp-g2": "e,f"}


async def test_an_index_stays_reserved_while_its_old_leader_pod_exists(db_session, k8s, controller, trace):
    await split_setup(db_session, k8s, list("abc"))
    add_rpc_pods(k8s, "sp", *"abc")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b"}

    k8s.pods["rpc-b"].info.ready = False
    await controller.reconcile_once()  # g0 goes, a is recycled
    assert groups(k8s) == {}
    old_leader = add_leader_pod(k8s, 0, "a")  # its pod is still terminating
    k8s.pods[old_leader].terminating = True
    add_rpc_pods(k8s, "sp", "a", "b")
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("apply", "llm-sp-g1")] and groups(k8s) == {"llm-sp-g1": "a,b"}

    del k8s.pods[old_leader]  # gone now: g1 stays as it is
    trace.clear()
    await controller.reconcile_once()
    assert trace == []


async def test_a_free_spare_replaces_a_lost_member_without_waiting_for_the_grace(
    db_session, k8s, controller, trace, clock
):
    await split_setup(db_session, k8s, list("abcdef"), spare_nodes=2, regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abcdef")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}

    k8s.pods["rpc-a"].info.ready = False
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("delete", "llm-sp-g0"), ("delete-pod", "rpc-b"), ("apply", "llm-sp-g2")]
    assert groups(k8s) == {"llm-sp-g1": "c,d", "llm-sp-g2": "e,f"}

    # no spare is free now (a is down, b restarts its rpc server): g1 waits out the pause for d
    k8s.pods["rpc-d"].info.ready = False
    trace.clear()
    await controller.reconcile_once()
    clock.now += 599
    await controller.reconcile_once()
    assert trace == []
    clock.now += 1
    await controller.reconcile_once()
    assert trace == [("delete", "llm-sp-g1"), ("delete-pod", "rpc-c")]


async def test_a_free_spare_replaces_a_lost_member_of_a_deployment_on_any_accel(
    db_session, k8s, controller, trace, clock
):
    await split_setup(db_session, k8s, list("abcdef"), spare_nodes=2, regroup_grace_s=600, accel="any")
    add_rpc_pods(k8s, "sp", *"abcdef")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}

    k8s.pods["rpc-a"].info.ready = False  # e and f are free cpu nodes: no pause
    trace.clear()
    await controller.reconcile_once()
    assert trace == [("delete", "llm-sp-g0"), ("delete-pod", "rpc-b"), ("apply", "llm-sp-g2")]
    assert groups(k8s) == {"llm-sp-g1": "c,d", "llm-sp-g2": "e,f"}


async def test_a_free_spare_of_another_accel_does_not_end_the_wait(db_session, k8s, controller, trace, clock):
    await split_setup(db_session, k8s, list("abcde"), regroup_grace_s=600, accel="any")
    k8s.nodes["e"].accel = "nvidia"  # a GPU node cannot take the place of a cpu member
    add_rpc_pods(k8s, "sp", *"abcde")
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}

    k8s.pods["rpc-d"].info.ready = False
    trace.clear()
    await controller.reconcile_once()
    clock.now += 599
    await controller.reconcile_once()
    assert trace == []
    clock.now += 1
    await controller.reconcile_once()
    assert trace == [("delete", "llm-sp-g1"), ("delete-pod", "rpc-c")]


async def test_a_lost_leader_with_a_truncated_pod_name_is_still_force_deleted(db_session, k8s, controller, trace):
    long = "x" * 40  # the longest deployment name: the ReplicaSet's generateName is cut to 58 characters
    await split_setup(db_session, k8s, list("abcdef"), spare_nodes=2, name=long)
    add_rpc_pods(k8s, long, *"abcdef")
    await controller.reconcile_once()
    leader = f"llm-{long}-g0-7c9d8f6b5"[:58] + "x7k2p"
    k8s.add_pod(leader, "a", "split-leader", long, workload=f"llm-{long}-g0")

    k8s.nodes["a"].ready = False
    k8s.pods["rpc-a"].info.ready = False
    trace.clear()
    await controller.reconcile_once()
    assert ("force-delete-pod", leader) in trace
    assert leader not in k8s.pods


async def test_a_held_group_rejoined_by_a_member_restarts_a_crashed_leader(db_session, k8s, controller, trace, clock):
    await split_setup(db_session, k8s, list("abcd"), regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abcd")
    await controller.reconcile_once()
    add_leader_pod(k8s, 0, "a")
    crashed = add_leader_pod(k8s, 1, "c")

    k8s.pods["rpc-d"].info.ready = False  # the leader of g1 lost its rpc connection and crash-loops
    k8s.pods["rpc-b"].info.ready = False  # the leader of g0 kept running
    k8s.pods[crashed].info.ready = False
    k8s.pods[crashed].info.message = "CrashLoopBackOff: back-off 5m0s restarting failed container"
    trace.clear()
    await controller.reconcile_once()
    assert trace == []

    clock.now += 60
    k8s.pods["rpc-d"].info.ready = True
    k8s.pods["rpc-b"].info.ready = True
    await controller.reconcile_once()
    assert trace == [("delete-pod", crashed)]  # no five-minute back-off; the healthy leader of g0 is left alone
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}


def restart_rpc_pod(k8s, node, started_at, deployment="sp"):
    """The rpc pod of `node` replaced by a fresh one (a new container started at `started_at`)."""
    k8s.add_pod(f"rpc-{node}", node, "rpc", deployment, port=18050, started_at=started_at)


async def test_a_short_outage_of_the_leaders_node_leaves_a_running_leader_and_its_rpc_servers_alone(
    db_session, k8s, controller, trace, clock
):
    """A NotReady blip (a lost lease, a slow probe under load): the leader ran all along, its sessions are alive."""
    await split_setup(db_session, k8s, list("abc"), group_size=3, regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abc")
    await controller.reconcile_once()
    leader = add_leader_pod(k8s, 0, "a")
    k8s.pods[leader].info.started_at = clock.now - 500

    k8s.pods["rpc-a"].info.ready = False
    trace.clear()
    await controller.reconcile_once()
    k8s.pods["rpc-a"].info.ready = True
    clock.now += 60
    await controller.reconcile_once()
    assert trace == [] and groups(k8s) == {"llm-sp-g0": "a,b,c"}


async def test_a_leader_restarted_after_the_outage_of_its_node_restarts_the_rpc_servers_that_ran_through_it(
    db_session, k8s, controller, trace, clock
):
    await split_setup(db_session, k8s, list("abc"), group_size=3, regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abc")
    await controller.reconcile_once()
    leader = add_leader_pod(k8s, 0, "a")
    k8s.pods[leader].info.started_at = clock.now - 500

    k8s.pods["rpc-a"].info.ready = False  # the leader's PC reboots (the loss is seen at 1000)
    trace.clear()
    await controller.reconcile_once()
    clock.now += 300
    k8s.pods[leader].info.started_at = 1290  # its leader started again with the node
    restart_rpc_pod(k8s, "a", 1290)
    restart_rpc_pod(k8s, "c", 1150)  # c's server was restarted meanwhile: it waits on no old connection
    await controller.reconcile_once()
    assert trace == [("delete-pod", "rpc-b")]


async def test_after_the_members_are_recycled_the_second_pass_relaunches_the_leader(
    db_session, k8s, controller, trace, clock
):
    """The leader came back with its node and the other rpc servers were recycled: their new servers started after the
    leader connected, so the leader holds dead connections and is relaunched once they are back."""
    await split_setup(db_session, k8s, list("abc"), group_size=3, regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abc")
    await controller.reconcile_once()
    leader = add_leader_pod(k8s, 0, "a")
    k8s.pods[leader].info.started_at = clock.now - 500

    k8s.pods["rpc-a"].info.ready = False  # the leader's PC reboots (the loss is seen at 1000)
    await controller.reconcile_once()
    clock.now += 300
    k8s.pods[leader].info.started_at = 1290
    restart_rpc_pod(k8s, "a", 1290)
    trace.clear()
    await controller.reconcile_once()  # first pass: b and c ran through the outage and are recycled
    assert trace == [("delete-pod", "rpc-b"), ("delete-pod", "rpc-c")]

    await controller.reconcile_once()  # their pods are gone: the group misses members again
    clock.now += 40
    restart_rpc_pod(k8s, "b", clock.now - 5)
    restart_rpc_pod(k8s, "c", clock.now - 5)
    trace.clear()
    await controller.reconcile_once()  # second pass: the new servers started after the leader did
    assert trace == [("delete-pod", leader)]


async def test_starts_within_the_clock_skew_count_as_restarts(db_session, k8s, controller, trace, clock):
    """The nodes' clocks may be off from main's: a doubtful start restarts rather than leaves a group stuck."""
    await split_setup(db_session, k8s, list("abc"), group_size=3, regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abc")
    await controller.reconcile_once()
    leader = add_leader_pod(k8s, 0, "a")
    k8s.pods[leader].info.started_at = clock.now - 500

    k8s.pods["rpc-a"].info.ready = False  # the loss is seen at 1000
    trace.clear()
    await controller.reconcile_once()
    clock.now += 300
    k8s.pods[leader].info.started_at = 940  # the node's clock is 100 s behind main's: it restarted after the loss
    restart_rpc_pod(k8s, "a", 940)
    restart_rpc_pod(k8s, "c", 1100)  # maybe before the loss, maybe after: recreated
    await controller.reconcile_once()
    assert trace == [("delete-pod", "rpc-b"), ("delete-pod", "rpc-c"), ("delete-pod", leader)]  # c may have dropped it


async def test_a_member_whose_rpc_server_restarted_restarts_the_leader_that_ran_through_it(
    db_session, k8s, controller, trace, clock
):
    """The leader holds a dead connection to the old server: its first request would end in GGML_ABORT."""
    await split_setup(db_session, k8s, list("abcd"), regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abcd")
    for n in "abcd":
        k8s.pods[f"rpc-{n}"].info.started_at = clock.now - 500
    await controller.reconcile_once()
    g0 = add_leader_pod(k8s, 0, "a")
    g1 = add_leader_pod(k8s, 1, "c")
    for p in (g0, g1):
        k8s.pods[p].info.started_at = clock.now - 400

    k8s.pods["rpc-b"].info.ready = False  # a blip: the same server comes back
    k8s.pods["rpc-d"].info.ready = False  # the PC of d reboots: a new server
    trace.clear()
    await controller.reconcile_once()
    clock.now += 60
    k8s.pods["rpc-b"].info.ready = True
    restart_rpc_pod(k8s, "d", clock.now - 10)
    await controller.reconcile_once()
    assert trace == [("delete-pod", g1)]
    assert groups(k8s) == {"llm-sp-g0": "a,b", "llm-sp-g1": "c,d"}


async def test_the_leaders_of_held_groups_are_named_for_the_backend_controller(db_session, k8s, controller, clock):
    await split_setup(db_session, k8s, list("abcd"), regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abcd")
    await controller.reconcile_once()
    assert controller.app_state.held_split_leaders == set()

    k8s.pods["rpc-d"].info.ready = False
    await controller.reconcile_once()
    assert controller.app_state.held_split_leaders == {"llm-sp-g1"}
    k8s.pods["rpc-d"].info.ready = True
    await controller.reconcile_once()
    assert controller.app_state.held_split_leaders == set()


async def test_a_group_whose_rpc_servers_are_restarted_is_named_for_the_backend_controller(
    db_session, k8s, controller, clock
):
    await split_setup(db_session, k8s, list("abc"), group_size=3, regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abc")
    await controller.reconcile_once()
    k8s.pods["rpc-a"].info.ready = False  # no leader pod runs: the group is restarted when a is back
    await controller.reconcile_once()
    k8s.pods["rpc-a"].info.ready = True
    await controller.reconcile_once()
    assert controller.app_state.held_split_leaders == {"llm-sp-g0"}


async def test_rpc_servers_of_an_older_template_form_no_group(db_session, k8s, controller, trace):
    """A rolling update of the rpc DaemonSet: a leader would get a --tensor-split against old and new servers."""
    await split_setup(db_session, k8s, list("abcd"))
    for n in "abcd":
        k8s.add_pod(f"rpc-{n}", n, "rpc", "sp", port=18050, spec_hash="old")
    await controller.reconcile_once()
    assert groups(k8s) == {}

    add_rpc_pods(k8s, "sp", "a", "b")  # recreated from the current template
    await controller.reconcile_once()
    assert groups(k8s) == {"llm-sp-g0": "a,b"}

    k8s.pods["rpc-a"].spec_hash = "older"  # a group on servers of another template breaks up
    trace.clear()
    await controller.reconcile_once()
    assert ("delete", "llm-sp-g0") in trace


async def test_the_regroup_grace_survives_a_restart_of_the_controllers(db_session, k8s, controller, trace, clock):
    await split_setup(db_session, k8s, list("abcd"), regroup_grace_s=600)
    add_rpc_pods(k8s, "sp", *"abcd")
    await controller.reconcile_once()
    k8s.pods["rpc-d"].info.ready = False
    trace.clear()
    await controller.reconcile_once()

    clock.now += 599
    restarted = WorkloadController(controller.app_state)
    await restarted.reconcile_once()
    assert trace == []  # the pause did not start over
    clock.now += 1
    await restarted.reconcile_once()
    assert trace == [("delete", "llm-sp-g1"), ("delete-pod", "rpc-c")]


async def test_the_not_ready_grace_survives_a_restart_of_the_controllers(db_session, k8s, controller, trace, clock):
    await create(db_session, nodes=["c1"], regroup_grace_s=300)
    k8s.add_node("c1", accel="cpu")
    await controller.reconcile_once()
    k8s.nodes["c1"].ready = False
    trace.clear()
    await controller.reconcile_once()
    clock.now += 299
    await WorkloadController(controller.app_state).reconcile_once()
    assert trace == []
    clock.now += 1
    await WorkloadController(controller.app_state).reconcile_once()
    assert trace == [("label", "c1", None)]


WORKDAYS = {"days": [1, 2, 3, 4, 5], "start": "08:00", "end": "20:00"}


async def test_schedule_switches_the_deployment_and_lends_its_nodes(db_session, k8s, controller, monkeypatch):
    from datetime import UTC, datetime

    from app.deployments import schedule

    now = {"t": datetime(2026, 11, 6, 6, 0, tzinfo=UTC)}  # Friday 13:00 in Novosibirsk
    monkeypatch.setattr(schedule, "now_utc", lambda: now["t"])
    day = await create(db_session, name="day", schedule=WORKDAYS, nodes=["c1"])
    await create(db_session, name="evening", auto_assign=True)
    k8s.add_node("c1", accel="cpu")
    await controller.reconcile_once()
    assert names(k8s) == {"llm-day-cpu"} and k8s.nodes["c1"].deployment == "day"

    now["t"] = datetime(2026, 11, 6, 14, 0, tzinfo=UTC)  # Friday 21:00: the window is closed
    await controller.reconcile_once()
    assert names(k8s) == {"llm-evening-cpu"} and k8s.nodes["c1"].deployment == "evening"
    row = await db_session.get(NodeAssignment, "c1", populate_existing=True)
    assert row.deployment_id == day.id  # only lent: the assignment stays

    now["t"] = datetime(2026, 11, 9, 1, 0, tzinfo=UTC)  # Monday 08:00: back to its own deployment
    await controller.reconcile_once()
    assert names(k8s) == {"llm-day-cpu"} and k8s.nodes["c1"].deployment == "day"


async def test_the_repos_of_every_deployment_are_published_for_the_nodes_cache_cleanup(db_session, k8s, controller, trace):
    await create(db_session, name="m1")
    other = await service.create(db_session, make_hf(), DeploymentIn(name="v1", engine="vllm", repo=VLLM_REPO, accel="nvidia"))
    await service.update(db_session, make_hf(), other.id, DeploymentPatch(enabled=False))
    k8s.add_node("a", accel="cpu")
    await controller.reconcile_once()
    # a disabled deployment keeps its files: it may be switched on again
    assert k8s.config_maps["llm-model-repos"] == {"repos": "org/model-GGUF,org/vllm-model"}

    writes = len(k8s.config_map_writes)
    await controller.reconcile_once()
    assert len(k8s.config_map_writes) == writes  # written again only when the list changes

    await service.delete(db_session, other.id)
    await controller.reconcile_once()
    assert k8s.config_maps["llm-model-repos"] == {"repos": "org/model-GGUF"}


async def test_the_draft_and_own_projector_repos_are_published_too(db_session, k8s, controller):
    row = await create(db_session, name="m1")
    row.draft_repo, row.draft_file, row.mmproj_file = "org/draft-GGUF", "d.gguf", "local/vision/p.gguf"
    await db_session.commit()
    k8s.add_node("a", accel="cpu")
    await controller.reconcile_once()
    # the nodes' cache cleanup must not delete the draft model that this deployment downloads
    assert k8s.config_maps["llm-model-repos"] == {"repos": "local/vision,org/draft-GGUF,org/model-GGUF"}


async def test_the_repo_list_is_written_again_after_a_refused_write(db_session, k8s, controller):
    await create(db_session, name="m1")
    put = k8s.put_config_map

    async def refused(name, data):
        raise ApiException(status=500, reason="boom")

    k8s.put_config_map = refused
    await controller.reconcile_once()
    assert "llm-model-repos" not in k8s.config_maps
    k8s.put_config_map = put
    await controller.reconcile_once()
    assert k8s.config_maps["llm-model-repos"] == {"repos": "org/model-GGUF"}


async def test_class_nodes_get_the_class_taint_once_and_main_never(db_session, k8s, controller):
    """Nodes that joined before the agents set `--node-taint` get it from the controller, so system pods stay on main."""
    k8s.add_node("llm-main", is_main=True)
    k8s.add_node("c1", accel="cpu")
    k8s.add_node("c2", accel="nvidia", vram_mib=12000)
    await controller.reconcile_once()
    assert sorted(k8s.tainted_nodes) == ["c1", "c2"]
    assert not k8s.nodes["llm-main"].class_taint
    await controller.reconcile_once()
    assert sorted(k8s.tainted_nodes) == ["c1", "c2"]  # already tainted: no second patch


async def test_a_refused_taint_does_not_block_the_rest(db_session, k8s, controller):
    await create(db_session, nodes=["c1"])
    k8s.add_node("c1", accel="cpu")

    async def conflict(name):
        raise ApiException(status=409, reason="Conflict")

    k8s.add_class_taint = conflict
    await controller.reconcile_once()
    assert names(k8s) == {"llm-m1-cpu"} and k8s.nodes["c1"].deployment == "m1"


async def test_the_end_of_the_window_drains_the_pods_before_they_go(db_session, k8s, controller, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.deployments import schedule

    end = datetime(2026, 11, 6, 13, 0, tzinfo=UTC)  # Friday 20:00 in Novosibirsk
    now = {"t": end - timedelta(hours=1)}
    monkeypatch.setattr(schedule, "now_utc", lambda: now["t"])
    await create(db_session, name="day", schedule=WORKDAYS, nodes=["c1"])
    await create(db_session, name="evening", auto_assign=True)
    k8s.add_node("c1", accel="cpu")
    await controller.reconcile_once()
    assert names(k8s) == {"llm-day-cpu"}

    now["t"] = end + timedelta(seconds=59)  # out of LiteLLM already (backend controller), streams finish
    await controller.reconcile_once()
    assert names(k8s) == {"llm-day-cpu"} and k8s.nodes["c1"].deployment == "day"
    now["t"] = end + timedelta(seconds=60)  # SCHEDULE_DRAIN_S, 60 s by default
    await controller.reconcile_once()
    assert names(k8s) == {"llm-evening-cpu"} and k8s.nodes["c1"].deployment == "evening"
