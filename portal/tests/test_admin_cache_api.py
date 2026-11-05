"""«Кэш моделей» (task 2-29): the portal's side of the mirror's cache and prefetch API."""

import json

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.api.admin_cache import Usage, deployment_downloads, shard_set
from app.catalog.hf import HfFile
from app.catalog.mirror_admin import MirrorAdmin, MirrorRefused, MirrorUnavailable
from app.models import AuditLog, ModelDeployment, NodeAssignment, PortalFlag
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.fakes.mirror_admin import FakeMirrorAdmin, cached_file
from tests.helpers.auth import csrf_headers, login

GiB = 2**30
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru"}
PIN = "a" * 40
DRAFT_PIN = "b" * 40
SHARDS = [f"big-Q4_K_M-{i:05d}-of-00003.gguf" for i in (1, 2, 3)]


def dep(**kw) -> ModelDeployment:
    return ModelDeployment(
        **{"name": "m1", "repo": "org/model-GGUF", "file": "model-Q4_K_M.gguf", "ctx_per_request": 8192, "parallel": 2,
           "auto_assign": False, **kw}
    )


# ---- the pure part: which files a deployment downloads -------------------------------------------------------------


def test_shard_set_names_every_part_of_a_multi_file_gguf():
    assert shard_set("model-Q4_K_M.gguf") == ["model-Q4_K_M.gguf"]
    assert shard_set("q4/big-00002-of-00003.gguf") == [f"q4/big-{i:05d}-of-00003.gguf" for i in (1, 2, 3)]


def test_a_deployment_downloads_its_model_projector_and_draft_at_their_pinned_commits():
    d = dep(
        repo="org/big", file=SHARDS[0], revision=PIN, mmproj_file="mmproj-F16.gguf", draft_repo="org/draft",
        draft_file="draft-Q8_0.gguf", draft_revision=DRAFT_PIN,
    )
    assert deployment_downloads(d) == [
        ("org/big", PIN, [*SHARDS, "mmproj-F16.gguf"]),
        ("org/draft", DRAFT_PIN, ["draft-Q8_0.gguf"]),
    ]
    assert deployment_downloads(dep()) == [("org/model-GGUF", "main", ["model-Q4_K_M.gguf"])]  # not pinned: main


def test_own_files_are_not_downloaded():
    assert deployment_downloads(dep(repo="local/mine", file="mine.gguf")) == []
    assert deployment_downloads(dep(mmproj_file="local/mine/mmproj.gguf", draft_repo="local/d", draft_file="d.gguf")) == [
        ("org/model-GGUF", "main", ["model-Q4_K_M.gguf"])
    ]
    assert deployment_downloads(dep(engine="vllm", repo="local/x", file=None)) == []


def test_usage_tells_which_deployments_use_a_file_or_a_repository():
    a = dep(id=1, name="a", enabled=True, repo="org/big", file=SHARDS[0], mmproj_file="mmproj-F16.gguf")
    b = dep(id=2, name="b", enabled=False, repo="org/other", file="o.gguf", draft_repo="org/big", draft_file="draft.gguf")
    v = dep(id=3, name="v", enabled=True, engine="vllm", repo="org/vllm", file=None)
    u = Usage([a, b, v])
    assert [w["name"] for w in u.of_file("org/big", SHARDS[2])] == ["a"]  # every part counts, not only the first
    assert [w["name"] for w in u.of_file("org/big", "mmproj-F16.gguf")] == ["a"]
    assert [w["name"] for w in u.of_file("org/big", "draft.gguf")] == ["b"]  # a draft model of another deployment
    assert u.of_file("org/big", "unrelated.gguf") == []
    assert u.of_file("org/vllm", "model.safetensors") == [{"id": 3, "name": "v", "enabled": True}]  # a whole repository
    assert sorted(w["name"] for w in u.of_repo("org/big")) == ["a", "b"]
    assert u.of_repo("org/nothing") == []
    assert u.of_file("org/other", "o.gguf") == [{"id": 2, "name": "b", "enabled": False}]  # switched off: still its weights


# ---- the client of the mirror ------------------------------------------------------------------------------------------


def mirror_over(handler) -> MirrorAdmin:
    return MirrorAdmin("http://127.0.0.1:8090/", "hf_key", transport=httpx.MockTransport(handler))


async def test_the_client_calls_the_admin_paths_of_the_mirror_with_the_key():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode(), request.headers.get("authorization"), request.content))
        if request.method == "POST":
            return httpx.Response(202, json={"jobs": [{"id": "x"}]})
        return httpx.Response(200, json={"ok": True})

    m = mirror_over(handler)
    await m.cache()
    await m.delete("org/m")
    await m.delete("org/m", "dir/with space/f.gguf")
    assert await m.prefetch_add("org/m", PIN, ["a.gguf"]) == [{"id": "x"}]
    await m.prefetch_add("org/m", None, ["a.gguf"])
    await m.prefetch_jobs()
    await m.prefetch_cancel("abc123")
    await m.prefetch_cancel(None)
    assert [(method, path) for method, path, _, _ in seen] == [
        ("GET", "/admin/cache"), ("DELETE", "/admin/cache/org/m"), ("DELETE", "/admin/cache/org/m/dir/with%20space/f.gguf"),
        ("POST", "/admin/prefetch"), ("POST", "/admin/prefetch"), ("GET", "/admin/prefetch"),
        ("DELETE", "/admin/prefetch/abc123"), ("DELETE", "/admin/prefetch"),
    ]
    assert {auth for _, _, auth, _ in seen} == {"Bearer hf_key"}
    assert json.loads(seen[3][3]) == {"repo": "org/m", "files": ["a.gguf"], "revision": PIN}
    assert json.loads(seen[4][3]) == {"repo": "org/m", "files": ["a.gguf"]}  # no revision: the mirror takes main


async def test_the_client_tells_a_refusal_from_a_mirror_that_is_down():
    m = mirror_over(lambda request: httpx.Response(409, json={"error": "busy"}))
    with pytest.raises(MirrorRefused) as refused:
        await m.delete("org/m")
    assert (refused.value.status, refused.value.detail) == (409, "busy")

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(MirrorUnavailable):
        await mirror_over(down).cache()
    with pytest.raises(MirrorUnavailable):
        await MirrorAdmin(None, "k").cache()  # HF_MIRROR_ENABLED=false
    with pytest.raises(MirrorRefused) as junk:
        await mirror_over(lambda request: httpx.Response(200, text="<html>")).cache()
    assert junk.value.status == 502


# ---- the API ---------------------------------------------------------------------------------------------------------------

@pytest_asyncio.fixture
async def env(app):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(NodeAssignment))
        await db.execute(delete(ModelDeployment))
        await db.execute(delete(PortalFlag))
        await db.commit()
    app.state.hf = FakeHf(files={"org/vllm": [HfFile("model.safetensors", GiB), HfFile("config.json", 5),
                                              HfFile(".gitattributes", 1), HfFile("onnx/.cache/x", 1)]})
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()
    mirror = app.state.mirror_admin = FakeMirrorAdmin([
        cached_file("org/big", SHARDS[0], 40 * GiB),
        cached_file("org/big", SHARDS[1], 40 * GiB),
        cached_file("org/free", "free-Q4_K_M.gguf", 5 * GiB),
        cached_file("org/free", "partial.gguf", GiB, partial=True, downloading=True),
        cached_file("org/busy", "busy.gguf", 3 * GiB, busy=True),
    ])
    return mirror


async def as_admin(client, app) -> dict:
    return csrf_headers(await login(client, app, ADMIN))


async def add_deployment(app, **kw) -> int:
    async with app.state.sessionmaker() as db:
        row = dep(**kw)
        db.add(row)
        await db.commit()
        return row.id


@pytest.mark.docker
async def test_everything_is_for_admins_and_the_changes_need_csrf(client, app, env):
    assert (await client.get("/api/admin/cache")).status_code == 401
    h = csrf_headers(await login(client, app, USER))
    for method, url in [("GET", "/api/admin/cache"), ("DELETE", "/api/admin/cache/org/free"), ("GET", "/api/admin/prefetch"),
                        ("POST", "/api/admin/prefetch"), ("DELETE", "/api/admin/prefetch/abc"), ("DELETE", "/api/admin/prefetch"),
                        ("POST", "/api/admin/deployments/1/prefetch")]:
        r = await client.request(method, url, headers=h, json={} if method == "POST" else None)
        assert r.status_code == 403, (method, url)
    await login(client, app, ADMIN)  # a session without the CSRF header
    body = {"repo": "org/free", "files": ["f.gguf"]}
    for method, url, json in [("DELETE", "/api/admin/cache/org/free", None), ("POST", "/api/admin/prefetch", body),
                              ("DELETE", "/api/admin/prefetch/abc", None), ("DELETE", "/api/admin/prefetch", None),
                              ("POST", "/api/admin/deployments/1/prefetch", {})]:
        assert (await client.request(method, url, json=json)).status_code == 403, (method, url)
    assert [c[0] for c in env.calls] == []


@pytest.mark.docker
async def test_the_cache_lists_the_mirrors_files_with_the_deployments_that_use_them(client, app, env):
    h = await as_admin(client, app)
    await add_deployment(app, name="big", repo="org/big", file=SHARDS[0], revision=PIN)
    r = await client.get("/api/admin/cache", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["limit_bytes"] == 150 * GiB and data["used_bytes"] == 89 * GiB
    assert data["disk_free_bytes"] == 600 * GiB and data["disk_total_bytes"] == 1000 * GiB
    files = {(f["repo"], f["path"]): f for f in data["files"]}
    assert [w["name"] for w in files[("org/big", SHARDS[0])]["used_by"]] == ["big"]
    assert [w["name"] for w in files[("org/big", SHARDS[1])]["used_by"]] == ["big"]  # a part of the same model
    assert files[("org/free", "free-Q4_K_M.gguf")]["used_by"] == []
    assert files[("org/free", "partial.gguf")]["downloading"] is True and files[("org/busy", "busy.gguf")]["busy"] is True
    assert files[("org/big", SHARDS[0])]["revision"] == "c" * 40 and files[("org/big", SHARDS[0])]["last_served"]


@pytest.mark.docker
async def test_a_mirror_that_is_down_is_a_503_with_a_reason(client, app, env):
    h = await as_admin(client, app)
    env.unavailable = True
    for method, url in [("GET", "/api/admin/cache"), ("GET", "/api/admin/prefetch"), ("DELETE", "/api/admin/cache/org/free")]:
        r = await client.request(method, url, headers=h)
        assert r.status_code == 503 and "Зеркало" in r.json()["detail"], (method, url)
    # without a fake: the default client goes to 127.0.0.1:<hf_mirror_port> (9 in the tests), where nothing listens
    del app.state.mirror_admin
    assert (await client.get("/api/admin/cache", headers=h)).status_code == 503


@pytest.mark.docker
async def test_a_mirror_switched_off_has_no_cache(client, app, env):
    h = await as_admin(client, app)
    del app.state.mirror_admin
    app.state.settings.hf_mirror_enabled = False
    r = await client.get("/api/admin/cache", headers=h)
    assert r.status_code == 503 and "HF_MIRROR_ENABLED" in r.json()["detail"]


@pytest.mark.docker
async def test_a_file_or_a_repository_is_deleted_through_the_mirror(client, app, env):
    h = await as_admin(client, app)
    r = await client.delete("/api/admin/cache/org/free/free-Q4_K_M.gguf", headers=h)
    assert r.status_code == 200 and r.json()["freed_bytes"] == 5 * GiB
    assert ("delete", "org/free", "free-Q4_K_M.gguf") in env.calls
    r = await client.delete("/api/admin/cache/org/free", headers=h)  # what is left of it is being downloaded
    assert r.status_code == 409
    assert (await client.delete("/api/admin/cache/org/free/nothing.gguf", headers=h)).status_code == 404
    assert (await client.delete("/api/admin/cache/org", headers=h)).status_code == 422
    assert (await client.delete("/api/admin/cache/not%20a/repo", headers=h)).status_code == 422
    assert (await client.delete("/api/admin/cache/org/busy", headers=h)).status_code == 409  # being served: the mirror says no


@pytest.mark.docker
async def test_a_file_a_deployment_uses_is_not_deleted_and_the_mirror_is_not_asked(client, app, env):
    h = await as_admin(client, app)
    await add_deployment(app, name="big", repo="org/big", file=SHARDS[0], revision=PIN, enabled=False)
    for url in (f"/api/admin/cache/org/big/{SHARDS[1]}", "/api/admin/cache/org/big"):
        r = await client.delete(url, headers=h)
        assert r.status_code == 409 and "big" in r.json()["detail"], url
    assert [c for c in env.calls if c[0] == "delete"] == []
    assert len(env.files) == 5
    # the deployment goes, the file can
    async with app.state.sessionmaker() as db:
        await db.execute(delete(ModelDeployment))
        await db.commit()
    assert (await client.delete("/api/admin/cache/org/big", headers=h)).status_code == 200
    assert [f["repo"] for f in env.files].count("org/big") == 0


@pytest.mark.docker
async def test_prefetch_goes_to_the_mirror_and_is_checked_first(client, app, env):
    h = await as_admin(client, app)
    r = await client.post("/api/admin/prefetch", headers=h, json={"repo": "org/new", "revision": PIN, "files": ["a.gguf", "b/c.gguf"]})
    assert r.status_code == 202, r.text
    assert [j["path"] for j in r.json()["jobs"]] == ["a.gguf", "b/c.gguf"] and r.json()["jobs"][0]["revision"] == PIN
    assert ("prefetch_add", "org/new", PIN, ("a.gguf", "b/c.gguf")) in env.calls
    for bad in ({"repo": "nonsense", "files": ["a"]}, {"repo": "local/mine", "files": ["a"]}, {"repo": "org/new", "files": []},
                {"repo": "org/new", "files": ["a"] * 201}, {"files": ["a"]}, {"repo": "org/new"}):
        r = await client.post("/api/admin/prefetch", headers=h, json=bad)
        assert r.status_code == 422, bad
    assert len([c for c in env.calls if c[0] == "prefetch_add"]) == 1
    env.next_error = MirrorRefused(400, "invalid file path: ../x")  # whatever the mirror finds wrong is the admin's input
    r = await client.post("/api/admin/prefetch", headers=h, json={"repo": "org/new", "files": ["../x"]})
    assert r.status_code == 422 and "invalid file path" in r.json()["detail"]
    env.next_error = MirrorRefused(500, "boom")
    assert (await client.post("/api/admin/prefetch", headers=h, json={"repo": "org/new", "files": ["x"]})).status_code == 502


@pytest.mark.docker
async def test_the_jobs_are_listed_cancelled_and_cleared(client, app, env):
    h = await as_admin(client, app)
    first = (await client.post("/api/admin/prefetch", headers=h, json={"repo": "org/new", "files": ["a.gguf", "b.gguf"]})).json()["jobs"]
    listing = (await client.get("/api/admin/prefetch", headers=h)).json()
    assert [j["path"] for j in listing["jobs"]] == ["a.gguf", "b.gguf"] and listing["concurrency"] == 2
    r = await client.delete(f"/api/admin/prefetch/{first[0]['id']}", headers=h)
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert (await client.delete("/api/admin/prefetch/unknown", headers=h)).status_code == 404
    r = await client.delete("/api/admin/prefetch", headers=h)  # the finished ones leave the list
    assert r.status_code == 200
    assert [j["path"] for j in (await client.get("/api/admin/prefetch", headers=h)).json()["jobs"]] == ["b.gguf"]


@pytest.mark.docker
async def test_a_deployments_weights_are_queued_at_its_pinned_commits(client, app, env):
    h = await as_admin(client, app)
    dep_id = await add_deployment(
        app, name="big", repo="org/big", file=SHARDS[0], revision=PIN, mmproj_file="mmproj-F16.gguf",
        draft_repo="org/draft", draft_file="draft-Q8_0.gguf", draft_revision=DRAFT_PIN,
    )
    r = await client.post(f"/api/admin/deployments/{dep_id}/prefetch", headers=h)
    assert r.status_code == 202, r.text
    assert [(j["repo"], j["revision"], j["path"]) for j in r.json()["jobs"]] == [
        *(("org/big", PIN, p) for p in [*SHARDS, "mmproj-F16.gguf"]), ("org/draft", DRAFT_PIN, "draft-Q8_0.gguf"),
    ]
    assert [c[1:3] for c in env.calls if c[0] == "prefetch_add"] == [("org/big", PIN), ("org/draft", DRAFT_PIN)]


@pytest.mark.docker
async def test_a_vllm_deployment_queues_the_visible_files_of_its_repository(client, app, env):
    h = await as_admin(client, app)
    dep_id = await add_deployment(app, name="v", engine="vllm", repo="org/vllm", file=None, revision=PIN)
    r = await client.post(f"/api/admin/deployments/{dep_id}/prefetch", headers=h)
    assert r.status_code == 202, r.text
    assert sorted(j["path"] for j in r.json()["jobs"]) == ["config.json", "model.safetensors"]  # no .gitattributes, no .cache


@pytest.mark.docker
async def test_a_deployment_that_has_nothing_to_download_is_refused(client, app, env):
    h = await as_admin(client, app)
    own = await add_deployment(app, name="own", repo="local/mine", file="mine.gguf")
    r = await client.post(f"/api/admin/deployments/{own}/prefetch", headers=h)
    assert r.status_code == 422 and "local" in r.json()["detail"]
    assert (await client.post("/api/admin/deployments/9999/prefetch", headers=h)).status_code == 404
    assert [c for c in env.calls if c[0] == "prefetch_add"] == []


async def audit_rows(app) -> list[AuditLog]:
    async with app.state.sessionmaker() as db:
        return list((await db.scalars(select(AuditLog).where(AuditLog.target_type == "cache").order_by(AuditLog.id))).all())


@pytest.mark.docker
async def test_every_change_of_the_cache_leaves_an_audit_row_only_when_the_mirror_did_it(client, app, env):
    h = await as_admin(client, app)
    async with app.state.sessionmaker() as db:
        await db.execute(delete(AuditLog))
        await db.commit()
    assert await audit_rows(app) == []
    # refused or failed: no row
    assert (await client.delete("/api/admin/cache/org/busy", headers=h)).status_code == 409
    assert (await client.delete("/api/admin/cache/org/free/nothing.gguf", headers=h)).status_code == 404
    assert (await client.post("/api/admin/prefetch", headers=h, json={"repo": "nonsense", "files": ["a"]})).status_code == 422
    assert await audit_rows(app) == []

    assert (await client.delete("/api/admin/cache/org/free/free-Q4_K_M.gguf", headers=h)).status_code == 200
    jobs = (await client.post("/api/admin/prefetch", headers=h, json={"repo": "org/new", "revision": PIN, "files": ["a.gguf"]})).json()["jobs"]
    assert (await client.delete(f"/api/admin/prefetch/{jobs[0]['id']}", headers=h)).status_code == 200
    assert (await client.delete("/api/admin/prefetch", headers=h)).status_code == 200
    dep_id = await add_deployment(app, name="big", repo="org/big", file=SHARDS[0], revision=PIN)
    assert (await client.post(f"/api/admin/deployments/{dep_id}/prefetch", headers=h)).status_code == 202

    rows = await audit_rows(app)
    assert [(r.action, r.target) for r in rows] == [
        ("cache.delete", "org/free/free-Q4_K_M.gguf"),
        ("cache.prefetch", "org/new"),
        ("cache.prefetch", "org/new/a.gguf"),
        ("cache.prefetch", "finished"),
        ("cache.prefetch", "big"),
    ]
    assert all(r.actor_email == ADMIN["email"] and r.ip for r in rows)
    assert rows[0].details == {"freed_bytes": 5 * GiB, "files": 1}
    assert rows[1].details == {"revision": PIN, "files": ["a.gguf"]}
    assert rows[2].details["action"] == "cancel" and rows[3].details["action"] == "clear"
    assert rows[4].details["deployment_id"] == dep_id and len(rows[4].details["files"]) == 3


@pytest.mark.docker
async def test_dot_segments_in_the_path_do_not_get_past_the_usage_check(client, app, env):
    h = await as_admin(client, app)
    await add_deployment(app, name="big", repo="org/big", file=SHARDS[0], revision=PIN)
    for tail in ("%2e%2e/%2e%2e/big/" + SHARDS[1], "%2e/x.gguf", "a//b.gguf", "a/%2e%2e"):  # encoded: the test client would normalize literal ones
        r = await client.delete(f"/api/admin/cache/org/free/{tail}", headers=h)
        assert r.status_code in (404, 422), (tail, r.status_code)
    assert [c for c in env.calls if c[0] == "delete"] == []
    assert len(env.files) == 5
