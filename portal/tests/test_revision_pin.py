"""The Hugging Face commit of a deployment is pinned when it is created or its model changes (task 2-15)."""

from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import delete

from app.catalog.gguf import GgufMeta
from app.catalog.hf import HfClient, HfFile, HfNotFound, HfUnavailable, MirrorFirstHf
from app.catalog.sources import ModelSources
from app.config import Settings
from app.deployments import service
from app.deployments.service import DeploymentIn, DeploymentPatch
from app.models import ModelDeployment, NodeAssignment, PortalFlag
from app.workloads.builders import RpcWorker, replicas_daemonset, split_leader, vllm_daemonset
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

EP = "https://hf.example"
SHA1 = "a" * 40
SHA2 = "b" * 40
REPO = "org/model-GGUF"
FILE = "model-Q4_K_M.gguf"
FILE8 = "model-Q8_0.gguf"
GiB = 2**30
META = GgufMeta("llama", 32, 32, 8, 128, 4096, 32768)
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru"}


# ---------- the Hugging Face client ----------


@respx.mock
async def test_commit_reads_the_sha_of_main():
    route = respx.get(f"{EP}/api/models/o/r/revision/main").mock(
        return_value=httpx.Response(200, json={"id": "o/r", "sha": SHA1, "siblings": []})
    )
    assert await HfClient(EP, "tok").commit("o/r") == SHA1
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
async def test_commit_errors():
    respx.get(f"{EP}/api/models/o/missing/revision/main").mock(return_value=httpx.Response(404))
    with pytest.raises(HfNotFound):
        await HfClient(EP, None).commit("o/missing")
    respx.get(f"{EP}/api/models/o/bad/revision/main").mock(return_value=httpx.Response(200, json={"sha": "main"}))
    with pytest.raises(HfUnavailable):
        await HfClient(EP, None).commit("o/bad")
    respx.get(f"{EP}/api/models/o/down/revision/main").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(HfUnavailable):
        await HfClient(EP, None).commit("o/down")


@respx.mock
async def test_commit_asks_the_mirror_first():
    mirror = respx.get("https://main/hf/api/models/o/r/revision/main").mock(
        return_value=httpx.Response(200, json={"sha": SHA2})
    )
    hf = MirrorFirstHf(HfClient("https://main/hf", "m"), HfClient(EP, None))
    assert await hf.commit("o/r") == SHA2
    assert mirror.called


async def test_sources_never_pin_an_own_model():
    hf = FakeHf(commits={"o/r": SHA1})
    sources = ModelSources(hf, local=object())
    assert await sources.commit("local/my-model") is None
    assert await sources.commit("o/r") == SHA1


# ---------- builders and the spec hash ----------


def dep(**kw):
    base = dict(
        name="m1", engine="llamacpp", repo=REPO, file=FILE, mode="replicas", group_size=2, accel="any",
        ctx_per_request=8192, parallel=2, meta={"arch": "llama", "weights_bytes": 5 * 10**9}, use_node_ram=True,
        ram_budget_pct=70, revision=None,
    )
    return SimpleNamespace(**{**base, **kw})


S = Settings(node_image_tag="0.1.0", hf_mirror_enabled=False)


def env_of(manifest) -> dict[str, str]:
    c = manifest["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e["value"] for e in c.get("env", []) if "value" in e}


def test_spec_hash_of_an_unpinned_deployment_is_unchanged():
    # a NULL revision (every deployment made before 2-15) keeps its hash: the upgrade restarts no pod
    legacy = SimpleNamespace(**{k: v for k, v in vars(dep()).items() if k != "revision"})
    assert service.spec_hash(dep()) == service.spec_hash(legacy)
    assert service.spec_hash(dep(revision=SHA1)) != service.spec_hash(dep())
    assert service.spec_hash(dep(revision=SHA1)) != service.spec_hash(dep(revision=SHA2))


def test_pods_get_the_pinned_revision():
    assert env_of(replicas_daemonset(dep(revision=SHA1), "cpu", S))["MODEL_REVISION"] == SHA1
    assert "MODEL_REVISION" not in env_of(replicas_daemonset(dep(), "cpu", S))
    group = [RpcWorker("a", "10.0.0.1", 18050, "cpu"), RpcWorker("b", "10.0.0.2", 18050, "cpu")]
    leader = split_leader(dep(mode="split", revision=SHA1), 0, group, S)
    assert env_of(leader)["MODEL_REVISION"] == SHA1


def test_vllm_gets_the_pinned_revision():
    args = vllm_daemonset(dep(engine="vllm", file=None, revision=SHA1), S)["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[args.index("--revision") + 1] == SHA1
    args = vllm_daemonset(dep(engine="vllm", file=None), S)["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--revision" not in args


# ---------- the service and the API (Postgres) ----------


def make_hf() -> FakeHf:
    return FakeHf(
        files={REPO: [HfFile(FILE, 4 * GiB), HfFile(FILE8, 8 * GiB)], "org/other-GGUF": [HfFile(FILE, 4 * GiB)]},
        metas={(REPO, FILE): META, (REPO, FILE8): META, ("org/other-GGUF", FILE): META},
        commits={REPO: SHA1, "org/other-GGUF": SHA2},
    )


@pytest.fixture
def settings(settings):
    settings.hf_endpoint = "http://127.0.0.1:9"
    return settings


@pytest_asyncio.fixture
async def clean(app):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(NodeAssignment))
        await db.execute(delete(ModelDeployment))
        await db.execute(delete(PortalFlag))
        await db.commit()
    app.state.hf = make_hf()
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()


@pytest.mark.docker
async def test_create_pins_and_a_model_change_repins(db_session, clean):
    hf = make_hf()
    d = await service.create(db_session, hf, DeploymentIn(name="m1", repo=REPO, file=FILE))
    assert d.revision == SHA1
    hf.commits[REPO] = SHA2  # the author uploads again: nothing changes until the model is changed
    d = await service.update(db_session, hf, d.id, DeploymentPatch(ctx_per_request=4096))
    assert d.revision == SHA1
    d = await service.update(db_session, hf, d.id, DeploymentPatch(file=FILE8))
    assert d.revision == SHA2
    d = await service.update(db_session, hf, d.id, DeploymentPatch(repo="org/other-GGUF", file=FILE))
    assert d.revision == SHA2 and ("commit", "org/other-GGUF") in hf.calls


@pytest.mark.docker
async def test_create_without_hugging_face_fails(db_session, clean):
    hf = make_hf()
    hf.commit_unavailable = True
    with pytest.raises(HfUnavailable):
        await service.create(db_session, hf, DeploymentIn(name="m1", repo=REPO, file=FILE))


@pytest.mark.docker
async def test_refresh_revision(db_session, clean):
    hf = make_hf()
    d = await service.create(db_session, hf, DeploymentIn(name="m1", repo=REPO, file=FILE))
    before = service.spec_hash(d)
    d, changed = await service.refresh_revision(db_session, hf, d.id)
    assert not changed and d.revision == SHA1 and service.spec_hash(d) == before
    hf.commits[REPO] = SHA2
    d, changed = await service.refresh_revision(db_session, hf, d.id)
    assert changed and d.revision == SHA2 and service.spec_hash(d) != before  # the pods roll to the new commit


@pytest.mark.docker
async def test_a_legacy_deployment_stays_on_main_until_refreshed(db_session, clean):
    hf = make_hf()
    d = await service.create(db_session, hf, DeploymentIn(name="m1", repo=REPO, file=FILE))
    d.revision = None  # made before 2-15
    await db_session.commit()
    d = await service.update(db_session, hf, d.id, DeploymentPatch(parallel=1))
    assert d.revision is None
    d, changed = await service.refresh_revision(db_session, hf, d.id)
    assert changed and d.revision == SHA1


@pytest.mark.docker
async def test_standalone_config_carries_the_revision(db_session, clean):
    hf = make_hf()
    d = await service.create(db_session, hf, DeploymentIn(name="m1", repo=REPO, file=FILE, nodes=["pc1"]))
    from app.models import StandaloneNode

    db_session.add(StandaloneNode(name="pc1", url="http://10.0.0.1:18080", accel="cpu"))
    row = await db_session.get(NodeAssignment, "pc1")
    row.source = "standalone"
    await db_session.commit()
    desired = await service.desired_for_node(db_session, "pc1")
    assert desired is not None and desired.model_revision == SHA1 and desired.spec_hash == service.spec_hash(d)


@pytest.mark.docker
async def test_api_shows_and_refreshes_the_revision(client, app, clean):
    h = csrf_headers(await login(client, app, ADMIN))
    r = await client.post("/api/admin/deployments", json={"name": "m1", "repo": REPO, "file": FILE}, headers=h)
    assert r.status_code == 201, r.text
    dep_id = r.json()["id"]
    assert r.json()["revision"] == SHA1
    r = await client.post(f"/api/admin/deployments/{dep_id}/refresh-revision", headers=h)
    assert r.status_code == 200 and r.json()["changed"] is False and r.json()["deployment"]["revision"] == SHA1
    app.state.hf.commits[REPO] = SHA2
    r = await client.post(f"/api/admin/deployments/{dep_id}/refresh-revision", headers=h)
    assert r.status_code == 200 and r.json()["changed"] is True and r.json()["deployment"]["revision"] == SHA2
    app.state.hf.commit_unavailable = True
    r = await client.post(f"/api/admin/deployments/{dep_id}/refresh-revision", headers=h)
    assert r.status_code == 502 and r.json()["detail"] == "Hugging Face недоступен"
    r = await client.post(f"/api/admin/deployments/{dep_id}/refresh-revision", headers={})
    assert r.status_code == 403
    assert (await client.post("/api/admin/deployments/999/refresh-revision", headers=h)).status_code == 404
