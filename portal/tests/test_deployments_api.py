import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.catalog.gguf import GgufMeta
from app.catalog.hf import HfFile, VllmInfo
from app.deployments import service
from app.models import ModelDeployment, NodeAssignment, PortalFlag, StandaloneNode
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

GiB = 2**30
REPO = "org/model-GGUF"
FILE = "model-Q4_K_M.gguf"
BIG_REPO = "org/big-GGUF"
BIG_FILE = "big-Q4_K_M.gguf"
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru"}
SMALL = GgufMeta("llama", 32, 32, 8, 128, 4096, 32768)
# a 31B-like model: Q4 weights ~18.7 GiB, 60 layers, 16 KV heads of 128
BIG = GgufMeta("gemma", 60, 32, 16, 128, 5376, 131072)


@pytest.fixture
def settings(settings):
    settings.hf_endpoint = "http://127.0.0.1:9"
    return settings


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(NodeAssignment))
        await db.execute(delete(ModelDeployment))
        await db.execute(delete(PortalFlag))
        await db.commit()
    app.state.hf = FakeHf(
        files={
            REPO: [HfFile(FILE, 4 * GiB), HfFile("model-Q8_0.gguf", 8 * GiB)],
            BIG_REPO: [HfFile(BIG_FILE, int(18.7 * GiB))],
        },
        metas={(REPO, FILE): SMALL, (REPO, "model-Q8_0.gguf"): SMALL, (BIG_REPO, BIG_FILE): BIG},
        vllm={"org/vllm": VllmInfo(10 * GiB, SMALL)},
    )
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()


def body(**kw) -> dict:
    return {"name": "m1", "repo": REPO, "file": FILE, **kw}


async def as_admin(client, app) -> dict:
    return csrf_headers(await login(client, app, ADMIN))


async def test_admin_routes_forbidden_for_user(client, app):
    h = csrf_headers(await login(client, app, USER))
    for method, url in [
        ("GET", "/api/admin/deployments"),
        ("POST", "/api/admin/deployments"),
        ("GET", "/api/admin/deployments/1"),
        ("PATCH", "/api/admin/deployments/1"),
        ("DELETE", "/api/admin/deployments/1"),
        ("GET", "/api/admin/deployments/1/status"),
        ("PUT", "/api/admin/nodes/n1/assignment"),
        ("GET", f"/api/admin/hf/files?repo={REPO}"),
        ("GET", f"/api/admin/hf/estimate?repo={REPO}&file={FILE}"),
    ]:
        r = await client.request(method, url, headers=h, json=body() if method in ("POST", "PATCH", "PUT") else None)
        assert r.status_code == 403, (method, url, r.status_code)


async def test_admin_routes_require_login(client):
    assert (await client.get("/api/admin/deployments")).status_code == 401


async def test_create_requires_csrf(client, app):
    await login(client, app, ADMIN)
    assert (await client.post("/api/admin/deployments", json=body())).status_code == 403
    assert (await client.post("/api/admin/deployments", json=body(), headers={"X-CSRF-Token": "bad"})).status_code == 403
    assert (await client.get("/api/admin/deployments")).json() == []
    for method, url in [("PATCH", "/api/admin/deployments/1"), ("DELETE", "/api/admin/deployments/1"),
                        ("PUT", "/api/admin/nodes/n1/assignment")]:
        assert (await client.request(method, url, json={})).status_code == 403


async def test_full_admin_flow_create_patch_ctx_delete(client, app):
    h = await as_admin(client, app)
    r = await client.post("/api/admin/deployments", json=body(nodes=["n1"]), headers=h)
    assert r.status_code == 201, r.text
    created = r.json()
    dep_id = created["id"]
    assert created["name"] == "m1" and created["assigned_nodes"] == ["n1"]
    assert created["meta"]["max_ctx"] == 32768 and created["meta_missing"] is False
    async with app.state.sessionmaker() as db:
        assert created["spec_hash"] == service.spec_hash(await service.get(db, dep_id))

    r = await client.get("/api/admin/deployments")
    (item,) = r.json()
    assert item == created  # list items carry the same fields as the single GET
    assert (await client.get(f"/api/admin/deployments/{dep_id}")).json() == created

    r = await client.post("/api/admin/deployments", json=body(), headers=h)
    assert r.status_code == 409 and r.json()["detail"] == "Развёртывание с таким именем уже есть"

    r = await client.patch(f"/api/admin/deployments/{dep_id}", json={"ctx_per_request": 4096}, headers=h)
    assert r.status_code == 200 and r.json()["ctx_per_request"] == 4096
    assert r.json()["spec_hash"] != created["spec_hash"]
    r = await client.patch(f"/api/admin/deployments/{dep_id}", json={"ctx_per_request": 99999}, headers=h)
    assert r.status_code == 422 and "контекст" in r.json()["detail"].lower()
    assert (await client.get(f"/api/admin/deployments/{dep_id}")).json()["ctx_per_request"] == 4096
    r = await client.patch(f"/api/admin/deployments/{dep_id}", json={"nodes": ["n2"], "enabled": False}, headers=h)
    assert r.json()["assigned_nodes"] == ["n2"] and r.json()["enabled"] is False

    r = await client.put("/api/admin/nodes/n1/assignment", json={"deployment_id": dep_id}, headers=h)
    assert r.status_code == 200
    got = (await client.get(f"/api/admin/deployments/{dep_id}")).json()
    assert got["assigned_nodes"] == ["n1", "n2"]
    r = await client.put("/api/admin/nodes/n1/assignment", json={"deployment_id": None}, headers=h)
    assert r.status_code == 200
    assert (await client.get(f"/api/admin/deployments/{dep_id}")).json()["assigned_nodes"] == ["n2"]
    r = await client.put("/api/admin/nodes/n1/assignment", json={"deployment_id": 999}, headers=h)
    assert r.status_code == 404

    assert (await client.delete(f"/api/admin/deployments/{dep_id}", headers=h)).status_code == 204
    assert (await client.get(f"/api/admin/deployments/{dep_id}")).status_code == 404
    assert (await client.delete(f"/api/admin/deployments/{dep_id}", headers=h)).status_code == 404
    assert (await client.patch(f"/api/admin/deployments/{dep_id}", json={}, headers=h)).status_code == 404
    assert (await client.get("/api/admin/deployments")).json() == []


async def test_validation_errors_are_russian_422(client, app):
    h = await as_admin(client, app)
    r = await client.post("/api/admin/deployments", json=body(engine="vllm", accel="cpu", file=None, repo="org/vllm"), headers=h)
    assert r.status_code == 422 and r.json()["detail"]
    r = await client.post("/api/admin/deployments", json=body(file="nope.gguf"), headers=h)
    assert r.status_code == 422
    r = await client.post("/api/admin/deployments", json=body(repo="org/absent"), headers=h)
    assert r.status_code == 404 and r.json()["detail"] == "Репозиторий или файл не найден"
    app.state.hf.unavailable = True
    r = await client.post("/api/admin/deployments", json=body(), headers=h)
    assert r.status_code == 502 and r.json()["detail"] == "Hugging Face недоступен"


async def test_standalone_node_assignment_rejected_for_split(client, app):
    h = await as_admin(client, app)
    async with app.state.sessionmaker() as db:
        db.add(StandaloneNode(name="s1", url="http://10.0.0.5:18080", accel="cpu", version="t"))
        await db.commit()
    r = await client.post("/api/admin/deployments", json=body(mode="split", accel="cpu", group_size=2), headers=h)
    r = await client.put("/api/admin/nodes/s1/assignment", json={"deployment_id": r.json()["id"]}, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "Standalone-нода поддерживает только replicas на llama.cpp"


async def test_offline_seed_marks_meta_missing_in_api(app, client):
    hf = FakeHf()
    hf.unavailable = True
    async with app.state.sessionmaker() as db:
        await service.seed_default(db, app.state.settings, hf)
    h = await as_admin(client, app)
    (item,) = (await client.get("/api/admin/deployments")).json()
    assert item["meta_missing"] is True and item["meta"] is None
    r = await client.patch(f"/api/admin/deployments/{item['id']}", json={"ctx_per_request": 100000}, headers=h)
    assert r.status_code == 200 and r.json()["meta_missing"] is True


async def test_deployment_status_endpoint(client, app):
    h = await as_admin(client, app)
    dep = (await client.post("/api/admin/deployments", json=body(nodes=["k1"]), headers=h)).json()
    app.state.k8s.add_pod("p1", "k1", "inference", "m1", ready=False, phase="Pending")
    app.state.k8s.logs["p1"] = "boom"
    r = await client.get(f"/api/admin/deployments/{dep['id']}/status")
    assert r.status_code == 200
    data = r.json()
    assert data["spec_hash"] == dep["spec_hash"] and data["ready_backends"] == 0
    assert data["nodes"][0]["pods"][0]["last_log"] == "boom"
    assert (await client.get("/api/admin/deployments/999/status")).status_code == 404


# ---------- catalog ----------


async def test_models_catalog_statuses(client, app):
    h = await as_admin(client, app)
    for name, nodes in (("ready-m", ["k1"]), ("starting-m", ["k2"]), ("down-m", [])):
        r = await client.post("/api/admin/deployments", json=body(name=name, nodes=nodes), headers=h)
        assert r.status_code == 201, r.text
    await client.post("/api/admin/deployments", json=body(name="hidden", enabled=False, nodes=["k3"]), headers=h)
    await app.state.litellm.add_model("pm-1", "ready-m", "http://k1:18080/v1", "k8s", "k1", 8192)

    r = await client.get("/api/models")
    assert r.status_code == 200
    by_name = {m["name"]: m for m in r.json()}
    assert set(by_name) == {"ready-m", "starting-m", "down-m"}  # disabled ones are hidden
    assert by_name["ready-m"] == {
        "name": "ready-m", "engine": "llamacpp", "mode": "replicas", "group_size": 2,
        "ctx_per_request": 8192, "parallel": 2, "ready_backends": 1, "status": "ready",
        "experimental": False, "vision": False,
    }
    assert (by_name["starting-m"]["status"], by_name["starting-m"]["ready_backends"]) == ("starting", 0)
    assert (by_name["down-m"]["status"], by_name["down-m"]["ready_backends"]) == ("down", 0)

    app.state.litellm.unavailable = True
    by_name = {m["name"]: m for m in (await client.get("/api/models")).json()}
    assert {m["status"] for m in by_name.values()} == {"unknown"}
    assert {m["ready_backends"] for m in by_name.values()} == {None}


async def test_models_catalog_needs_login_but_not_admin(client, app):
    assert (await client.get("/api/models")).status_code == 401
    await login(client, app, USER)
    assert (await client.get("/api/models")).status_code == 200


async def test_public_status_no_login(client, app):
    h = await as_admin(client, app)
    await client.post("/api/admin/deployments", json=body(nodes=["k1"]), headers=h)
    await app.state.litellm.add_model("pm-1", "m1", "http://k1:18080/v1", "k8s", "k1", 8192)
    client.cookies.clear()
    r = await client.get("/api/public/status")
    assert r.status_code == 200
    assert r.json() == {"models": [{"name": "m1", "status": "ready", "ready_backends": 1}]}
    app.state.litellm.unavailable = True
    assert (await client.get("/api/public/status")).json() == {
        "models": [{"name": "m1", "status": "unknown", "ready_backends": None}]
    }


# ---------- Hugging Face inspector ----------


async def test_hf_files_and_estimate_endpoint(client, app):
    await as_admin(client, app)
    r = await client.get("/api/admin/hf/files", params={"repo": REPO})
    assert r.status_code == 200
    assert r.json() == [
        {"file": FILE, "size_bytes": 4 * GiB, "quant": "Q4_K_M", "shards": [FILE]},
        {"file": "model-Q8_0.gguf", "size_bytes": 8 * GiB, "quant": "Q8_0", "shards": ["model-Q8_0.gguf"]},
    ]
    r = await client.get("/api/admin/hf/files", params={"repo": "org/vllm", "engine": "vllm"})
    assert r.status_code == 200 and r.json()["size_bytes"] == 10 * GiB and r.json()["meta"]["max_ctx"] == 32768

    k8s = app.state.k8s
    k8s.add_node("g1", accel="nvidia", vram_mib=16 * 1024)  # capacity 16 GiB - 1.5 GiB = 14.5 GiB
    k8s.add_node("g2", accel="nvidia", vram_mib=16 * 1024)
    async with app.state.sessionmaker() as db:
        db.add(StandaloneNode(name="s1", url="http://10.0.0.5:18080", accel="cpu", ram_mib=24 * 1024, version="t"))
        await db.commit()
    params = {"repo": BIG_REPO, "file": BIG_FILE, "ctx_per_request": 8192, "parallel": 2, "group_size": 2,
              "nodes": "g1,g2,s1,ghost"}
    r = await client.get("/api/admin/hf/estimate", params=params)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["meta"]["n_layers"] == 60 and data["meta"]["max_ctx"] == 131072
    est = data["estimate"]
    assert est["weights_bytes"] == int(18.7 * GiB)
    assert est["kv_bytes"] == 2 * 60 * 16 * 128 * 16384 * 2
    assert est["total_bytes"] == est["weights_bytes"] + est["kv_bytes"]
    per = {n["name"]: n for n in data["nodes"]}
    assert list(per) == ["g1", "g2", "s1", "ghost"]
    assert per["g1"]["capacity_bytes"] == (16 * 1024 - 1536) * 2**20
    # total ~26.2 GiB: half plus overhead is ~13.6 GiB, which fits 14.5 GiB; s1 has 18.2 GiB of usable RAM
    assert per["g1"]["fits"] is True and per["g2"]["fits"] is True and per["s1"]["fits"] is True
    assert per["s1"]["capacity_bytes"] == (24 * 1024 * 2**20 * 8 // 10) - GiB
    assert per["ghost"] == {"name": "ghost", "capacity_bytes": None, "need_bytes": est["per_node_bytes"], "fits": False}
    assert data["recommended_group_size"] == 2
    assert data["ctx_ok"] is True

    r = await client.get("/api/admin/hf/estimate", params={**params, "ctx_per_request": 200000})
    assert r.json()["ctx_ok"] is False
    r = await client.get("/api/admin/hf/estimate", params={**params, "group_size": 1, "nodes": "g1"})
    assert r.json()["nodes"][0]["fits"] is False and r.json()["recommended_group_size"] is None
    r = await client.get("/api/admin/hf/estimate", params={"repo": "org/absent", "file": "x.gguf"})
    assert r.status_code == 404 and r.json()["detail"] == "Репозиторий или файл не найден"


@pytest.mark.parametrize("path", ["files", "estimate"])
@pytest.mark.parametrize("repo", ["no-slash", "../etc/passwd", "org/", "a/b/c", "org/mo del", "org/m?x=1"])
async def test_hf_endpoints_reject_a_malformed_repo(client, app, path, repo):
    await as_admin(client, app)
    app.state.hf.calls.clear()
    r = await client.get(f"/api/admin/hf/{path}", params={"repo": repo, "file": FILE})
    assert r.status_code == 422 and r.json()["detail"] == "Репозиторий указывается как владелец/название"
    assert app.state.hf.calls == []  # nothing was sent to Hugging Face


@pytest.mark.parametrize("path", ["files", "estimate"])
async def test_hf_endpoints_reject_an_unknown_engine(client, app, path):
    await as_admin(client, app)
    r = await client.get(f"/api/admin/hf/{path}", params={"repo": REPO, "file": FILE, "engine": "tgi"})
    assert r.status_code == 422 and r.json()["detail"] == "Движок: llamacpp или vllm"


async def test_hf_unavailable_502(client, app):
    await as_admin(client, app)
    app.state.hf.unavailable = True
    for url, params in [
        ("/api/admin/hf/files", {"repo": REPO}),
        ("/api/admin/hf/files", {"repo": REPO, "engine": "vllm"}),
        ("/api/admin/hf/estimate", {"repo": REPO, "file": FILE}),
    ]:
        r = await client.get(url, params=params)
        assert r.status_code == 502 and r.json()["detail"] == "Hugging Face недоступен", url
    assert (await client.get("/api/admin/hf/files", params={"repo": "org/absent"})).status_code == 502
    app.state.hf.unavailable = False
    r = await client.get("/api/admin/hf/files", params={"repo": "org/absent"})
    assert r.status_code == 404 and r.json()["detail"] == "Репозиторий или файл не найден"


async def test_operation_fields_through_the_api(client, app):
    h = await as_admin(client, app)
    schedule = {"days": [1, 2, 3, 4, 5], "start": "08:00", "end": "20:00"}
    r = await client.post(
        "/api/admin/deployments",
        json=body(mode="split", accel="cpu", spare_nodes=1, schedule=schedule),
        headers=h,
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert (created["spare_nodes"], created["regroup_grace_s"], created["schedule"]) == (1, None, schedule)
    assert created["regroup_grace_effective_s"] == 600  # the split default
    assert isinstance(created["schedule_active"], bool)
    assert created["schedule_drain_s"] == 60  # SCHEDULE_DRAIN_S, shown in the hint of the schedule

    dep_id = created["id"]
    r = await client.patch(f"/api/admin/deployments/{dep_id}", json={"regroup_grace_s": 30, "schedule": None}, headers=h)
    assert r.status_code == 200, r.text
    got = r.json()
    assert (got["regroup_grace_s"], got["regroup_grace_effective_s"], got["schedule"]) == (30, 30, None)
    assert got["schedule_active"] is True and got["spare_nodes"] == 1

    r = await client.patch(
        f"/api/admin/deployments/{dep_id}", json={"schedule": {"days": [1], "start": "08:00", "end": "08:00"}}, headers=h
    )
    assert r.status_code == 422 and r.json()["detail"] == "Начало и конец окна расписания совпадают"


async def test_a_gpu_node_fits_a_replica_by_what_its_vram_holds(client, app):
    """llama.cpp keeps the input embeddings in host RAM: a GPU node fits a model whose layers, output and KV fit its
    VRAM, even when the file with the embeddings is larger; a CPU node still needs everything."""
    from app.catalog.gguf import GgufLayout

    await as_admin(client, app)
    # 18.7 GiB file, of it 10 GiB of input embeddings (Gemma 3n/4 per-layer embeddings): about 8.7 GiB on the GPU
    app.state.hf.layouts[(BIG_REPO, BIG_FILE)] = GgufLayout([int(0.14 * GiB)] * 60, int(0.3 * GiB), 10 * GiB, GiB, 262144)
    k8s = app.state.k8s
    k8s.add_node("g1", accel="nvidia", vram_mib=16 * 1024)  # 14.5 GiB usable
    k8s.add_node("c1", accel="cpu", mem_mib=24 * 1024)  # 18.2 GiB usable
    params = {"repo": BIG_REPO, "file": BIG_FILE, "ctx_per_request": 2048, "parallel": 1, "nodes": "g1,c1"}
    r = await client.get("/api/admin/hf/estimate", params=params)
    assert r.status_code == 200, r.text
    e = r.json()["estimate"]
    assert e["device_bytes"] == int(0.14 * GiB) * 60 + int(0.3 * GiB) + e["kv_bytes"] + 512 * 2**20
    assert e["host_bytes"] == e["weights_bytes"] - int(0.14 * GiB) * 60 - int(0.3 * GiB)
    fits = {n["name"]: (n["fits"], n["need_bytes"]) for n in r.json()["nodes"]}
    assert fits == {"g1": (True, e["device_bytes"]), "c1": (False, e["per_node_bytes"])}  # per node 18.7 GiB + KV + overhead > 18.2 GiB of RAM
    app.state.hf.layouts.clear()  # no layout: the whole file is counted on the GPU too
    r = await client.get("/api/admin/hf/estimate", params=params)
    assert r.json()["estimate"]["device_bytes"] is None
    assert {n["name"]: n["fits"] for n in r.json()["nodes"]} == {"g1": False, "c1": False}


async def test_split_estimate_places_layers_over_vram_and_ram_of_the_first_group(client, app):
    from app.catalog.gguf import GgufLayout

    await as_admin(client, app)
    app.state.hf.layouts[(BIG_REPO, BIG_FILE)] = GgufLayout([int(0.3 * GiB)] * 60, GiB // 2, GiB // 2, GiB // 4, 262144)
    k8s = app.state.k8s
    for n in ("g2", "g1", "g3"):
        k8s.add_node(n, accel="nvidia", vram_mib=12282, mem_mib=32000, ram_mib=28000)
    params = {"repo": BIG_REPO, "file": BIG_FILE, "ctx_per_request": 8192, "parallel": 2, "group_size": 2,
              "nodes": "g1,g2,g3", "mode": "split"}
    r = await client.get("/api/admin/hf/estimate", params=params)
    assert r.status_code == 200, r.text
    data = r.json()
    vram = (12282 - 1536) * 2**20
    ram = 28000 * 2**20 * 70 // 100
    vram -= 1024 * 2**20  # GPU_COMPUTE_RESERVE_MIB
    assert data["nodes"][0]["capacity_bytes"] == vram + ram  # split: VRAM plus the RAM budget
    assert data["recommended_group_size"] == 2
    plan = data["placement"]
    assert [n["name"] for n in plan["nodes"]] == ["g1", "g2"] and plan["nodes"][0]["leader"] is True
    assert plan["fits"] is True and plan["ram_bytes"] > 0 and plan["speed_warning"] is True
    g1, g2 = plan["nodes"]
    assert g1["vram_budget_bytes"] == vram and g2["ram_budget_bytes"] == 0  # the last CPU device takes nothing
    assert g1["ram_bytes"] == plan["ram_bytes"] and g2["cpu_layers"] == 0
    assert sum(plan["tensor_split"]) == 61 and len(plan["tensor_split"]) == 4

    r = await client.get("/api/admin/hf/estimate", params={**params, "use_node_ram": "false"})
    plan = r.json()["placement"]
    assert plan["fits"] is False and len(plan["tensor_split"]) == 2 and plan["speed_warning"] is False
    assert r.json()["nodes"][0]["capacity_bytes"] == vram
    # the recommendation is checked by the same placement: VRAM only, three cards are needed
    rec = r.json()["recommended_group_size"]
    assert rec == 3
    r = await client.get("/api/admin/hf/estimate", params={**params, "use_node_ram": "false", "group_size": rec})
    assert r.json()["placement"]["fits"] is True
    r = await client.get("/api/admin/hf/estimate", params={**params, "ram_budget_pct": 95})
    assert r.status_code == 422
    r = await client.get("/api/admin/hf/estimate", params={k: v for k, v in params.items() if k != "mode"})
    assert r.json()["placement"] is None  # replicas: no placement


async def test_deployment_view_shows_the_node_ram_settings(client, app):
    h = await as_admin(client, app)
    r = await client.post(
        "/api/admin/deployments", json=body(mode="split", accel="nvidia", use_node_ram=False, ram_budget_pct=60), headers=h
    )
    assert r.status_code == 201, r.text
    assert (r.json()["use_node_ram"], r.json()["ram_budget_pct"]) == (False, 60)
    r = await client.patch(f"/api/admin/deployments/{r.json()['id']}", json={"ram_budget_pct": 5}, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "Доля RAM ноды для модели от 10 до 90 %"


async def test_a_broken_stored_schedule_does_not_break_the_pages(client, app):
    from sqlalchemy import update

    from app.models import ModelDeployment

    h = await as_admin(client, app)
    r = await client.post("/api/admin/deployments", json=body(), headers=h)
    dep_id = r.json()["id"]
    async with app.state.sessionmaker() as db:  # written past the API
        await db.execute(update(ModelDeployment).where(ModelDeployment.id == dep_id).values(schedule={"days": [1]}))
        await db.commit()
    r = await client.get(f"/api/admin/deployments/{dep_id}", headers=h)
    assert r.status_code == 200 and r.json()["schedule_active"] is True  # runs around the clock
    assert (await client.get("/api/admin/deployments", headers=h)).status_code == 200
    assert (await client.get("/api/models", headers=h)).status_code == 200


@pytest.mark.parametrize(
    "schedule",
    [
        {"days": [1], "start": "08:00"},
        {"days": "1", "start": "08:00", "end": "20:00"},
        {"days": [1], "start": "08:00", "end": "25:00"},
        "08:00-20:00",
    ],
)
async def test_an_invalid_schedule_is_refused_on_write(client, app, schedule):
    h = await as_admin(client, app)
    r = await client.post("/api/admin/deployments", json=body(schedule=schedule), headers=h)
    assert r.status_code == 422
    dep_id = (await client.post("/api/admin/deployments", json=body(), headers=h)).json()["id"]
    r = await client.patch(f"/api/admin/deployments/{dep_id}", json={"schedule": schedule}, headers=h)
    assert r.status_code == 422
