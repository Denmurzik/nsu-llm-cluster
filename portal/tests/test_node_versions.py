from dataclasses import replace
"""Versions and node updates (task 2-30): main's registry as the pods see it, what the nodes report, the update requests."""

import hashlib
from datetime import UTC, datetime, timedelta

import fakeredis
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app import settings_store
from app.config import Settings
from app.models import AuditLog, ModelDeployment, NodeAssignment, StandaloneNode
from app.nodes import versions
from app.nodes.versions import ImageInfo, Registry, RegistryError
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

S = Settings(node_image_tag="0.2.0")
D1 = "sha256:" + "11" * 32
D2 = "sha256:" + "22" * 32
D3 = "sha256:" + "33" * 32
CONFIG = "sha256:" + "aa" * 32


def manifest_of(config_digest=CONFIG):
    return {"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json", "config": {"digest": config_digest}}


def blob_of(version="0.1.0", revision="abc1234", created="2026-11-03T10:00:00Z"):
    return {
        "created": created,
        "config": {"Labels": {"org.opencontainers.image.version": version, "org.opencontainers.image.revision": revision}},
    }


class FakeRegistry:
    """Main's registry (CNCF distribution) as an httpx transport: tag -> digest, labels from the config blob."""

    def __init__(self):
        self.tags: dict[str, str] = {}
        self.labels: dict[str, tuple[str, str]] = {}
        self.calls: list[str] = []
        self.status = 200  # every answer; 503: the registry is down
        self.index = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.status != 200:
            return httpx.Response(self.status)
        head = "/v2/nsu-llm-node/"
        assert path.startswith(head), path
        rest = path[len(head):]
        if rest == "tags/list":
            return httpx.Response(200, json={"name": "nsu-llm-node", "tags": sorted(self.tags)})
        kind, _, ref = rest.partition("/")
        if kind == "manifests":
            if ref in self.tags:
                digest = self.tags[ref]
                if self.index:
                    body = {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
                        {"digest": "sha256:" + "ee" * 32, "platform": {"os": "linux", "architecture": "arm64"}},
                        {"digest": "sha256:" + "ff" * 32 if ref else "", "platform": {"os": "linux", "architecture": "amd64"}},
                    ]}
                    return httpx.Response(200, json=body, headers={"Docker-Content-Digest": digest})
                return httpx.Response(200, json=manifest_of(f"sha256:{ref_hash(ref)}"), headers={"Docker-Content-Digest": digest})
            if ref == "sha256:" + "ff" * 32:
                return httpx.Response(200, json=manifest_of(f"sha256:{ref_hash('0.2.0-gpu')}"))
            return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})
        if kind == "blobs":
            for tag in self.tags:
                if ref == f"sha256:{ref_hash(tag)}":
                    version, revision = self.labels.get(tag, ("0.1.0", "abc1234"))
                    return httpx.Response(200, json=blob_of(version, revision))
            return httpx.Response(404)
        raise AssertionError(path)


def ref_hash(tag):
    return hashlib.sha256(tag.encode()).hexdigest()


@pytest.fixture
def registry():
    """The process-wide Registry pointed at a fake registry, empty before and after."""
    fake = FakeRegistry()
    fake.tags = {"0.2.0-cpu": D1, "0.2.0-gpu": D2}
    versions.REGISTRY.reset()
    versions.REGISTRY.transport = httpx.MockTransport(fake)
    yield fake
    versions.REGISTRY.reset()
    versions.REGISTRY.transport = None


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)


# ---------------------------------------------------------------- the registry client


async def test_the_registry_gives_the_digest_and_the_labels_of_a_tag(registry):
    info = await versions.REGISTRY.fetch(S, "0.2.0-gpu")
    assert info == ImageInfo("0.2.0-gpu", D2, "0.1.0", "abc1234", "2026-11-03T10:00:00Z")


async def test_an_index_is_followed_to_the_amd64_image(registry):
    registry.index = True
    info = await versions.REGISTRY.fetch(S, "0.2.0-gpu")
    assert info.digest == D2  # the digest of the index: what a pull by tag resolves to
    assert (info.version, info.revision) == ("0.1.0", "abc1234")


async def test_a_tag_the_registry_does_not_have_is_none_and_a_broken_registry_an_error(registry):
    assert await versions.REGISTRY.fetch(S, "9.9.9-cpu") is None
    registry.status = 503
    with pytest.raises(RegistryError):
        await versions.REGISTRY.fetch(S, "0.2.0-cpu")


async def test_a_registry_that_is_down_keeps_the_digest_the_pods_were_built_with(registry, redis):
    reg = versions.REGISTRY
    assert (await reg.resolve(S, "0.2.0-cpu", redis)).digest == D1
    reg.reset()  # the process restarts: Redis remembers the digest
    registry.status = 503
    seeded = await reg.resolve(S, "0.2.0-cpu", redis)
    assert seeded is not None and seeded.digest == D1
    assert reg.digest(S, "0.2.0-cpu") == D1
    # and without Redis a young process knows nothing, which the controller reads as «wait»
    reg.reset()
    assert await reg.resolve(S, "0.2.0-cpu", None) is None
    assert reg.digest(S, "0.2.0-cpu") is None and not reg.known(S, "0.2.0-cpu")


async def test_a_registry_that_answers_404_says_there_is_no_pin(registry, redis):
    reg = versions.REGISTRY
    assert (await reg.resolve(S, "0.2.0-cpu", redis)).digest == D1
    del registry.tags["0.2.0-cpu"]
    reg._cache.clear()
    reg._tried.clear()
    assert await reg.resolve(S, "0.2.0-cpu", redis) is None
    assert reg.known(S, "0.2.0-cpu") and reg.digest(S, "0.2.0-cpu") is None  # known to be absent: the tag form
    assert await redis.get(f"{versions.KEY_PIN}nsu-llm-node:0.2.0-cpu") is None


async def test_the_registry_is_not_asked_again_within_a_minute(registry, redis):
    await versions.REGISTRY.resolve(S, "0.2.0-cpu", redis)
    await versions.REGISTRY.resolve(S, "0.2.0-cpu", redis)
    assert [c for c in registry.calls if "/manifests/0.2.0-cpu" in c] == ["/v2/nsu-llm-node/manifests/0.2.0-cpu"]


class State:
    def __init__(self, settings, redis=None):
        self.settings, self.redis = settings, redis


async def test_the_controller_waits_for_the_registry_only_while_the_process_is_young(registry, redis, monkeypatch):
    reg = versions.REGISTRY
    registry.status = 503
    state = State(S)
    assert await reg.refresh_pins(state) is False  # nothing known, first minutes: skip the round, build no pods
    assert await reg.refresh_pins(state) is False
    monkeypatch.setattr(versions, "PIN_WAIT_S", 0.0)
    assert await reg.refresh_pins(state) is True  # a main without a registry still gets its pods (the tag form)
    registry.status = 200
    reg.reset()
    assert await reg.refresh_pins(State(S, redis)) is True
    assert versions.pinned_digest(S, "cpu") == D1 and versions.pinned_digest(S, "nvidia") == D2
    assert versions.pinned_digest(Settings(node_image_pin=False), "cpu") is None


async def test_tags_of_the_repository(registry):
    assert await versions.REGISTRY.list_tags(S) == ["0.2.0-cpu", "0.2.0-gpu"]


# ---------------------------------------------------------------- who is outdated, who goes now


def report(digest=D1, **kw):
    return {"version": "0.1.0", "revision": "abc1234", "digest": digest, "tag": "nsu-llm-node:0.2.0-cpu", "update_failed_at": None, **kw}


OFFER = ImageInfo("0.2.0-cpu", D1, "0.1.0", "abc1234")


def test_a_node_is_outdated_by_digest_else_by_revision_else_by_version():
    assert versions.outdated(report(D1), OFFER) is False
    assert versions.outdated(report(D3), OFFER) is True
    assert versions.outdated(None, OFFER) is None and versions.outdated(report(D1), None) is None
    # an image from an archive carries no registry digest: the labels tell
    assert versions.outdated(report("", revision="abc1234"), OFFER) is False
    assert versions.outdated(report("", revision="def5678"), OFFER) is True
    assert versions.outdated(report("", revision="", version="0.0.9"), OFFER) is True
    assert versions.outdated(report("", revision="", version="0.1.0"), OFFER) is None  # same version, cannot tell
    assert versions.outdated(report("", revision="unknown", version="0.1.0"), OFFER) is None


def verdict(rep, request=None, offer=OFFER, serving=False, settings=S, now=None, auto=False):
    return versions.decide(settings, rep, request, offer, serving, auto, now)


def test_nothing_happens_without_a_request_unless_auto_update_is_on():
    assert verdict(report(D3)).go is False
    v = verdict(report(D3), auto=True)
    assert (v.go, v.mode, v.waiting) == (True, "auto", False)
    assert verdict(report(D3), auto=True, serving=True).go is False  # auto never replaces a serving node
    assert verdict(report(D1), auto=True).go is False  # nothing to do
    assert verdict(None, auto=True).go is False  # a node that never reported is not guessed at


def test_the_admin_chooses_now_or_after_the_deployment_is_off():
    now = {"when": "now"}
    idle = {"when": "idle"}
    assert verdict(report(D3), now, serving=True).go is True  # forced
    v = verdict(report(D3), idle, serving=True)
    assert (v.go, v.waiting, v.mode, v.when) == (False, True, "requested", "idle")
    assert verdict(report(D3), idle, serving=False).go is True
    assert verdict(report("", revision="", version="0.1.0"), idle).go is True  # the admin's word is enough when it cannot be told


def test_a_node_that_runs_the_offered_image_is_never_told_to_replace_it():
    v = verdict(report(D1), {"when": "now"})
    assert v.go is False and "актуальн" in v.reason


def test_no_image_in_the_registry_no_update():
    v = verdict(report(D3), {"when": "now"}, offer=None)
    assert v.go is False and "реестре" in v.reason


def test_after_a_failed_try_the_node_waits_and_a_new_request_resets_it():
    t0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    failed = report(D3, update_failed_at=t0.isoformat(), update_error="pull failed")
    v = verdict(failed, {"when": "now"}, now=t0 + timedelta(seconds=60))
    assert (v.go, v.waiting) == (False, True)
    assert verdict(failed, {"when": "now"}, now=t0 + timedelta(seconds=S.node_update_retry_s + 1)).go is True


# ---------------------------------------------------------------- the API


AUTH = {"Authorization": "Bearer agent-test"}
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}
REG = {"name": "win-07", "url": "http://192.168.3.77:18080", "accel": "nvidia", "ram_mib": 16384, "version": "0.1.0"}


@pytest_asyncio.fixture
async def env(app, registry):
    async with app.state.sessionmaker() as db:
        for table in (NodeAssignment, StandaloneNode, ModelDeployment):
            await db.execute(delete(table))
        await db.commit()
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()
    app.state.backend_status = {"at": "t", "backends": []}
    app.state.k8s.add_node("llm-main", accel="cpu", internal_ip="192.168.3.21", is_main=True)
    app.state.k8s.add_node("pc01", accel="cpu", internal_ip="192.168.0.193")
    return app


def image_report(**kw):
    return {"version": "0.1.0", "revision": "abc1234", "digest": D1, "tag": "nsu-llm-node:0.2.0-cpu", **kw}


@pytest.mark.docker
async def test_node_endpoints_need_the_agent_token(client, env):
    assert (await client.post("/api/nodes/pc01/image", json=image_report())).status_code == 401
    assert (await client.get("/api/nodes/pc01/desired")).status_code == 401
    assert (await client.post("/api/nodes/Bad_Name/image", json=image_report(), headers=AUTH)).status_code == 422
    bad = await client.post("/api/nodes/pc01/image", json=image_report(digest="sha256:short"), headers=AUTH)
    assert bad.status_code == 422


@pytest.mark.docker
async def test_the_node_reports_what_it_runs_and_main_tells_what_to_run(client, env):
    r = await client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "name": "nsu-llm-node:0.2.0-cpu",
        "image": {"name": "nsu-llm-node:0.2.0-cpu", "digest": D1, "version": "0.1.0", "revision": "abc1234"},
        "update": {"go": False, "mode": None, "when": None, "waiting": False, "reason": ""},
        "rpc_peers": [],  # in no split deployment (task SEC-1)
    }
    gpu = await client.get("/api/nodes/pc01/desired", params={"accel": "nvidia"}, headers=AUTH)
    assert gpu.json()["image"]["name"] == "nsu-llm-node:0.2.0-gpu" and gpu.json()["image"]["digest"] == D2
    assert (await client.get("/api/nodes/pc01/desired", params={"accel": "tpu"}, headers=AUTH)).status_code == 422
    assert (await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)).status_code == 200


@pytest.mark.docker
async def test_no_image_in_the_registry_is_an_empty_answer_not_an_error(client, env, registry):
    registry.tags.clear()
    r = await client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["image"] is None and r.json()["update"]["go"] is False


async def ask(client, h, name="pc01", **body):
    return await client.post(f"/api/admin/nodes/{name}/update", json=body, headers=h)


@pytest.mark.docker
async def test_the_admin_asks_a_node_to_update_and_the_request_ends_when_it_runs_the_new_image(client, env):
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)  # an older image
    h = csrf_headers(await login(client, env, ADMIN))
    assert (await client.post("/api/admin/nodes/pc01/update", json={"when": "now"})).status_code == 403  # no CSRF
    r = await ask(client, h, when="now")
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "pc01", "ok": True, "when": "now", "waiting": False}
    d = (await client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)).json()
    assert d["update"] == {"go": True, "mode": "requested", "when": "now", "waiting": False, "reason": ""}
    listed = (await client.get("/api/admin/versions", headers=h)).json()
    (node,) = listed["nodes"]
    assert node["update"]["when"] == "now" and node["update"]["by"] == "admin@g.nsu.ru"
    # the node replaced its container and reports the offered digest: done
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D1), headers=AUTH)
    d = (await client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)).json()
    assert d["update"]["go"] is False and d["update"]["mode"] is None
    (node,) = (await client.get("/api/admin/versions", headers=h)).json()["nodes"]
    assert node["update"] is None and node["outdated"] is False


@pytest.mark.docker
async def test_after_the_deployment_is_off_waits_for_a_serving_node(client, env):
    async with env.state.sessionmaker() as db:
        dep = ModelDeployment(name="m1", repo="o/r", file="f.gguf", ctx_per_request=4096, parallel=1)
        db.add(dep)
        await db.flush()
        db.add(NodeAssignment(node_name="pc01", deployment_id=dep.id, source="k8s"))
        await db.commit()
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)
    h = csrf_headers(await login(client, env, ADMIN))
    r = await ask(client, h, when="idle")
    assert r.json()["waiting"] is True
    ask_node = lambda: client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)  # noqa: E731
    d = (await ask_node()).json()["update"]
    assert (d["go"], d["waiting"]) == (False, True)
    (node,) = (await client.get("/api/admin/versions", headers=h)).json()["nodes"]
    assert node["update"]["waiting"] is True
    # the deployment is switched off: the node goes
    async with env.state.sessionmaker() as db:
        row = await db.get(ModelDeployment, dep.id)
        row.enabled = False
        await db.commit()
    assert (await ask_node()).json()["update"]["go"] is True
    # «сейчас» does not wait for that
    async with env.state.sessionmaker() as db:
        (await db.get(ModelDeployment, dep.id)).enabled = True
        await db.commit()
    await ask(client, h, when="now")
    assert (await ask_node()).json()["update"]["go"] is True


@pytest.mark.docker
async def test_a_failed_update_is_shown_and_delays_the_next_try(client, env):
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)
    h = csrf_headers(await login(client, env, ADMIN))
    await ask(client, h, when="now")
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3, update_error="docker pull не удался"), headers=AUTH)
    (node,) = (await client.get("/api/admin/versions", headers=h)).json()["nodes"]
    assert node["update_error"] == "docker pull не удался" and node["update_failed_at"]
    d = (await client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)).json()["update"]
    assert (d["go"], d["waiting"]) == (False, True)
    # the node restarts with the same image: the failure is still known; the admin asks again: a fresh chance
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)
    (node,) = (await client.get("/api/admin/versions", headers=h)).json()["nodes"]
    assert node["update_error"] == "docker pull не удался"
    await ask(client, h, when="now")
    (node,) = (await client.get("/api/admin/versions", headers=h)).json()["nodes"]
    assert node["update_error"] is None
    assert (await client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)).json()["update"]["go"] is True


@pytest.mark.docker
async def test_requests_that_cannot_be_made(client, env, registry):
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D1), headers=AUTH)
    h = csrf_headers(await login(client, env, ADMIN))
    r = await ask(client, h, when="now")
    assert r.status_code == 409 and "актуальном" in r.json()["detail"]
    assert (await ask(client, h, when="now", force=True)).status_code == 200  # reinstall on purpose
    assert (await ask(client, h, name="nobody")).status_code == 404
    main = await ask(client, h, name="llm-main")
    assert main.status_code == 409 and "главного" in main.json()["detail"]
    registry.tags.clear()
    versions.REGISTRY.reset()
    assert (await ask(client, h, when="now", force=True)).status_code == 409
    assert (await client.post("/api/admin/nodes/pc01/update", json={"when": "tomorrow"}, headers=h)).status_code == 422


@pytest.mark.docker
async def test_the_request_can_be_taken_back(client, env):
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)
    h = csrf_headers(await login(client, env, ADMIN))
    await ask(client, h, when="idle")
    r = await client.delete("/api/admin/nodes/pc01/update", headers=h)
    assert r.json() == {"name": "pc01", "cancelled": True}
    assert (await client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)).json()["update"]["go"] is False


@pytest.mark.docker
async def test_bulk_update_reports_each_node(client, env):
    env.state.k8s.add_node("pc02", accel="nvidia", internal_ip="192.168.0.194")
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)
    await client.post("/api/nodes/pc02/image", json=image_report(digest=D2, tag="nsu-llm-node:0.2.0-gpu"), headers=AUTH)
    h = csrf_headers(await login(client, env, ADMIN))
    r = await client.post("/api/admin/nodes/update", json={"names": ["pc01", "pc02", "ghost", "pc01"], "when": "now"}, headers=h)
    assert r.status_code == 200, r.text
    results = {x["name"]: x for x in r.json()["results"]}
    assert list(results) == ["pc01", "pc02", "ghost"]  # a name once
    assert results["pc01"]["ok"] is True
    assert results["pc02"]["ok"] is False and "актуальном" in results["pc02"]["error"]
    assert results["ghost"]["ok"] is False


@pytest.mark.docker
async def test_the_versions_page_groups_the_nodes_and_marks_the_outdated(client, env, registry):
    env.state.k8s.add_node("pc02", accel="nvidia", internal_ip="192.168.0.194")
    env.state.k8s.add_node("pc03", accel="cpu", internal_ip="192.168.0.195")
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D1), headers=AUTH)
    await client.post("/api/nodes/pc02/image", json=image_report(digest=D3, tag="nsu-llm-node:0.2.0-gpu", revision="old0001"), headers=AUTH)
    # a standalone PC that registered but never reported its image (an older node)
    await client.post("/api/nodes/register", json=REG, headers=AUTH)
    h = csrf_headers(await login(client, env, ADMIN))
    r = await client.get("/api/admin/versions", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["main"] == {"version": None, "revision": None, "digest": None}
    assert data["registry"]["available"] is True
    assert [(t["tag"], t["digest"], t["current"]) for t in data["registry"]["tags"]] == [("0.2.0-gpu", D2, True), ("0.2.0-cpu", D1, True)]
    assert data["offered"]["nvidia"]["digest"] == D2 and data["offered"]["cpu"]["version"] == "0.1.0"
    assert data["auto_update"] is False
    nodes = {n["name"]: n for n in data["nodes"]}
    assert "llm-main" not in nodes
    assert (nodes["pc01"]["outdated"], nodes["pc01"]["digest"]) == (False, D1)
    assert (nodes["pc02"]["outdated"], nodes["pc02"]["revision"]) == (True, "old0001")
    assert (nodes["pc03"]["outdated"], nodes["pc03"]["version"], nodes["pc03"]["digest"]) == (None, None, None)
    assert (nodes["win-07"]["outdated"], nodes["win-07"]["version"]) == (None, "0.1.0")  # its registration version is all that is known
    groups = {tuple(g["nodes"]): g for g in data["groups"]}
    assert groups[("pc01",)]["outdated"] is False and groups[("pc02",)]["outdated"] is True
    assert sum(g["count"] for g in data["groups"]) == 4


@pytest.mark.docker
async def test_main_reports_its_own_version(client, env, settings):
    settings.nsu_llm_version, settings.nsu_llm_revision, settings.main_image_digest = "0.1.0", "deadbee", D3
    h = csrf_headers(await login(client, env, ADMIN))
    data = (await client.get("/api/admin/versions", headers=h)).json()
    assert data["main"] == {"version": "0.1.0", "revision": "deadbee", "digest": D3}


@pytest.mark.docker
async def test_a_registry_that_is_down_does_not_break_the_page(client, env, registry):
    registry.status = 503
    h = csrf_headers(await login(client, env, ADMIN))
    r = await client.get("/api/admin/versions", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["registry"]["available"] is False and data["registry"]["tags"] == []
    assert data["offered"] == {"cpu": None, "nvidia": None}
    assert all(n["outdated"] is None for n in data["nodes"])


@pytest.mark.docker
async def test_the_updater_that_asks_main_is_noticed(client, env):
    h = csrf_headers(await login(client, env, ADMIN))
    (node,) = (await client.get("/api/admin/versions", headers=h)).json()["nodes"]
    assert node["updater_seen_at"] is None
    await client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)
    (node,) = (await client.get("/api/admin/versions", headers=h)).json()["nodes"]
    assert node["updater_seen_at"]


@pytest.mark.docker
async def test_versions_are_for_admins(client, env):
    h = csrf_headers(await login(client, env, USER))
    assert (await client.get("/api/admin/versions", headers=h)).status_code == 403
    assert (await client.post("/api/admin/nodes/pc01/update", json={}, headers=h)).status_code == 403
    assert (await client.post("/api/admin/nodes/update", json={"names": ["pc01"]}, headers=h)).status_code == 403
    assert (await client.get("/api/admin/versions")).status_code in (401, 403)


@pytest.mark.docker
async def test_run_node_sh_gets_the_answer_as_plain_lines(client, env, registry):
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)
    h = csrf_headers(await login(client, env, ADMIN))
    r = await client.get("/api/nodes/pc01/desired", params={"accel": "cpu", "format": "lines"}, headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text == f"name=nsu-llm-node:0.2.0-cpu\ngo=0\nimage=nsu-llm-node:0.2.0-cpu\ndigest={D1}\nmode=\nwhen=\nreason=\nrpc_peers=\n"
    await ask(client, h, when="now")
    r = await client.get("/api/nodes/pc01/desired", params={"accel": "cpu", "format": "lines"}, headers=AUTH)
    assert r.text == f"name=nsu-llm-node:0.2.0-cpu\ngo=1\nimage=nsu-llm-node:0.2.0-cpu\ndigest={D1}\nmode=requested\nwhen=now\nreason=\nrpc_peers=\n"
    # nothing to run (the registry has no image): go=0 whatever was asked
    versions.REGISTRY.reset()
    registry.tags.clear()
    r = await client.get("/api/nodes/pc01/desired", params={"accel": "cpu", "format": "lines"}, headers=AUTH)
    assert r.text.startswith("name=nsu-llm-node:0.2.0-cpu\ngo=0\nimage=\ndigest=\n") and "реестре" in r.text


@pytest.mark.docker
async def test_the_failure_the_host_reports_keeps_what_the_node_reported_before(client, env):
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3, revision="old0001"), headers=AUTH)
    bare = {"version": "", "revision": "", "digest": "", "tag": "", "update_error": "образ не удалось скачать"}
    assert (await client.post("/api/nodes/pc01/image", json=bare, headers=AUTH)).status_code == 200
    h = csrf_headers(await login(client, env, ADMIN))
    (node,) = (await client.get("/api/admin/versions", headers=h)).json()["nodes"]
    assert (node["digest"], node["revision"], node["version"]) == (D3, "old0001", "0.1.0")
    assert node["update_error"] == "образ не удалось скачать"


async def audit_rows(app) -> list[AuditLog]:
    async with app.state.sessionmaker() as db:
        return list((await db.scalars(select(AuditLog).where(AuditLog.action == "node.update").order_by(AuditLog.id))).all())


@pytest.mark.docker
async def test_the_requests_of_the_admin_are_in_the_audit_log(client, env):
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)
    h = csrf_headers(await login(client, env, ADMIN))
    await ask(client, h, when="idle")
    await ask(client, h, name="llm-main", when="now")  # refused: nothing is logged for it
    await client.post("/api/admin/nodes/update", json={"names": ["pc01", "ghost"], "when": "now"}, headers=h)
    await client.delete("/api/admin/nodes/pc01/update", headers=h)
    await client.delete("/api/admin/nodes/pc01/update", headers=h)  # nothing to take back: nothing is logged
    rows = await audit_rows(env)
    assert [(r.actor_email, r.target_type, r.target) for r in rows] == [("admin@g.nsu.ru", "node", "pc01")] * 3
    assert rows[0].details == {"when": "idle", "force": False, "accel": "cpu"}
    assert rows[1].details == {"when": "now", "force": False, "accel": "cpu"}
    assert rows[2].details == {"cancelled": True}


@pytest.mark.docker
async def test_auto_update_follows_the_value_the_admin_set_in_the_panel(client, env, settings):
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)
    ask_node = lambda: client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)  # noqa: E731
    h = csrf_headers(await login(client, env, ADMIN))
    assert (await ask_node()).json()["update"]["go"] is False and (await client.get("/api/admin/versions", headers=h)).json()["auto_update"] is False
    async with env.state.sessionmaker() as db:  # the panel (2-34) stores it; the env default stays false
        await settings_store.set(db, "node_auto_update", True, None)
        await db.commit()
    assert settings.node_auto_update is False
    update = (await ask_node()).json()["update"]
    assert (update["go"], update["mode"]) == (True, "auto")
    assert (await client.get("/api/admin/versions", headers=h)).json()["auto_update"] is True


@pytest.mark.docker
async def test_main_names_the_image_of_its_tag_so_the_scripts_need_no_version_of_their_own(client, env, settings, registry):
    """run-node takes the tag from NODE_IMAGE_TAG of main, whatever the registry knows (found on the 122b bed: the scripts said 0.1.0,
    main ran 0.1.1)."""
    settings.node_image_tag = "0.1.1"
    versions.REGISTRY.reset()
    for accel, want in (("cpu", "nsu-llm-node:0.1.1-cpu"), ("nvidia", "nsu-llm-node:0.1.1-gpu")):
        r = await client.get("/api/nodes/pc01/desired", params={"accel": accel}, headers=AUTH)
        assert r.json()["name"] == want and r.json()["image"] is None  # the registry has no 0.1.1 tags, the name is still given
        lines = await client.get("/api/nodes/pc01/desired", params={"accel": accel, "format": "lines"}, headers=AUTH)
        assert lines.text.splitlines()[0] == f"name={want}"


@pytest.mark.docker
async def test_the_name_of_the_node_image_is_public_and_follows_node_image_tag(client, env, settings):
    """install-node asks before it has a token main would accept: one answer names the tag (NODE_IMAGE_TAG) for every script."""
    settings.node_image_tag = "0.1.1"
    r = await client.get("/api/node-image")
    assert r.status_code == 200
    # the registry has no 0.1.1 tags: no digest to pin (SEC-2: the installer pulls by the digest when there is one)
    assert r.json() == {
        "tag": "0.1.1", "cpu": "nsu-llm-node:0.1.1-cpu", "nvidia": "nsu-llm-node:0.1.1-gpu", "digest": {"cpu": "", "nvidia": ""},
    }
    settings.node_image_tag = "0.2.0"
    assert (await client.get("/api/node-image")).json()["digest"] == {"cpu": D1, "nvidia": D2}
    settings.node_image_tag = "0.2.0"
    assert (await client.get("/api/node-image")).json()["nvidia"] == "nsu-llm-node:0.2.0-gpu"


@pytest.mark.docker
async def test_a_request_that_cannot_be_checked_against_the_registry_gets_one_try(client, env):
    """An image with no digest and no real labels cannot be compared: the node was told to go every minute until the request
    expired (7 days). One try, then the request is done."""
    await client.post("/api/nodes/pc01/image", json=image_report(digest="", revision="dev", version="dev"), headers=AUTH)
    h = csrf_headers(await login(client, env, ADMIN))
    assert (await ask(client, h, when="now")).status_code == 200
    ask_node = lambda: client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)  # noqa: E731
    first = (await ask_node()).json()["update"]
    assert (first["go"], first["mode"]) == (True, "requested")
    second = (await ask_node()).json()["update"]
    assert second["go"] is False and second["mode"] is None
    (node,) = (await client.get("/api/admin/versions", headers=h)).json()["nodes"]
    assert node["update"] is None
    # a new request is a new try
    assert (await ask(client, h, when="now", force=True)).status_code == 200
    assert (await ask_node()).json()["update"]["go"] is True


@pytest.mark.docker
async def test_a_request_with_a_known_difference_is_not_cut_short(client, env):
    """The one-try rule is only for what cannot be compared: a node that is known to be outdated keeps being told."""
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)
    h = csrf_headers(await login(client, env, ADMIN))
    await ask(client, h, when="now")
    for _ in range(3):
        assert (await client.get("/api/nodes/pc01/desired", params={"accel": "cpu"}, headers=AUTH)).json()["update"]["go"] is True


@pytest.mark.docker
async def test_a_long_failure_message_is_cut_not_refused(client, env):
    """run-node.ps1 appends the first line of the exception to its text: a 422 would hide the failure and the retry pause."""
    r = await client.post("/api/nodes/pc01/image", json={"update_error": "я" * 1500}, headers=AUTH)
    assert r.status_code == 200, r.text
    h = csrf_headers(await login(client, env, ADMIN))
    (node,) = (await client.get("/api/admin/versions", headers=h)).json()["nodes"]
    assert node["update_error"] == "я" * 500
    too_much = await client.post("/api/nodes/pc01/image", json={"update_error": "x" * 30000}, headers=AUTH)
    assert too_much.status_code == 422


@pytest.mark.docker
async def test_the_request_is_set_only_after_its_audit_row_is_committed(client, env, monkeypatch):
    """A request that acts without an audit row (the commit failed, a bulk call broke on the n-th node) is the wrong order."""
    seen = []
    real = versions.set_request

    async def spy(redis, name, when, by):
        async with env.state.sessionmaker() as db:
            rows = (await db.execute(select(AuditLog).where(AuditLog.action == "node.update"))).scalars().all()
        seen.append((name, len(rows)))
        return await real(redis, name, when, by)

    monkeypatch.setattr(versions, "set_request", spy)
    await client.post("/api/nodes/pc01/image", json=image_report(digest=D3), headers=AUTH)
    h = csrf_headers(await login(client, env, ADMIN))
    base = len(await _audit_rows(env))
    assert (await ask(client, h, when="now")).status_code == 200
    assert seen == [("pc01", base + 1)]


async def _audit_rows(env):
    async with env.state.sessionmaker() as db:
        return (await db.execute(select(AuditLog).where(AuditLog.action == "node.update"))).scalars().all()


# ---------------------------------------------------------------- rpc_peers (task SEC-1, for the node's firewall on 18050)


async def _split_deployment(app, name="big"):
    async with app.state.sessionmaker() as db:
        db.add(ModelDeployment(name=name, repo="o/r", file="f.gguf", ctx_per_request=4096, parallel=1, mode="split"))
        db.add(ModelDeployment(name="small", repo="o/r", file="f.gguf", ctx_per_request=4096, parallel=1))
        await db.commit()


@pytest.mark.docker
async def test_desired_names_the_rpc_peers_of_the_nodes_split_deployment(client, env):
    await _split_deployment(env)
    k8s = env.state.k8s
    k8s.nodes["pc01"] = replace(k8s.nodes["pc01"], deployment="big")
    k8s.add_node("pc02", accel="cpu", internal_ip="192.168.0.20", external_ip="10.1.0.2", deployment="big")
    k8s.add_node("pc03", accel="cpu", internal_ip="192.168.0.30", deployment="big")
    k8s.add_node("pc04", accel="cpu", internal_ip="192.168.0.40", deployment="small")  # another deployment
    k8s.add_node("pc05", accel="cpu", internal_ip="192.168.0.50")  # in the group of pc01, its label already gone
    await k8s.apply({
        "kind": "Deployment", "metadata": {"name": "llm-big-g0", "labels": {"llm.nsu/deployment": "big"}},
        "spec": {"template": {"metadata": {"annotations": {"llm.nsu/group-members": "pc05,pc01"}}}},
    })
    r = await client.get("/api/nodes/pc01/desired", headers=AUTH)
    assert r.json()["rpc_peers"] == ["10.1.0.2", "192.168.0.30", "192.168.0.50"]  # never itself, never main
    lines = (await client.get("/api/nodes/pc01/desired", params={"format": "lines"}, headers=AUTH)).text.splitlines()
    assert "rpc_peers=10.1.0.2,192.168.0.30,192.168.0.50" in lines
    # a node in no split deployment: an empty list, not an absent field
    r = await client.get("/api/nodes/pc04/desired", headers=AUTH)
    assert r.json()["rpc_peers"] == []
    assert "rpc_peers=" in (await client.get("/api/nodes/pc04/desired", params={"format": "lines"}, headers=AUTH)).text.splitlines()
    # the cluster does not answer: the field is absent, the node keeps its firewall as it is
    k8s.unavailable = True
    r = await client.get("/api/nodes/pc01/desired", headers=AUTH)
    assert r.status_code == 200 and "rpc_peers" not in r.json()
    assert not any(x.startswith("rpc_peers") for x in (await client.get(
        "/api/nodes/pc01/desired", params={"format": "lines"}, headers=AUTH)).text.splitlines())
