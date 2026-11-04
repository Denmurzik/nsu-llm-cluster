import asyncio
from typing import NamedTuple

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text

from app.catalog.gguf import GgufLayout, GgufMeta
from app.config import Settings
from app.catalog.hf import HfFile, VllmInfo
from app.deployments import service
from app.deployments.service import (
    DeploymentConflict,
    DeploymentIn,
    DeploymentInvalid,
    DeploymentPatch,
    NodeCandidate,
)
from app.k8s import K8sUnavailable
from app.models import ModelDeployment, NodeAssignment, PortalFlag, StandaloneNode
from app.workloads import builders
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.fakes.progress import FakeProgress

pytestmark = pytest.mark.docker

GiB = 2**30
REPO = "org/model-GGUF"
FILE = "model-Q4_K_M.gguf"
FILE8 = "model-Q8_0.gguf"
VLLM_REPO = "org/vllm-model"


def gmeta(max_ctx: int = 32768) -> GgufMeta:
    return GgufMeta("llama", 32, 32, 8, 128, 4096, max_ctx)


def make_hf() -> FakeHf:
    return FakeHf(
        files={REPO: [HfFile(FILE, 4 * GiB), HfFile(FILE8, 8 * GiB), HfFile("mmproj-f16.gguf", 100)]},
        metas={(REPO, FILE): gmeta(), (REPO, FILE8): gmeta(65536)},
        vllm={VLLM_REPO: VllmInfo(10 * GiB, gmeta(16384))},
    )


def dep_in(**kw) -> DeploymentIn:
    return DeploymentIn(**{"name": "m1", "repo": REPO, "file": FILE, **kw})


@pytest.fixture
def settings(settings):
    settings.hf_endpoint = "http://127.0.0.1:9"  # refused at once: the startup seed must not reach the internet
    return settings


@pytest_asyncio.fixture(autouse=True)
async def clean_slate(app):
    """The app seeds a default deployment on startup; every test here starts without it."""
    async with app.state.sessionmaker() as db:
        await db.execute(delete(NodeAssignment))
        await db.execute(delete(ModelDeployment))
        await db.execute(delete(PortalFlag))
        await db.commit()


async def count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


class Assigned(NamedTuple):
    deployment_id: int | None
    source: str
    manual: bool


async def assignment(db, node: str) -> Assigned | None:
    row = await db.get(NodeAssignment, node, populate_existing=True)
    return None if row is None else Assigned(row.deployment_id, row.source, row.manual)


def standalone(name: str, accel: str = "cpu") -> StandaloneNode:
    return StandaloneNode(name=name, url="http://10.0.0.5:18080", accel=accel, version="t")


# ---------- create / update ----------


async def test_create_llamacpp_stores_meta_and_assigns_nodes(db_session):
    db_session.add(standalone("n2"))
    await db_session.commit()
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in(nodes=["n1", "N2"]))
    assert dep.id and dep.name == "m1" and dep.engine == "llamacpp" and dep.mode == "replicas"
    assert dep.meta == {
        "weights_bytes": 4 * GiB, "arch": "llama", "n_layers": 32, "n_heads": 32, "n_kv_heads": 8,
        "head_dim": 128, "max_ctx": 32768, "embedding": 4096, "value_dim": None, "mla": False, "nextn_layers": 0,
    }
    n1, n2 = await assignment(db_session, "n1"), await assignment(db_session, "n2")
    assert (n1.deployment_id, n1.source, n1.manual) == (dep.id, "k8s", True)
    assert (n2.deployment_id, n2.source, n2.manual) == (dep.id, "standalone", True)
    assert [d.id for d in await service.list_all(db_session)] == [dep.id]
    assert (await service.get(db_session, dep.id)).name == "m1"
    assert await service.get(db_session, dep.id + 100) is None


async def test_create_vllm_uses_vllm_info(db_session):
    dep = await service.create(
        db_session, make_hf(), DeploymentIn(name="v", engine="vllm", repo=VLLM_REPO, accel="nvidia")
    )
    assert dep.file is None and dep.meta["weights_bytes"] == 10 * GiB and dep.meta["max_ctx"] == 16384


@pytest.mark.parametrize(
    "overrides",
    [
        {"engine": "vllm", "file": None, "accel": "cpu", "repo": VLLM_REPO},
        {"engine": "vllm", "file": None, "accel": "any", "repo": VLLM_REPO},
        {"engine": "vllm", "file": None, "accel": "nvidia", "mode": "split", "repo": VLLM_REPO},
        {"engine": "vllm", "accel": "nvidia", "repo": VLLM_REPO},  # vllm with a file
        {"file": "nope.gguf"},
        {"file": None},
        {"ctx_per_request": 40000},  # above max_ctx 32768
        {"ctx_per_request": 255},
        {"parallel": 17},
        {"parallel": 0},
        {"mode": "split", "accel": "cpu", "group_size": 1},
        {"name": "Bad_Name"},
        {"name": "-x"},
        {"name": "a" * 41},
        {"engine": "tgi"},
        {"accel": "tpu"},
        {"repo": "../etc"},
    ],
    ids=lambda o: ",".join(f"{k}={v}" for k, v in o.items())[:60],
)
async def test_validation_rules(db_session, overrides):
    with pytest.raises(DeploymentInvalid) as err:
        await service.create(db_session, make_hf(), dep_in(**overrides))
    assert err.value.detail
    assert await count(db_session, ModelDeployment) == 0


async def test_validation_rules_duplicate_name(db_session):
    await service.create(db_session, make_hf(), dep_in())
    with pytest.raises(DeploymentConflict) as err:
        await service.create(db_session, make_hf(), dep_in())
    assert err.value.detail == "Развёртывание с таким именем уже есть"
    assert isinstance(err.value, DeploymentInvalid)
    assert await count(db_session, ModelDeployment) == 1


async def test_create_hf_errors_propagate(db_session):
    from app.catalog.hf import HfNotFound, HfUnavailable

    with pytest.raises(HfNotFound):
        await service.create(db_session, make_hf(), dep_in(repo="org/absent"))
    hf = make_hf()
    hf.unavailable = True
    with pytest.raises(HfUnavailable):
        await service.create(db_session, hf, dep_in())
    assert await count(db_session, ModelDeployment) == 0


async def test_spec_hash_changes_only_on_pod_fields(db_session):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in())
    h = service.spec_hash(dep)
    assert len(h) == 12 and int(h, 16) >= 0
    await service.update(db_session, hf, dep.id, DeploymentPatch(auto_assign=False, enabled=False))
    assert service.spec_hash(await service.get(db_session, dep.id)) == h
    await service.update(db_session, hf, dep.id, DeploymentPatch(ctx_per_request=4096))
    h2 = service.spec_hash(await service.get(db_session, dep.id))
    await service.update(db_session, hf, dep.id, DeploymentPatch(parallel=4))
    h3 = service.spec_hash(await service.get(db_session, dep.id))
    assert len({h, h2, h3}) == 3


async def test_spec_hash_counts_group_size_only_in_split_mode(db_session):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in(accel="cpu"))
    h = service.spec_hash(dep)
    await service.update(db_session, hf, dep.id, DeploymentPatch(group_size=5))
    assert service.spec_hash(await service.get(db_session, dep.id)) == h  # replicas: the group size changes no pod
    await service.update(db_session, hf, dep.id, DeploymentPatch(mode="split"))
    split5 = service.spec_hash(await service.get(db_session, dep.id))
    await service.update(db_session, hf, dep.id, DeploymentPatch(group_size=3))
    assert service.spec_hash(await service.get(db_session, dep.id)) not in (h, split5)


async def test_patch_validates_merged_state(db_session):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in())  # accel any
    with pytest.raises(DeploymentInvalid):  # split + stored accel any: the device limit counts GPU nodes (2 each)
        await service.update(db_session, hf, dep.id, DeploymentPatch(mode="split", group_size=32))
    with pytest.raises(DeploymentInvalid):  # above max_ctx
        await service.update(db_session, hf, dep.id, DeploymentPatch(ctx_per_request=10**6))
    with pytest.raises(DeploymentInvalid):  # vllm needs nvidia, no file
        await service.update(db_session, hf, dep.id, DeploymentPatch(engine="vllm"))
    stored = await service.get(db_session, dep.id)
    assert (stored.mode, stored.ctx_per_request, stored.engine) == ("replicas", 8192, "llamacpp")
    updated = await service.update(db_session, hf, dep.id, DeploymentPatch(mode="split", group_size=2, accel="cpu"))
    assert (updated.mode, updated.accel, updated.group_size) == ("split", "cpu", 2)


async def test_update_missing_deployment(db_session):
    with pytest.raises(service.DeploymentNotFound):
        await service.update(db_session, make_hf(), 999, DeploymentPatch(enabled=False))
    with pytest.raises(service.DeploymentNotFound):
        await service.delete(db_session, 999)


async def test_update_file_refetches_meta(db_session):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in())
    calls = len(hf.calls)
    await service.update(db_session, hf, dep.id, DeploymentPatch(ctx_per_request=4096))
    assert len(hf.calls) == calls  # context-only change needs no Hugging Face call
    updated = await service.update(db_session, hf, dep.id, DeploymentPatch(file=FILE8))
    assert ("gguf_meta", REPO, FILE8) in hf.calls
    assert updated.meta["weights_bytes"] == 8 * GiB and updated.meta["max_ctx"] == 65536
    with pytest.raises(DeploymentInvalid):
        await service.update(db_session, hf, dep.id, DeploymentPatch(file="missing.gguf"))


async def test_update_nodes_reassigns_and_unassign_is_manual(db_session):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in(nodes=["n1", "n2"]))
    await service.update(db_session, hf, dep.id, DeploymentPatch(nodes=["n2", "n3"]))
    assert (await assignment(db_session, "n1")).deployment_id is None
    assert (await assignment(db_session, "n1")).manual is True
    assert (await assignment(db_session, "n2")).deployment_id == dep.id
    assert (await assignment(db_session, "n3")).deployment_id == dep.id
    await service.update(db_session, hf, dep.id, DeploymentPatch(enabled=False))  # nodes untouched
    assert (await assignment(db_session, "n3")).deployment_id == dep.id


async def test_update_to_split_rejected_while_standalone_assigned(db_session):
    hf = make_hf()
    db_session.add(standalone("s1"))
    await db_session.commit()
    dep_id = (await service.create(db_session, hf, dep_in(accel="cpu", nodes=["s1"]))).id
    with pytest.raises(DeploymentInvalid):
        await service.update(db_session, hf, dep_id, DeploymentPatch(mode="split"))
    assert (await service.get(db_session, dep_id)).mode == "replicas"


async def test_delete_unassigns_nodes(db_session):
    dep = await service.create(db_session, make_hf(), dep_in(nodes=["n1"]))
    await service.delete(db_session, dep.id)
    assert await service.get(db_session, dep.id) is None
    assert (await assignment(db_session, "n1")).deployment_id is None


async def test_delete_hands_manually_assigned_nodes_back_to_auto_assignment(db_session):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in(nodes=["n1"]))
    assert (await assignment(db_session, "n1")).manual is True
    other = await service.create(db_session, hf, dep_in(name="m2", auto_assign=True))
    await service.delete(db_session, dep.id)
    assert (await assignment(db_session, "n1")).manual is False  # a freed node must not idle forever
    changes = await service.auto_assign(db_session, [NodeCandidate("n1", "cpu", "k8s", None, 16000)])
    assert changes == [("n1", other.id)]


# ---------- offline meta, seed ----------


async def test_offline_seed_skips_max_ctx_and_flags_meta_missing(db_session, settings):
    hf = make_hf()
    hf.unavailable = True
    await service.seed_default(db_session, settings, hf)
    dep = (await service.list_all(db_session))[0]
    assert dep.meta is None
    calls = len(hf.calls)
    updated = await service.update(db_session, hf, dep.id, DeploymentPatch(ctx_per_request=10**6))
    assert updated.ctx_per_request == 10**6 and updated.meta is None  # no max_ctx known, nothing to check
    assert len(hf.calls) == calls


async def test_seed_default_once_and_offline_ok(db_session, settings):
    seed_meta = gmeta(131072)
    hf = FakeHf(
        files={settings.default_model_repo: [HfFile(settings.default_model_file, 5 * GiB)]},
        metas={(settings.default_model_repo, settings.default_model_file): seed_meta},
    )
    await service.seed_default(db_session, settings, hf)
    await service.seed_default(db_session, settings, hf)
    (dep,) = await service.list_all(db_session)
    assert (dep.name, dep.repo, dep.file) == (
        "gemma-4-e4b", "unsloth/gemma-4-E4B-it-GGUF", "gemma-4-E4B-it-Q4_K_M.gguf"
    )
    assert (dep.engine, dep.mode, dep.accel, dep.ctx_per_request, dep.parallel) == ("llamacpp", "replicas", "any", 8192, 2)
    assert dep.auto_assign is True and dep.enabled is True
    assert dep.meta["max_ctx"] == 131072 and dep.meta["weights_bytes"] == 5 * GiB

    await service.delete(db_session, dep.id)
    offline = make_hf()
    offline.unavailable = True
    await service.seed_default(db_session, settings, offline)
    await service.seed_default(db_session, settings, hf)
    assert await service.list_all(db_session) == []  # a seed the admin deleted does not come back


async def test_seed_default_does_not_return_after_restart(db_session, app, settings):
    hf = make_hf()
    hf.unavailable = True
    await service.seed_default(db_session, settings, hf)
    (dep,) = await service.list_all(db_session)
    await service.delete(db_session, dep.id)
    async with app.state.sessionmaker() as other:  # a restart: a new session, the same database
        await service.seed_default(other, settings, hf)
        assert await service.list_all(other) == []


async def test_seed_default_marks_an_installation_that_already_has_deployments(db_session, settings):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in())
    await service.seed_default(db_session, settings, hf)
    assert [d.name for d in await service.list_all(db_session)] == ["m1"]
    await service.delete(db_session, dep.id)
    await service.seed_default(db_session, settings, hf)
    assert await service.list_all(db_session) == []  # the flag was set at the first call


SEED_META = gmeta(131072)


def seed_hf(settings, **kw):
    return FakeHf(
        files={settings.default_model_repo: [HfFile(settings.default_model_file, 5 * GiB)]},
        metas={(settings.default_model_repo, settings.default_model_file): SEED_META},
        **kw,
    )


async def test_refresh_missing_meta_fills_meta_after_an_offline_seed(db_session, settings):
    offline = make_hf()
    offline.unavailable = True
    await service.seed_default(db_session, settings, offline)
    (dep,) = await service.list_all(db_session)
    assert dep.meta is None and dep.auto_assign is True
    await service.refresh_missing_meta(db_session, offline)  # still offline: nothing breaks
    assert (await service.get(db_session, dep.id)).meta is None
    await service.refresh_missing_meta(db_session, seed_hf(settings))
    assert (await service.get(db_session, dep.id)).meta["max_ctx"] == 131072 and not service.ctx_exceeds_model(dep)


async def test_refresh_missing_meta_does_not_overwrite_an_admin_change_made_meanwhile(db_session, settings, app):
    offline = make_hf()
    offline.unavailable = True
    await service.seed_default(db_session, settings, offline)
    (dep,) = await service.list_all(db_session)
    slow = seed_hf(settings)
    lookup = slow.gguf_meta

    async def admin_patches_during_the_lookup(repo, file):
        meta = await lookup(repo, file)
        async with app.state.sessionmaker() as admin:  # the admin switches the model while Hugging Face answers
            await service.update(admin, make_hf(), dep.id, DeploymentPatch(repo=REPO, file=FILE8, ctx_per_request=4096))
        return meta

    slow.gguf_meta = admin_patches_during_the_lookup
    await service.refresh_missing_meta(db_session, slow)
    async with app.state.sessionmaker() as fresh:
        stored = await service.get(fresh, dep.id)
        assert (stored.repo, stored.file) == (REPO, FILE8)
        assert stored.meta["max_ctx"] == 65536  # the admin's model, not the seed model looked up before


class Clock:
    """Stands in for the service module's clock."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(service, "_now", c)
    return c


async def test_refresh_missing_meta_backs_off_exponentially_up_to_an_hour(db_session, settings, clock):
    offline = make_hf()
    offline.unavailable = True
    await service.seed_default(db_session, settings, offline)
    backoff = {}
    delays = []
    for _ in range(10):
        calls = len(offline.calls)
        await service.refresh_missing_meta(db_session, offline, backoff)
        attempted = len(offline.calls)
        assert attempted > calls
        ((retry_at, delay),) = backoff.values()
        delays.append(delay)
        clock.now = retry_at - 1
        await service.refresh_missing_meta(db_session, offline, backoff)
        assert len(offline.calls) == attempted  # one second before the window ends: still no request
        clock.now = retry_at
    assert delays == [30, 60, 120, 240, 480, 960, 1920, 3600, 3600, 3600]


async def test_refresh_missing_meta_skips_deployments_in_backoff(db_session, settings, clock):
    offline = make_hf()
    offline.unavailable = True
    await service.seed_default(db_session, settings, offline)
    backoff = {}
    await service.refresh_missing_meta(db_session, offline, backoff)
    calls = len(offline.calls)
    await service.refresh_missing_meta(db_session, offline, backoff)
    assert len(offline.calls) == calls  # inside the window: Hugging Face is not asked again
    online = seed_hf(settings)
    clock.now += 31
    await service.refresh_missing_meta(db_session, online, backoff)
    assert backoff == {}
    (dep,) = await service.list_all(db_session)
    assert dep.meta is not None


async def test_refresh_missing_meta_never_blocks_longer_than_the_budget(db_session, settings, monkeypatch):
    monkeypatch.setattr(service, "META_REFRESH_BUDGET_S", 2.5)
    hf = make_hf()
    hf.unavailable = True
    await service.seed_default(db_session, settings, hf)
    for i in range(5):
        db_session.add(ModelDeployment(
            name=f"slow{i}", engine="llamacpp", repo=REPO, file=FILE, mode="replicas", group_size=2, accel="any",
            ctx_per_request=4096, parallel=1, auto_assign=False, enabled=True, meta=None,
        ))
    await db_session.commit()

    class Hang:
        async def gguf_files(self, repo):
            await asyncio.sleep(30)

    started = asyncio.get_running_loop().time()
    backoff = {}
    await service.refresh_missing_meta(db_session, Hang(), backoff)
    assert asyncio.get_running_loop().time() - started < 5  # 6 deployments, 30 s each, one 2.5 s budget
    assert len(backoff) == 1  # the one that ran out of time is in backoff, the rest wait for the next pass


async def test_refresh_stops_without_a_failure_when_under_a_second_is_left(db_session, settings, monkeypatch):
    monkeypatch.setattr(service, "META_REFRESH_BUDGET_S", 0.9)
    offline = make_hf()
    offline.unavailable = True
    await service.seed_default(db_session, settings, offline)
    hf = make_hf()
    backoff = {}
    await service.refresh_missing_meta(db_session, hf, backoff)
    assert hf.calls == [] and backoff == {}  # no request, no false failure


async def test_refresh_flags_a_context_above_the_model_limit(db_session, settings, caplog):
    offline = make_hf()
    offline.unavailable = True
    await service.seed_default(db_session, settings, offline)
    (dep,) = await service.list_all(db_session)
    dep.ctx_per_request = 200000  # accepted while the limit was unknown
    await db_session.commit()
    assert service.ctx_exceeds_model(dep) is False
    with caplog.at_level("WARNING"):
        await service.refresh_missing_meta(db_session, seed_hf(settings))
    dep = await service.get(db_session, dep.id)
    assert service.ctx_exceeds_model(dep) is True
    assert "above the model limit" in caplog.text


# ---------- assignment ----------


async def test_assign_standalone_to_split_rejected(db_session):
    hf = make_hf()
    db_session.add(standalone("s1"))
    await db_session.commit()
    split = await service.create(db_session, hf, dep_in(name="sp", mode="split", accel="cpu", group_size=2))
    vllm = await service.create(
        db_session, hf, DeploymentIn(name="v", engine="vllm", repo=VLLM_REPO, accel="nvidia")
    )
    ids = (split.id, vllm.id)
    for dep_id in ids:
        with pytest.raises(DeploymentInvalid) as err:
            await service.assign(db_session, "s1", dep_id)
        assert err.value.detail == "Standalone-нода поддерживает только replicas на llama.cpp"
    await service.assign(db_session, "k1", ids[0])  # a k8s node may join a split deployment
    assert (await assignment(db_session, "k1")).source == "k8s"
    with pytest.raises(service.DeploymentNotFound):
        await service.assign(db_session, "k1", 999)


async def test_auto_assign_deterministic_and_accel_aware(db_session):
    hf = make_hf()
    a = await service.create(db_session, hf, dep_in(name="a", accel="cpu", auto_assign=True))
    b = await service.create(db_session, hf, dep_in(name="b", accel="nvidia", auto_assign=True))
    c = await service.create(db_session, hf, dep_in(name="c", accel="any", auto_assign=True))
    await service.create(db_session, hf, dep_in(name="d", enabled=False, auto_assign=True))
    await service.create(db_session, hf, dep_in(name="e", auto_assign=False))
    cands = [
        NodeCandidate("g2", "nvidia", "k8s", 24000, 32000),
        NodeCandidate("c2", "cpu", "k8s", None, 16000),
        NodeCandidate("g1", "nvidia", "k8s", 24000, 32000),
        NodeCandidate("c1", "cpu", "standalone", None, 8000),
    ]
    changes = await service.auto_assign(db_session, cands)
    assert changes == [("c1", a.id), ("c2", a.id), ("g1", b.id), ("g2", b.id)]
    for name, dep_id in changes:
        row = await assignment(db_session, name)
        assert (row.deployment_id, row.manual) == (dep_id, False)
    assert (await assignment(db_session, "c1")).source == "standalone"
    assert await service.auto_assign(db_session, list(reversed(cands))) == []  # idempotent

    await service.delete(db_session, a.id)
    await service.delete(db_session, b.id)
    # only the unrestricted deployment is left: it takes both kinds, rows freed by delete are non-manual
    changes = await service.auto_assign(db_session, cands + [NodeCandidate("g3", "nvidia", "k8s", 1, 1)])
    assert [n for n, _ in changes] == ["c1", "c2", "g1", "g2", "g3"]
    assert {i for _, i in changes} == {c.id}


async def test_auto_assign_skips_standalone_for_split(db_session):
    hf = make_hf()
    sp = await service.create(db_session, hf, dep_in(name="sp", mode="split", accel="cpu", group_size=2, auto_assign=True))
    cands = [NodeCandidate("s1", "cpu", "standalone", None, 8000), NodeCandidate("k1", "cpu", "k8s", None, 8000)]
    assert await service.auto_assign(db_session, cands) == [("k1", sp.id)]
    row = await assignment(db_session, "s1")
    assert row is not None and row.deployment_id is None and row.source == "standalone"


async def test_manual_unassign_is_not_overridden_by_auto_assign(db_session):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in(auto_assign=True))
    await service.assign(db_session, "n1", dep.id)
    await service.assign(db_session, "n1", None)
    assert await service.auto_assign(db_session, [NodeCandidate("n1", "cpu", "k8s", None, 8000)]) == []
    row = await assignment(db_session, "n1")
    assert (row.deployment_id, row.manual) == (None, True)
    # a manual assignment to one deployment is not moved either
    other = await service.create(db_session, hf, dep_in(name="other"))
    other_id = other.id
    await service.assign(db_session, "n2", other_id)
    assert await service.auto_assign(db_session, [NodeCandidate("n2", "cpu", "k8s", None, 8000)]) == []
    assert (await assignment(db_session, "n2")).deployment_id == other_id


async def test_live_nodes_respects_ttl(db_session):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    for name, age in (("fresh", 5), ("stale", 100)):
        node = standalone(name)
        node.last_seen_at = now - timedelta(seconds=age)
        db_session.add(node)
    await db_session.commit()
    assert [n.name for n in await service.live_nodes(db_session, 45)] == ["fresh"]
    assert {n.name for n in await service.live_nodes(db_session, 600)} == {"fresh", "stale"}


async def test_desired_for_node_ctx_is_product(db_session):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in(name="tiny", ctx_per_request=4096, parallel=3, nodes=["s1"]))
    desired = await service.desired_for_node(db_session, "s1")
    assert desired == service.DesiredConfig(
        deployment="tiny", model_repo=REPO, model_file=FILE, public_model_name="tiny",
        ctx_size=4096 * 3, parallel=3, spec_hash=service.spec_hash(dep),
        llm_env={"KV_CACHE_TYPE": "q8_0"},  # new deployments get the q8_0 cache; the node gets it like a pod
    )
    assert await service.desired_for_node(db_session, "unknown") is None
    await service.update(db_session, hf, dep.id, DeploymentPatch(enabled=False))
    assert await service.desired_for_node(db_session, "s1") is None
    await service.update(db_session, hf, dep.id, DeploymentPatch(enabled=True))
    assert await service.desired_for_node(db_session, "s1") is not None
    await service.assign(db_session, "s1", None)
    assert await service.desired_for_node(db_session, "s1") is None
    split = await service.create(db_session, hf, dep_in(name="sp", mode="split", accel="cpu", group_size=2))
    await service.assign(db_session, "s1", split.id)
    assert await service.desired_for_node(db_session, "s1") is None


# ---------- status ----------


async def test_deployment_status_reports_pods_logs_and_backends(db_session):
    hf = make_hf()
    db_session.add(standalone("s1"))
    await db_session.commit()
    dep = await service.create(db_session, hf, dep_in(nodes=["k1", "k2", "s1"]))
    k8s, litellm = FakeK8s(), FakeLiteLLM()
    k8s.add_pod("p-ok", "k1", "inference", "m1", ready=True)
    k8s.add_pod("p-bad", "k2", "inference", "m1", ready=False, phase="Pending", restarts=3, message="ImagePullBackOff")
    k8s.add_pod("p-other", "k1", "inference", "other-dep")
    k8s.logs["p-bad"] = "first\nloading model failed"
    k8s.logs["p-ok"] = "all good"
    await litellm.add_model("pm-1", "m1", "http://k1:18080/v1", "k8s", "k1", 8192)

    status = await service.deployment_status(db_session, k8s, litellm, dep)
    assert status["spec_hash"] == service.spec_hash(dep)
    assert status["ready_backends"] == 1 and status["groups"] == []
    by_node = {n["name"]: n for n in status["nodes"]}
    assert set(by_node) == {"k1", "k2", "s1"}
    assert by_node["k1"]["source"] == "k8s" and by_node["s1"]["source"] == "standalone"
    ok = by_node["k1"]["pods"][0]
    assert ok == {"name": "p-ok", "role": "inference", "phase": "Running", "ready": True, "restarts": 0,
                  "message": "", "last_log": None, "progress": None}
    bad = by_node["k2"]["pods"][0]
    assert (bad["ready"], bad["restarts"], bad["message"], bad["last_log"]) == (False, 3, "ImagePullBackOff", "loading model failed")
    assert (by_node["k1"]["applied"], by_node["k2"]["applied"], by_node["s1"]["applied"]) == (True, False, False)
    assert by_node["s1"]["pods"] == []
    node = await db_session.get(StandaloneNode, "s1")
    node.applied_spec_hash = service.spec_hash(dep)
    await db_session.commit()
    status = await service.deployment_status(db_session, k8s, litellm, dep)
    assert {n["name"]: n["applied"] for n in status["nodes"]}["s1"] is True


async def test_deployment_status_degrades_when_backends_are_down(db_session):
    dep = await service.create(db_session, make_hf(), dep_in(nodes=["k1"]))
    k8s, litellm = FakeK8s(), FakeLiteLLM()
    k8s.unavailable = True
    litellm.unavailable = True
    status = await service.deployment_status(db_session, k8s, litellm, dep)
    assert status["ready_backends"] is None
    assert status["nodes"] == [{"name": "k1", "source": "k8s", "pods": [], "applied": False}]


async def test_deployment_status_split_groups(db_session):
    dep = await service.create(
        db_session, make_hf(), dep_in(name="sp", mode="split", accel="cpu", group_size=2, nodes=["c", "a", "b"])
    )
    k8s = FakeK8s()
    for name in "abc":
        k8s.add_node(name, accel="cpu")
    status = await service.deployment_status(db_session, k8s, FakeLiteLLM(), dep)
    assert status["groups"] == [["a", "b"]]  # the incomplete tail is dropped


async def test_deployment_status_split_groups_follow_the_running_leaders(db_session):
    dep = await service.create(
        db_session, make_hf(), dep_in(name="sp", mode="split", accel="cpu", group_size=2, nodes=["a", "b", "c", "d"])
    )
    k8s = FakeK8s()
    for name in "abcd":
        k8s.add_node(name, accel="cpu")
    leader = builders.split_leader(dep, 1, [builders.RpcWorker(n, "10.0.0.1", 18050, "cpu") for n in "bc"], Settings())
    await k8s.apply(leader)  # a stable group formed earlier: b and c stay together, a and d pair up
    status = await service.deployment_status(db_session, k8s, FakeLiteLLM(), dep)
    assert status["groups"] == [["a", "d"], ["b", "c"]]


def race_after_rows_read(db_session, monkeypatch, racer):
    """Runs `racer` (another session) right after auto_assign has read the assignment rows, once."""
    original = db_session.scalars
    fired = False

    async def hooked(stmt, *args, **kwargs):
        nonlocal fired
        result = await original(stmt, *args, **kwargs)
        if not fired and "node_assignments" in str(stmt):
            fired = True
            await racer()
        return result

    monkeypatch.setattr(db_session, "scalars", hooked)


async def test_auto_assign_does_not_overwrite_a_concurrent_manual_unassign(app, db_session, monkeypatch):
    hf = make_hf()
    await service.create(db_session, hf, dep_in(auto_assign=False))
    cand = [NodeCandidate("n1", "cpu", "k8s", None, 8000)]
    assert await service.auto_assign(db_session, cand) == []  # an idle, non-manual row now exists
    auto = await service.create(db_session, hf, dep_in(name="auto", auto_assign=True))

    async def admin_unassigns():
        async with app.state.sessionmaker() as other:
            await service.assign(other, "n1", None)

    race_after_rows_read(db_session, monkeypatch, admin_unassigns)
    assert await service.auto_assign(db_session, cand) == []
    row = await assignment(db_session, "n1")
    assert (row.deployment_id, row.manual) == (None, True)
    assert auto.id is not None


async def test_auto_assign_does_not_overwrite_a_concurrent_manual_assign_of_a_new_row(app, db_session, monkeypatch):
    hf = make_hf()
    await service.create(db_session, hf, dep_in(name="auto", auto_assign=True))
    manual = await service.create(db_session, hf, dep_in(name="manual", auto_assign=False))
    manual_id = manual.id

    async def admin_assigns():
        async with app.state.sessionmaker() as other:
            await service.assign(other, "n1", manual_id)

    race_after_rows_read(db_session, monkeypatch, admin_assigns)
    assert await service.auto_assign(db_session, [NodeCandidate("n1", "cpu", "k8s", None, 8000)]) == []
    row = await assignment(db_session, "n1")
    assert (row.deployment_id, row.manual) == (manual_id, True)


async def test_admin_unassign_wins_over_a_concurrent_auto_assign_of_the_same_row(app, db_session):
    hf = make_hf()
    await service.create(db_session, hf, dep_in(name="off", auto_assign=False))
    cand = [NodeCandidate("n1", "cpu", "k8s", None, 8000)]
    assert await service.auto_assign(db_session, cand) == []  # an idle, non-manual row exists
    await service.create(db_session, hf, dep_in(name="auto", auto_assign=True))

    async with app.state.sessionmaker() as admin:
        await service._assign(admin, "n1", None)  # the admin has read the free row, not committed yet
        task = asyncio.create_task(service.auto_assign(db_session, cand))
        await asyncio.sleep(0.5)
        assert not task.done()  # auto_assign waits for the row instead of writing over it
        await admin.commit()
        assert await asyncio.wait_for(task, 10) == []
    row = await assignment(db_session, "n1")
    assert (row.deployment_id, row.manual) == (None, True)


async def test_admin_assign_of_a_new_row_survives_a_concurrent_auto_insert(app, db_session, monkeypatch):
    hf = make_hf()
    await service.create(db_session, hf, dep_in(name="auto", auto_assign=True))
    manual = await service.create(db_session, hf, dep_in(name="manual", auto_assign=False))
    manual_id = manual.id
    cand = [NodeCandidate("n1", "cpu", "k8s", None, 8000)]

    async with app.state.sessionmaker() as admin:
        original = admin.get
        fired = False

        async def hooked(entity, *args, **kwargs):
            nonlocal fired
            result = await original(entity, *args, **kwargs)
            if entity is NodeAssignment and result is None and not fired:
                fired = True  # auto_assign inserts the same node between the admin's read and write
                async with app.state.sessionmaker() as auto_db:
                    assert await service.auto_assign(auto_db, cand) != []
            return result

        monkeypatch.setattr(admin, "get", hooked)
        row = await service.assign(admin, "n1", manual_id)  # must not raise IntegrityError
        assert (row.deployment_id, row.manual) == (manual_id, True)
    row = await assignment(db_session, "n1")
    assert (row.deployment_id, row.manual) == (manual_id, True)


# ---------- accelerator checks, concurrency, order ----------


async def test_manual_assignment_checks_the_node_accel(db_session):
    hf = make_hf()
    k8s = FakeK8s()
    k8s.add_node("gpu-k", accel="nvidia")
    k8s.add_node("cpu-k", accel="cpu")
    db_session.add(standalone("cpu-s", "cpu"))
    await db_session.commit()
    cpu_id = (await service.create(db_session, hf, dep_in(name="c", accel="cpu"))).id
    with pytest.raises(DeploymentInvalid, match="gpu-k.*nvidia.*cpu"):
        await service.assign(db_session, "gpu-k", cpu_id, k8s)
    assert await assignment(db_session, "gpu-k") is None
    await service.assign(db_session, "cpu-k", cpu_id, k8s)
    await service.assign(db_session, "cpu-s", cpu_id, k8s)
    nv_id = (await service.create(db_session, hf, dep_in(name="g", accel="nvidia"))).id
    with pytest.raises(DeploymentInvalid):
        await service.assign(db_session, "cpu-s", nv_id, k8s)  # a standalone node is checked without the cluster too
    with pytest.raises(DeploymentInvalid):
        await service.assign(db_session, "cpu-s", nv_id)
    any_id = (await service.create(db_session, hf, dep_in(name="a", accel="any"))).id
    await service.assign(db_session, "gpu-k", any_id, k8s)
    await service.assign(db_session, "gpu-k", None, k8s)  # unassigning is never blocked


async def test_create_with_nodes_checks_accel_and_unknown_nodes_pass(db_session):
    hf = make_hf()
    k8s = FakeK8s()
    k8s.add_node("gpu-k", accel="nvidia")
    with pytest.raises(DeploymentInvalid):
        await service.create(db_session, hf, dep_in(accel="cpu", nodes=["gpu-k"]), k8s)
    assert await count(db_session, ModelDeployment) == 0
    k8s.unavailable = True  # nothing is known: the check cannot fail a request
    dep = await service.create(db_session, hf, dep_in(accel="cpu", nodes=["gpu-k", "ghost"]), k8s)
    assert (await assignment(db_session, "gpu-k")).deployment_id == dep.id


async def test_accel_change_releases_auto_nodes_and_rejects_manual_ones(db_session):
    hf = make_hf()
    k8s = FakeK8s()
    k8s.add_node("gpu-k", accel="nvidia")
    k8s.add_node("cpu-k", accel="cpu")
    dep = await service.create(db_session, hf, dep_in(accel="any", auto_assign=True))
    dep_id = dep.id
    await service.auto_assign(
        db_session,
        [NodeCandidate("gpu-k", "nvidia", "k8s", 8192, 16000), NodeCandidate("cpu-k", "cpu", "k8s", None, 16000)],
    )
    assert (await assignment(db_session, "gpu-k")).deployment_id == dep_id

    updated = await service.update(db_session, hf, dep_id, DeploymentPatch(accel="cpu"), k8s)
    assert updated.accel == "cpu"
    assert (await assignment(db_session, "gpu-k")).deployment_id is None  # released
    assert (await assignment(db_session, "gpu-k")).manual is False  # and free for the next auto assignment
    assert (await assignment(db_session, "cpu-k")).deployment_id == dep_id

    pinned_id = (await service.create(db_session, hf, dep_in(name="p", accel="any"))).id
    await service.assign(db_session, "gpu-k", pinned_id, k8s)
    with pytest.raises(DeploymentInvalid, match="gpu-k"):
        await service.update(db_session, hf, pinned_id, DeploymentPatch(accel="cpu"), k8s)
    assert (await service.get(db_session, pinned_id)).accel == "any"  # rolled back
    assert (await assignment(db_session, "gpu-k")).deployment_id == pinned_id
    # the same accel again is not a change
    await service.update(db_session, hf, pinned_id, DeploymentPatch(accel="any", ctx_per_request=4096), k8s)


async def test_concurrent_create_with_one_name_is_a_conflict(db_session, app):
    hf = make_hf()

    async def attempt():
        async with app.state.sessionmaker() as db:
            return await service.create(db, hf, dep_in(name="race"))

    results = await asyncio.gather(*(attempt() for _ in range(6)), return_exceptions=True)
    made = [r for r in results if not isinstance(r, Exception)]
    assert len(made) == 1
    assert all(isinstance(r, DeploymentConflict) for r in results if isinstance(r, Exception)), results
    assert await count(db_session, ModelDeployment) == 1


async def test_create_assigns_nodes_in_sorted_order(db_session, monkeypatch):
    seen = []
    original = service._assign

    async def spy(db, node, dep, accels=None):
        seen.append(node)
        return await original(db, node, dep, accels)

    monkeypatch.setattr(service, "_assign", spy)
    await service.create(db_session, make_hf(), dep_in(nodes=["zz", "Aa", "mm", "aa"]))
    assert seen == ["aa", "mm", "zz"]


async def test_deployment_status_shows_the_load_progress_of_inference_pods(db_session):
    dep = await service.create(db_session, make_hf(), dep_in(nodes=["k1", "k2", "k3", "k4"]))
    k8s = FakeK8s()
    for i, name in enumerate(["k1", "k2", "k3", "k4"], start=1):
        k8s.add_node(name, accel="cpu", internal_ip=f"10.0.0.{i}")
    k8s.add_pod("p-ready", "k1", "inference", "m1", ready=True)
    k8s.add_pod("p-dl", "k2", "inference", "m1", ready=False)
    k8s.add_pod("p-load", "k3", "inference", "m1", ready=False)
    k8s.add_pod("p-pending", "k4", "inference", "m1", ready=False, phase="Pending")
    item = {"repo": REPO, "path": FILE, "offset": GiB, "size": 4 * GiB, "active": True, "updated": 1.0}
    progress = FakeProgress(transfers={"k2": [item]}, health={"http://10.0.0.3:18080/health": 503})
    status = await service.deployment_status(db_session, k8s, FakeLiteLLM(), dep, progress=progress, infer_port=18080)
    pods = {p["name"]: p["progress"] for n in status["nodes"] for p in n["pods"]}
    assert pods["p-ready"]["stage"] == "ready"
    assert pods["p-dl"] == {"stage": "downloading", "percent": 25.0, "done_bytes": GiB, "total_bytes": 4 * GiB}
    assert pods["p-load"]["stage"] == "loading"
    assert pods["p-pending"] is None
    # only running pods that are not ready are probed
    assert sorted(progress.health_calls) == ["http://10.0.0.2:18080/health", "http://10.0.0.3:18080/health"]


async def test_deployment_status_without_a_progress_source_or_for_rpc_pods(db_session):
    dep = await service.create(
        db_session, make_hf(), dep_in(name="sp", mode="split", accel="cpu", group_size=2, nodes=["a", "b"])
    )
    k8s = FakeK8s()
    for name in "ab":
        k8s.add_node(name, accel="cpu")
    k8s.add_pod("rpc-a", "a", "rpc", "sp", ready=True)
    k8s.add_pod("lead", "a", "split-leader", "sp", ready=False)
    status = await service.deployment_status(db_session, k8s, FakeLiteLLM(), dep, progress=FakeProgress())
    pods = {p["name"]: p["progress"] for n in status["nodes"] for p in n["pods"]}
    assert pods == {"rpc-a": None, "lead": {"stage": "starting", "percent": None, "done_bytes": None, "total_bytes": None}}
    status = await service.deployment_status(db_session, k8s, FakeLiteLLM(), dep)
    assert {p["name"]: p["progress"] for n in status["nodes"] for p in n["pods"]}["lead"] is None


# ---------- operation of the class: maintenance, spare nodes, regroup grace, schedule ----------

WORKDAYS = {"days": [1, 2, 3, 4, 5], "start": "08:00", "end": "20:00"}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"spare_nodes": -1}, "Резервных нод от 0 до 100"),
        ({"spare_nodes": 101}, "Резервных нод от 0 до 100"),
        ({"regroup_grace_s": -1}, "Пауза перед пересборкой от 0 до 86400 секунд"),
        ({"regroup_grace_s": 86401}, "Пауза перед пересборкой от 0 до 86400 секунд"),
        ({"schedule": {"days": [], "start": "08:00", "end": "20:00"}}, "Выберите хотя бы один день расписания"),
        ({"schedule": {"days": [1], "start": "8", "end": "20:00"}}, "Время расписания указывается как ЧЧ:ММ"),
    ],
)
async def test_operation_fields_are_validated(db_session, overrides, message):
    with pytest.raises(DeploymentInvalid) as err:
        await service.create(db_session, make_hf(), dep_in(**overrides))
    assert err.value.detail == message


async def test_operation_fields_are_stored_and_patch_can_clear_them(db_session):
    hf = make_hf()
    schedule = {"days": [5, 1, 2, 3, 4, 1], "start": "08:00", "end": "20:00"}
    dep = await service.create(
        db_session, hf, dep_in(mode="split", accel="cpu", spare_nodes=1, regroup_grace_s=300, schedule=schedule)
    )
    assert (dep.spare_nodes, dep.regroup_grace_s, dep.schedule) == (1, 300, WORKDAYS)
    before = service.spec_hash(dep)
    dep = await service.update(db_session, hf, dep.id, DeploymentPatch(spare_nodes=2))
    assert (dep.spare_nodes, dep.regroup_grace_s, dep.schedule) == (2, 300, WORKDAYS)  # unsent fields stay
    dep = await service.update(db_session, hf, dep.id, DeploymentPatch(regroup_grace_s=None, schedule=None))
    assert (dep.spare_nodes, dep.regroup_grace_s, dep.schedule) == (2, None, None)
    assert service.spec_hash(dep) == before  # none of them changes the pods


async def test_regroup_grace_defaults_by_mode(db_session, settings):
    hf = make_hf()
    replicas = await service.create(db_session, hf, dep_in(name="r"))
    split = await service.create(db_session, hf, dep_in(name="s", mode="split", accel="cpu"))
    assert service.regroup_grace(replicas, settings) == settings.node_notready_grace_s == 120
    assert service.regroup_grace(split, settings) == settings.split_regroup_grace_s == 600
    split = await service.update(db_session, hf, split.id, DeploymentPatch(regroup_grace_s=0))
    assert service.regroup_grace(split, settings) == 0


async def test_split_group_limit_keeps_spare_nodes():
    dep = ModelDeployment(name="s", mode="split", group_size=2, spare_nodes=0)
    assert service.split_group_limit(dep, 7) is None  # no spares: every pair forms a group
    dep.spare_nodes = 1
    assert service.split_group_limit(dep, 7) == 3
    assert service.split_group_limit(dep, 5) == 2
    dep.spare_nodes = 3
    assert service.split_group_limit(dep, 2) == 0


async def test_maintenance_keeps_the_assignment_and_blocks_auto_assignment(db_session):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in(auto_assign=True, nodes=["n1"]))
    row = await service.set_maintenance(db_session, "N1", True)
    assert (row.node_name, row.maintenance, row.deployment_id) == ("n1", True, dep.id)
    k8s = FakeK8s()
    k8s.add_node("n2", accel="cpu")
    fresh = await service.set_maintenance(db_session, "n2", True, k8s)  # a node without an assignment row yet
    assert (fresh.maintenance, fresh.deployment_id, fresh.source, fresh.manual) == (True, None, "k8s", False)

    cands = [NodeCandidate("n2", "cpu", "k8s", None, 8000), NodeCandidate("n3", "cpu", "k8s", None, 8000)]
    assert await service.auto_assign(db_session, cands) == [("n3", dep.id)]  # n2 is on maintenance
    await service.set_maintenance(db_session, "n2", False, k8s)
    assert await service.auto_assign(db_session, cands) == [("n2", dep.id)]
    assert (await assignment(db_session, "n1")).deployment_id == dep.id  # n1 gets its model back afterwards


async def test_desired_for_standalone_node_on_maintenance_or_off_schedule(db_session, monkeypatch):
    from datetime import UTC, datetime

    from app.deployments import schedule

    monkeypatch.setattr(schedule, "now_utc", lambda: datetime(2026, 11, 7, 6, 0, tzinfo=UTC))  # Saturday 13:00 NSK
    hf = make_hf()
    db_session.add(standalone("s1"))
    await db_session.commit()
    day = await service.create(db_session, hf, dep_in(name="day", schedule=WORKDAYS, nodes=["s1"]))
    assert await service.desired_for_node(db_session, "s1") is None  # off schedule, nobody to lend the node to
    spare_time = await service.create(db_session, hf, dep_in(name="weekend", auto_assign=True))
    assert (await service.desired_for_node(db_session, "s1")).deployment == "weekend"  # lent while "day" is off
    monkeypatch.setattr(schedule, "now_utc", lambda: datetime(2026, 11, 6, 6, 0, tzinfo=UTC))  # Friday 13:00 NSK
    assert (await service.desired_for_node(db_session, "s1")).deployment == "day"
    await service.set_maintenance(db_session, "s1", True)
    assert await service.desired_for_node(db_session, "s1") is None
    assert day.id != spare_time.id


async def test_catalog_shows_an_off_schedule_deployment_as_down(db_session, monkeypatch):
    from datetime import UTC, datetime

    from app.deployments import schedule

    monkeypatch.setattr(schedule, "now_utc", lambda: datetime(2026, 11, 7, 6, 0, tzinfo=UTC))  # Saturday
    await service.create(db_session, make_hf(), dep_in(name="day", schedule=WORKDAYS, nodes=["k1"]))
    await service.create(db_session, make_hf(), dep_in(name="always", nodes=["k2"]))

    async def counts():
        return {}

    status = {dep.name: s for dep, _, s in await service.catalog(db_session, counts)}
    assert status == {"day": service.STATUS_DOWN, "always": service.STATUS_STARTING}


async def test_deployment_status_split_groups_leave_spare_nodes_out(db_session):
    dep = await service.create(
        db_session,
        make_hf(),
        dep_in(name="sp", mode="split", accel="cpu", group_size=2, spare_nodes=1, nodes=["a", "b", "c", "d"]),
    )
    k8s = FakeK8s()
    for name in "abcd":
        k8s.add_node(name, accel="cpu")
    status = await service.deployment_status(db_session, k8s, FakeLiteLLM(), dep)
    assert status["groups"] == [["a", "b"]]  # c is the spare, d waits with it
    await service.set_maintenance(db_session, "a", True)  # still counted for the number of groups, never placed
    status = await service.deployment_status(db_session, k8s, FakeLiteLLM(), dep)
    assert status["groups"] == [["b", "c"]]


# ---------- split over VRAM and RAM of the nodes ----------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ram_budget_pct": 9}, "Доля RAM ноды для модели от 10 до 90 %"),
        ({"ram_budget_pct": 91}, "Доля RAM ноды для модели от 10 до 90 %"),
        (
            {"mode": "split", "accel": "nvidia", "group_size": 32},
            "В группе не больше 63 устройств: не больше 31 нод при использовании RAM нод",
        ),
        (
            {"mode": "split", "accel": "nvidia", "group_size": 64, "use_node_ram": False},
            "В группе не больше 63 устройств: не больше 63 нод",
        ),
        (  # accel any: any member may be a GPU node with two devices
            {"mode": "split", "accel": "any", "group_size": 32},
            "В группе не больше 63 устройств: не больше 31 нод при использовании RAM нод",
        ),
    ],
)
async def test_node_ram_fields_are_validated(db_session, overrides, message):
    with pytest.raises(DeploymentInvalid) as err:
        await service.create(db_session, make_hf(), dep_in(**overrides))
    assert err.value.detail == message


async def test_a_cluster_node_wins_over_a_standalone_record_when_assigned(db_session):
    """The stale record of a PC that moved to k3s does not make it a standalone node for validation."""
    k8s = FakeK8s()
    k8s.add_node("shadow-pc", accel="nvidia", vram_mib=8188)
    db_session.add(StandaloneNode(name="shadow-pc", url="http://10.0.0.7:18080", accel="cpu", version="t"))
    await db_session.commit()
    dep = await service.create(db_session, make_hf(), dep_in(mode="split", accel="nvidia", group_size=2), k8s)
    row = await service.assign(db_session, "shadow-pc", dep.id, k8s)  # the record says cpu, the cluster says nvidia
    assert row.source == "k8s"
    dep2 = await service.create(db_session, make_hf(), dep_in(name="m2", mode="split", accel="nvidia", nodes=["shadow-pc"]), k8s)
    assert (await assignment(db_session, "shadow-pc")).deployment_id == dep2.id


async def test_split_on_any_accel_takes_gpu_and_cpu_nodes(db_session):
    dep = await service.create(
        db_session, make_hf(), dep_in(name="mix", mode="split", accel="any", group_size=3, nodes=["g1", "c1", "c2"])
    )
    assert (dep.mode, dep.accel, dep.group_size) == ("split", "any", 3)
    await service.update(db_session, make_hf(), dep.id, DeploymentPatch(group_size=31))  # 31 x 2 devices fit in 63


async def test_node_ram_fields_default_on_and_count_for_split_pods_only(db_session):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in(mode="split", accel="nvidia", group_size=31))
    assert (dep.use_node_ram, dep.ram_budget_pct) == (True, 70)
    h = service.spec_hash(dep)
    dep = await service.update(db_session, hf, dep.id, DeploymentPatch(use_node_ram=False, ram_budget_pct=50))
    assert (dep.use_node_ram, dep.ram_budget_pct) == (False, 50)
    assert service.spec_hash(dep) != h
    rep = await service.create(db_session, hf, dep_in(name="rep"))
    h = service.spec_hash(rep)
    rep = await service.update(db_session, hf, rep.id, DeploymentPatch(ram_budget_pct=40))
    assert service.spec_hash(rep) == h  # replicas use no RAM budget


async def test_meta_keeps_the_layer_layout_and_attention_keys(db_session):
    hf = make_hf()
    hf.metas[(REPO, FILE)] = GgufMeta("deepseek2", 3, 128, 1, 576, 7168, 163840, value_dim=512, mla=True, nextn_layers=1)
    hf.layouts[(REPO, FILE)] = GgufLayout([10, 20, 30], 5, 4, 30, 1000)
    dep = await service.create(db_session, hf, dep_in(mode="split", accel="cpu"))
    keys = ("value_dim", "mla", "nextn_layers", "layers", "output_bytes", "embd_bytes", "max_tensor_bytes", "n_vocab")
    assert {k: dep.meta[k] for k in keys} == {
        "value_dim": 512, "mla": True, "nextn_layers": 1, "layers": [10, 20, 30], "output_bytes": 5, "embd_bytes": 4,
        "max_tensor_bytes": 30, "n_vocab": 1000,
    }
    assert ("gguf_inspect", REPO, (FILE,)) in hf.calls


async def test_only_split_reads_the_layer_layout_and_a_switch_to_split_reads_it_then(db_session):
    hf = make_hf()
    hf.layouts[(REPO, FILE)] = GgufLayout([10] * 32, 5, 4, 10, 1000)
    dep = await service.create(db_session, hf, dep_in())
    assert "layers" not in dep.meta and not any(c[0] == "gguf_inspect" for c in hf.calls)  # replicas: 2 MB, not the table
    dep = await service.update(db_session, hf, dep.id, DeploymentPatch(mode="split", accel="cpu"))
    assert dep.meta["layers"] == [10] * 32
    hf.unavailable = True  # once read, a later change of other fields does not ask Hugging Face again
    dep = await service.update(db_session, hf, dep.id, DeploymentPatch(ram_budget_pct=60))
    assert dep.ram_budget_pct == 60 and dep.meta["layers"] == [10] * 32


async def test_a_switch_to_split_while_hugging_face_is_down_keeps_the_meta(db_session):
    hf = make_hf()
    dep = await service.create(db_session, hf, dep_in())
    hf.unavailable = True
    dep = await service.update(db_session, hf, dep.id, DeploymentPatch(mode="split", accel="cpu"))
    assert dep.mode == "split" and dep.meta["n_layers"] == 32 and "layers" not in dep.meta


async def test_maintenance_of_an_unknown_node_or_of_main_is_refused(db_session):
    k8s = FakeK8s()
    k8s.add_node("llm-main", is_main=True)
    k8s.add_node("pc01", accel="cpu")
    with pytest.raises(service.NodeNotFound):
        await service.set_maintenance(db_session, "ghost", True, k8s)
    with pytest.raises(DeploymentInvalid, match="Главный узел"):
        await service.set_maintenance(db_session, "LLM-MAIN", True, k8s)
    assert (await db_session.scalars(select(NodeAssignment))).all() == []  # nothing was written
    assert (await service.set_maintenance(db_session, "pc01", True, k8s)).maintenance
    db_session.add(standalone("s1"))
    await db_session.commit()
    assert (await service.set_maintenance(db_session, "s1", True)).maintenance  # known without the cluster
    k8s.unavailable = True
    with pytest.raises(K8sUnavailable):  # cannot tell whether the node exists
        await service.set_maintenance(db_session, "pc02", True, k8s)


async def test_a_standalone_node_keeps_serving_during_the_drain(db_session, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.deployments import schedule

    end = datetime(2026, 11, 6, 13, 0, tzinfo=UTC)  # Friday 20:00 in Novosibirsk
    hf = make_hf()
    db_session.add(standalone("s1"))
    await db_session.commit()
    await service.create(db_session, hf, dep_in(name="day", schedule=WORKDAYS, nodes=["s1"]))
    await service.create(db_session, hf, dep_in(name="evening", auto_assign=True))
    monkeypatch.setattr(schedule, "now_utc", lambda: end + timedelta(seconds=10))
    # the heartbeat keeps the node on its model while streams finish; the backend controller (no drain) drops it
    assert (await service.desired_for_node(db_session, "s1", drain_s=30)).deployment == "day"
    assert (await service.desired_for_node(db_session, "s1")).deployment == "evening"
    monkeypatch.setattr(schedule, "now_utc", lambda: end + timedelta(seconds=30))
    assert (await service.desired_for_node(db_session, "s1", drain_s=30)).deployment == "evening"


# ---------- llama.cpp settings (task 2-22a) ----------

DRAFT_REPO = "org/draft-GGUF"
DRAFT_FILE = "draft-Q8_0.gguf"
SHA_A, SHA_B = "a" * 40, "b" * 40


def llm_hf() -> FakeHf:
    hf = make_hf()
    hf.files[DRAFT_REPO] = [HfFile(DRAFT_FILE, GiB), HfFile("draft-Q4.gguf", GiB // 2), HfFile("sub/draft-Q2.gguf", GiB // 4)]
    hf.files["local/tiny"] = [HfFile("tiny-Q8_0.gguf", GiB // 8)]
    hf.files["local/gemma-4"] = [HfFile("mmproj-F16.gguf", GiB // 8)]
    hf.commits = {REPO: SHA_A, DRAFT_REPO: SHA_B}
    return hf


async def test_create_defaults_of_the_llm_settings(db_session):
    dep = await service.create(db_session, make_hf(), dep_in())
    assert (dep.reasoning, dep.kv_cache_type, dep.experimental) == ("off", "q8_0", False)  # new: q8_0
    assert (dep.sampling, dep.draft_repo, dep.draft_file, dep.draft_revision) == (None, None, None, None)
    assert (dep.mmproj_file, dep.extra_args, dep.system_prompt) == (None, None, None)


async def test_rows_made_before_the_settings_keep_f16(db_session):
    await db_session.execute(
        text(
            "insert into model_deployments (name, engine, repo, file, mode, group_size, accel, ctx_per_request,"
            " parallel, auto_assign, enabled) values ('old', 'llamacpp', 'o/r', 'f.gguf', 'replicas', 2, 'any',"
            " 4096, 2, true, true)"
        )
    )
    await db_session.commit()
    old = await service.get_by_name(db_session, "old")
    assert (old.reasoning, old.kv_cache_type, old.sampling, old.experimental) == ("off", "f16", None, False)


async def test_create_and_patch_store_the_settings_and_empty_values_become_null(db_session):
    hf = llm_hf()
    dep = await service.create(
        db_session, hf,
        dep_in(
            reasoning="auto", kv_cache_type="q4_0", sampling={"temperature": 0.7, "top_k": 20},
            draft_repo=f" {DRAFT_REPO} ", draft_file=DRAFT_FILE, mmproj_file="mmproj-f16.gguf",
            extra_args=" --mlock ", system_prompt="Ты помощник.", experimental=True,
        ),
    )
    assert (dep.reasoning, dep.kv_cache_type, dep.experimental) == ("auto", "q4_0", True)
    assert dep.sampling == {"temperature": 0.7, "top_k": 20}
    assert (dep.draft_repo, dep.draft_file, dep.draft_revision) == (DRAFT_REPO, DRAFT_FILE, SHA_B)
    assert (dep.mmproj_file, dep.extra_args, dep.system_prompt) == ("mmproj-f16.gguf", "--mlock", "Ты помощник.")
    dep = await service.update(
        db_session, hf, dep.id,
        DeploymentPatch(sampling={}, mmproj_file=" ", extra_args="", system_prompt=" \n\t ", draft_repo=None, draft_file=None),
    )
    assert (dep.sampling, dep.mmproj_file, dep.extra_args, dep.system_prompt) == (None, None, None, None)
    assert (dep.draft_repo, dep.draft_file, dep.draft_revision) == (None, None, None)
    assert (dep.reasoning, dep.kv_cache_type, dep.experimental) == ("auto", "q4_0", True)  # not sent: unchanged


async def test_patch_missing_fields_leave_the_settings_unchanged(db_session):
    hf = llm_hf()
    dep = await service.create(db_session, hf, dep_in(kv_cache_type="q4_0", sampling={"top_p": 0.5}, extra_args="--mlock"))
    dep = await service.update(db_session, hf, dep.id, DeploymentPatch(parallel=3))
    assert (dep.kv_cache_type, dep.sampling, dep.extra_args) == ("q4_0", {"top_p": 0.5}, "--mlock")


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"reasoning": "maybe"}, "Рассуждения"),
        ({"kv_cache_type": "q5_0"}, "KV-кэша"),
        ({"sampling": {"temperature": 3}}, "temperature"),
        ({"sampling": {"seed": 1}}, "Неизвестный параметр сэмплинга: seed"),
        ({"sampling": {"top_k": 1.5}}, "целое"),
        ({"sampling": [1]}, "Сэмплинг: объект с параметрами"),
        ({"sampling": "hot"}, "Сэмплинг: объект с параметрами"),
        ({"draft_repo": DRAFT_REPO}, "нужны и репозиторий, и файл"),
        ({"draft_file": DRAFT_FILE}, "нужны и репозиторий, и файл"),
        ({"draft_repo": "not a repo", "draft_file": DRAFT_FILE}, "Репозиторий черновой модели"),
        ({"draft_repo": DRAFT_REPO, "draft_file": "d.bin"}, ".gguf"),
        ({"draft_repo": DRAFT_REPO, "draft_file": "../d.gguf"}, "Файл черновой модели"),
        ({"draft_repo": DRAFT_REPO, "draft_file": "/etc/d.gguf"}, "Файл черновой модели"),
        ({"mmproj_file": "mmproj.bin"}, ".gguf"),
        ({"mmproj_file": "../mmproj.gguf"}, "проектора"),
        ({"mmproj_file": "/abs/mmproj.gguf"}, "проектора"),
        ({"mmproj_file": "local/m/../x.gguf"}, "проектора"),
        ({"mmproj_file": "local/m"}, "проектора"),
        ({"mmproj_file": "my file.gguf"}, "проектора"),
        ({"mmproj_file": "a$b.gguf"}, "проектора"),
        ({"mmproj_file": "a;b.gguf"}, "проектора"),
        ({"mmproj_file": "a`b.gguf"}, "проектора"),
        ({"mmproj_file": "a'b.gguf"}, "проектора"),
        ({"mmproj_file": 'a"b.gguf'}, "проектора"),
        ({"mmproj_file": "local/gemma-4/my file.gguf"}, "проектора"),
        ({"mmproj_file": "local/bad name/m.gguf"}, "проектора"),
        ({"mmproj_file": "absent.gguf"}, "Файл проектора изображений не найден в репозитории"),
        ({"mmproj_file": "local/gemma-4/absent.gguf"}, "Файл проектора изображений не найден в репозитории"),
        ({"draft_repo": DRAFT_REPO, "draft_file": "my file.gguf"}, "Файл черновой модели"),
        ({"draft_repo": DRAFT_REPO, "draft_file": "a$b.gguf"}, "Файл черновой модели"),
        ({"draft_repo": DRAFT_REPO, "draft_file": "a;b.gguf"}, "Файл черновой модели"),
        ({"draft_repo": DRAFT_REPO, "draft_file": "a`b.gguf"}, "Файл черновой модели"),
        ({"draft_repo": DRAFT_REPO, "draft_file": "a'b.gguf"}, "Файл черновой модели"),
        ({"draft_repo": DRAFT_REPO, "draft_file": 'a"b.gguf'}, "Файл черновой модели"),
        ({"draft_repo": DRAFT_REPO, "draft_file": "absent.gguf"}, "Файл черновой модели не найден в репозитории"),
        ({"draft_repo": "local/tiny", "draft_file": "absent.gguf"}, "Файл черновой модели не найден в репозитории"),
        ({"draft_repo": "local/bad name", "draft_file": "tiny-Q8_0.gguf"}, "Репозиторий черновой модели"),
        ({"draft_repo": "o/" + "r" * 600, "draft_file": DRAFT_FILE}, "не длиннее 512"),
        ({"extra_args": "--port 1"}, "Флаг --port задаётся порталом"),
        ({"extra_args": "--foo /etc/passwd"}, "Пути к файлам"),
        ({"extra_args": "--a " * 41}, "не больше 40"),
        ({"system_prompt": "x" * 8001}, "8000"),
    ],
)
async def test_invalid_llm_settings_are_refused_in_russian(db_session, kw, reason):
    with pytest.raises(DeploymentInvalid) as err:
        await service.create(db_session, llm_hf(), dep_in(**kw))
    assert reason in err.value.detail
    assert await count(db_session, ModelDeployment) == 0


async def test_patch_validates_the_merged_draft_pair(db_session):
    hf = llm_hf()
    dep = await service.create(db_session, hf, dep_in(draft_repo=DRAFT_REPO, draft_file=DRAFT_FILE))
    with pytest.raises(DeploymentInvalid, match="нужны и репозиторий, и файл"):
        await service.update(db_session, hf, dep.id, DeploymentPatch(draft_file=None))  # the repo stays: half a pair
    with pytest.raises(DeploymentInvalid, match="Флаг --host"):
        await service.update(db_session, hf, dep.id, DeploymentPatch(extra_args="--host 0.0.0.0"))
    assert (await service.get(db_session, dep.id)).draft_file == DRAFT_FILE


async def test_system_prompt_of_8000_characters_and_a_local_mmproj_are_accepted(db_session):
    dep = await service.create(
        db_session, llm_hf(), dep_in(system_prompt="я" * 8000, mmproj_file="local/gemma-4/mmproj-F16.gguf")
    )
    assert len(dep.system_prompt) == 8000 and dep.mmproj_file == "local/gemma-4/mmproj-F16.gguf"


async def test_draft_revision_is_pinned_like_the_revision(db_session):
    hf = llm_hf()
    dep = await service.create(db_session, hf, dep_in(draft_repo=DRAFT_REPO, draft_file=DRAFT_FILE))
    assert (dep.revision, dep.draft_revision) == (SHA_A, SHA_B)
    hf.commits[DRAFT_REPO] = SHA_A
    dep = await service.update(db_session, hf, dep.id, DeploymentPatch(parallel=3))
    assert dep.draft_revision == SHA_B  # a change that is not the draft one keeps the pin
    dep = await service.update(db_session, hf, dep.id, DeploymentPatch(draft_file="draft-Q4.gguf"))
    assert dep.draft_revision == SHA_A  # another draft file: pinned at the commit of main now
    hf.commits[DRAFT_REPO] = SHA_B
    dep, changed = await service.refresh_revision(db_session, hf, dep.id)
    assert changed and dep.draft_revision == SHA_B and dep.revision == SHA_A
    dep, changed = await service.refresh_revision(db_session, hf, dep.id)
    assert not changed


async def test_a_local_draft_has_no_pin(db_session):
    hf = llm_hf()
    dep = await service.create(db_session, hf, dep_in(draft_repo="local/tiny", draft_file="tiny-Q8_0.gguf"))
    assert dep.draft_repo == "local/tiny" and dep.draft_revision is None  # FakeHf: no commit for a repo it does not know


def test_spec_hash_includes_the_pod_settings_and_not_the_gateway_ones():
    from types import SimpleNamespace

    base = dict(
        engine="llamacpp", repo=REPO, file=FILE, mode="replicas", group_size=2, ctx_per_request=4096, parallel=2,
        use_node_ram=True, ram_budget_pct=70, revision=None, reasoning="off", kv_cache_type="f16", sampling=None,
        draft_repo=None, draft_file=None, draft_revision=None, mmproj_file=None, extra_args=None,
        system_prompt=None, experimental=False,
    )
    old = service.spec_hash(SimpleNamespace(**{k: base[k] for k in base if k not in service._LLM_FIELDS}))
    assert service.spec_hash(SimpleNamespace(**base)) == old  # a row made before the settings keeps its pods

    def h(**kw):
        return service.spec_hash(SimpleNamespace(**{**base, **kw}))

    assert h(system_prompt="Ты помощник.") == old and h(experimental=True) == old  # gateway side: no restart
    assert h(system_prompt="a") == h(system_prompt="b")
    changed = {
        "reasoning": h(reasoning="on"), "kv": h(kv_cache_type="q8_0"), "kv4": h(kv_cache_type="q4_0"),
        "sampling": h(sampling={"temperature": 0.5}), "sampling2": h(sampling={"temperature": 0.6}),
        "draft": h(draft_repo=DRAFT_REPO, draft_file=DRAFT_FILE, draft_revision=SHA_A),
        "draft_file": h(draft_repo=DRAFT_REPO, draft_file="other.gguf", draft_revision=SHA_A),
        "draft_rev": h(draft_repo=DRAFT_REPO, draft_file=DRAFT_FILE, draft_revision=SHA_B),
        "mmproj": h(mmproj_file="mmproj-f16.gguf"), "args": h(extra_args="--mlock"),
    }
    assert old not in changed.values() and len(set(changed.values())) == len(changed)
    assert h(sampling={"temperature": 1}) == h(sampling={"temperature": 1.0})


async def test_changing_kv_cache_type_changes_the_hash_and_the_prompt_and_experimental_do_not(db_session):
    hf = llm_hf()
    dep = await service.create(db_session, hf, dep_in())
    h = service.spec_hash(dep)
    dep = await service.update(db_session, hf, dep.id, DeploymentPatch(system_prompt="Отвечай кратко.", experimental=True))
    assert service.spec_hash(dep) == h
    dep = await service.update(db_session, hf, dep.id, DeploymentPatch(kv_cache_type="f16"))
    assert service.spec_hash(dep) != h
    for kw in ({"reasoning": "on"}, {"sampling": {"top_p": 0.9}}, {"mmproj_file": "mmproj-f16.gguf"}, {"extra_args": "--mlock"}):
        before = service.spec_hash(dep)
        dep = await service.update(db_session, hf, dep.id, DeploymentPatch(**kw))
        assert service.spec_hash(dep) != before, kw


async def test_a_draft_file_in_a_subdirectory_is_accepted(db_session):
    dep = await service.create(db_session, llm_hf(), dep_in(draft_repo=DRAFT_REPO, draft_file="sub/draft-Q2.gguf"))
    assert dep.draft_file == "sub/draft-Q2.gguf"


async def test_the_files_are_looked_up_only_when_new_or_changed(db_session):
    hf = llm_hf()
    dep = await service.create(
        db_session, hf, dep_in(draft_repo=DRAFT_REPO, draft_file=DRAFT_FILE, mmproj_file="mmproj-f16.gguf")
    )
    hf.calls.clear()
    await service.update(db_session, hf, dep.id, DeploymentPatch(parallel=3))
    assert [c for c in hf.calls if c[0] == "list_files"] == []  # nothing about the files changed: no lookup
    await service.update(db_session, hf, dep.id, DeploymentPatch(mmproj_file="mmproj-f16.gguf", draft_file=DRAFT_FILE))
    assert [c for c in hf.calls if c[0] == "list_files"] == []  # the same values again
    with pytest.raises(DeploymentInvalid, match="Файл черновой модели не найден"):
        await service.update(db_session, hf, dep.id, DeploymentPatch(draft_file="absent.gguf"))
    with pytest.raises(DeploymentInvalid, match="Файл проектора изображений не найден"):
        await service.update(db_session, hf, dep.id, DeploymentPatch(mmproj_file="absent.gguf"))
    got = await service.get(db_session, dep.id)
    assert (got.draft_file, got.mmproj_file) == (DRAFT_FILE, "mmproj-f16.gguf")
