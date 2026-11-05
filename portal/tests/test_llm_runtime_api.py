"""Task 2-22b over the admin API: the estimate with the KV type, the draft model and the projector; the command preview."""

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.catalog import estimate as est
from app.catalog.gguf import GgufMeta
from app.catalog.hf import HfFile
from app.deployments import service
from app.models import ModelDeployment, NodeAssignment, PortalFlag
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

GiB = 2**30
MiB = 2**20
REPO = "org/model-GGUF"
FILE = "model-Q4_K_M.gguf"
MMPROJ = "mmproj-F16.gguf"
DRAFT_REPO = "org/draft-GGUF"
DRAFT_FILE = "draft-Q8_0.gguf"
SHA = "c" * 40
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru"}
META = GgufMeta("llama", 32, 32, 8, 128, 4096, 32768)
DRAFT_META = GgufMeta("llama", 12, 8, 2, 64, 512, 8192)
PARAMS = {"repo": REPO, "file": FILE, "ctx_per_request": 8192, "parallel": 2, "nodes": "g1"}


@pytest.fixture
def settings(settings):
    settings.hf_endpoint = "http://127.0.0.1:9"
    return settings


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    async with app.state.sessionmaker() as session:
        await session.execute(delete(NodeAssignment))
        await session.execute(delete(ModelDeployment))
        await session.execute(delete(PortalFlag))
        await session.commit()
    app.state.hf = FakeHf(
        files={
            REPO: [HfFile(FILE, 4 * GiB), HfFile(MMPROJ, 900 * MiB)],
            DRAFT_REPO: [HfFile(DRAFT_FILE, 700 * MiB)],
        },
        metas={(REPO, FILE): META, (DRAFT_REPO, DRAFT_FILE): DRAFT_META},
        commits={REPO: SHA, DRAFT_REPO: "d" * 40},
    )
    k8s = FakeK8s()
    k8s.add_node("g1", accel="nvidia", vram_mib=16 * 1024)
    app.state.k8s = k8s
    app.state.litellm = FakeLiteLLM()


def body(**kw) -> dict:
    return {"name": "m1", "repo": REPO, "file": FILE, **kw}


async def admin(client, app) -> dict:
    return csrf_headers(await login(client, app, ADMIN))


async def test_estimate_follows_the_kv_cache_type(client, app):
    await admin(client, app)
    f16 = (await client.get("/api/admin/hf/estimate", params=PARAMS)).json()["estimate"]
    q8 = (await client.get("/api/admin/hf/estimate", params={**PARAMS, "kv_cache_type": "q8_0"})).json()["estimate"]
    q4 = (await client.get("/api/admin/hf/estimate", params={**PARAMS, "kv_cache_type": "q4_0"})).json()["estimate"]
    assert f16["kv_bytes"] == 2 * 32 * 8 * 128 * 16384 * 2
    assert q8["kv_bytes"] == f16["kv_bytes"] * 17 // 32  # 34 of 64 bytes per element
    assert q4["kv_bytes"] == f16["kv_bytes"] * 9 // 32
    assert q8["total_bytes"] == q8["weights_bytes"] + q8["kv_bytes"]
    r = await client.get("/api/admin/hf/estimate", params={**PARAMS, "kv_cache_type": "q5_1"})
    assert r.status_code == 422


async def test_estimate_has_the_draft_and_the_projector_parts(client, app):
    await admin(client, app)
    base = (await client.get("/api/admin/hf/estimate", params=PARAMS)).json()["estimate"]
    assert (base["draft_weights_bytes"], base["draft_kv_bytes"], base["mmproj_bytes"]) == (0, 0, 0)
    r = await client.get(
        "/api/admin/hf/estimate",
        params={**PARAMS, "draft_repo": DRAFT_REPO, "draft_file": DRAFT_FILE, "mmproj_file": MMPROJ},
    )
    assert r.status_code == 200, r.text
    e = r.json()["estimate"]
    draft_kv = est.kv_bytes(DRAFT_META, 8192 * 2, parallel=2)
    assert (e["draft_weights_bytes"], e["draft_kv_bytes"], e["mmproj_bytes"]) == (700 * MiB, draft_kv, 900 * MiB)
    extra = 700 * MiB + draft_kv + 900 * MiB
    assert e["kv_bytes"] == base["kv_bytes"] and e["total_bytes"] == base["total_bytes"] + extra
    assert e["per_node_bytes"] == base["per_node_bytes"] + extra
    # a projector that is not in the repository counts nothing
    r = await client.get("/api/admin/hf/estimate", params={**PARAMS, "mmproj_file": "absent.gguf"})
    assert r.json()["estimate"]["mmproj_bytes"] == 0


async def test_estimate_checks_the_draft_choice(client, app):
    await admin(client, app)
    r = await client.get("/api/admin/hf/estimate", params={**PARAMS, "draft_repo": DRAFT_REPO})
    assert r.status_code == 422 and "репозиторий, и файл" in r.json()["detail"]
    r = await client.get("/api/admin/hf/estimate", params={**PARAMS, "draft_repo": "nope", "draft_file": "d.gguf"})
    assert r.status_code == 422
    r = await client.get("/api/admin/hf/estimate", params={**PARAMS, "draft_repo": "org/absent", "draft_file": "d.gguf"})
    assert r.status_code == 404


async def test_the_sizes_of_the_draft_and_the_projector_are_stored_with_the_deployment(client, app):
    h = await admin(client, app)
    r = await client.post(
        "/api/admin/deployments",
        json=body(draft_repo=DRAFT_REPO, draft_file=DRAFT_FILE, mmproj_file=MMPROJ, kv_cache_type="q8_0"), headers=h,
    )
    assert r.status_code == 201, r.text
    dep_id = r.json()["id"]
    async with app.state.sessionmaker() as db:
        dep = await service.get(db, dep_id)
        stored = dep.meta["extras"]
        assert stored["key"] == est.extras_key(DRAFT_REPO, DRAFT_FILE, MMPROJ)
        assert stored["mmproj_bytes"] == 900 * MiB and stored["draft"]["weights_bytes"] == 700 * MiB
        assert est.dep_extras(dep)[0] == 700 * MiB and est.dep_extras(dep)[2] == 900 * MiB
    # another projector: the stored sizes are read again; none: they are dropped
    r = await client.patch(f"/api/admin/deployments/{dep_id}", json={"mmproj_file": None, "draft_repo": None, "draft_file": None}, headers=h)
    assert r.status_code == 200, r.text
    async with app.state.sessionmaker() as db:
        assert "extras" not in (await service.get(db, dep_id)).meta
    # a projector the repository does not list is refused by the create/patch checks (2-22a)
    app.state.hf.files[REPO] = [HfFile(FILE, 4 * GiB)]
    r = await client.patch(f"/api/admin/deployments/{dep_id}", json={"mmproj_file": MMPROJ}, headers=h)
    assert r.status_code == 422


async def test_command_preview_of_an_unsaved_form(client, app):
    h = await admin(client, app)
    r = await client.post(
        "/api/admin/deployments/command-preview",
        json=body(
            kv_cache_type="q8_0", reasoning="auto", sampling={"temperature": 0.5}, mmproj_file=MMPROJ,
            draft_repo=DRAFT_REPO, draft_file=DRAFT_FILE, extra_args="--no-warmup", accel="nvidia",
        ),
        headers=h,
    )
    assert r.status_code == 200, r.text
    p = r.json()
    assert set(p) == {"leader", "rpc", "notes"} and p["rpc"] is None
    for fragment in (
        "llama-server", "--cache-type-k q8_0", "--reasoning auto", "--temp 0.5", f"--mmproj /var/lib/llm-models/org__model-GGUF/{MMPROJ}",
        f"-md /var/lib/llm-models/org__draft-GGUF/{DRAFT_FILE}", "-ngld 99", "-m /var/lib/llm-models/org__model-GGUF/" + FILE,
    ):
        assert fragment in p["leader"], fragment
    assert p["leader"].endswith("--no-warmup")
    assert "LLAMA_API_KEY=***" in p["leader"] and "--api-key" not in p["leader"]
    assert (await client.get("/api/admin/deployments")).json() == []  # nothing was saved


async def test_command_preview_of_a_split_form_has_both_commands(client, app):
    h = await admin(client, app)
    r = await client.post(
        "/api/admin/deployments/command-preview", json=body(mode="split", group_size=2, accel="nvidia"), headers=h
    )
    assert r.status_code == 200, r.text
    p = r.json()
    assert "--rpc" in p["leader"] and "-ngl 999" in p["leader"] and p["rpc"].startswith("LLAMA_CACHE=")


async def test_command_preview_validates_like_a_create(client, app):
    h = await admin(client, app)
    r = await client.post("/api/admin/deployments/command-preview", json=body(extra_args="--port 80"), headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "Флаг --port задаётся порталом"
    r = await client.post("/api/admin/deployments/command-preview", json=body(extra_args="--my-opt /etc/x"), headers=h)
    assert r.status_code == 422 and "Пути к файлам" in r.json()["detail"]
    r = await client.post("/api/admin/deployments/command-preview", json=body(draft_repo=DRAFT_REPO), headers=h)
    assert r.status_code == 422


async def test_command_of_a_saved_deployment(client, app):
    h = await admin(client, app)
    created = (await client.post("/api/admin/deployments", json=body(kv_cache_type="q4_0"), headers=h)).json()
    r = await client.get(f"/api/admin/deployments/{created['id']}/command")
    assert r.status_code == 200, r.text
    assert "--cache-type-k q4_0 --cache-type-v q4_0" in r.json()["leader"]
    assert f"-m /var/lib/llm-models/org__model-GGUF/{FILE}" in r.json()["leader"]  # pinned: downloaded by the script
    assert (await client.get("/api/admin/deployments/9999/command")).status_code == 404


async def test_the_command_endpoints_are_for_admins(client, app):
    h = csrf_headers(await login(client, app, USER))
    assert (await client.post("/api/admin/deployments/command-preview", json=body(), headers=h)).status_code == 403
    assert (await client.get("/api/admin/deployments/1/command")).status_code == 403


async def test_mmproj_files_of_a_repository(client, app):
    await admin(client, app)
    app.state.hf.files[REPO] = [
        HfFile(FILE, 4 * GiB), HfFile("mmproj-BF16.gguf", 1000 * MiB), HfFile(MMPROJ, 900 * MiB),
        HfFile("sub/mmproj-model-f32.gguf", 1800 * MiB), HfFile("README.md", 10),
    ]
    r = await client.get("/api/admin/hf/mmproj", params={"repo": REPO})
    assert r.status_code == 200, r.text
    assert r.json() == [
        {"file": "mmproj-BF16.gguf", "size_bytes": 1000 * MiB, "quant": "BF16"},
        {"file": MMPROJ, "size_bytes": 900 * MiB, "quant": "F16"},
        {"file": "sub/mmproj-model-f32.gguf", "size_bytes": 1800 * MiB, "quant": "F32"},
    ]
    assert (await client.get("/api/admin/hf/mmproj", params={"repo": "org/absent"})).status_code == 404
    assert (await client.get("/api/admin/hf/mmproj", params={"repo": "bad"})).status_code == 422
