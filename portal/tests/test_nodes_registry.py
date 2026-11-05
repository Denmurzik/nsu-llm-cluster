from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from app.deployments import service
from app.models import NodeAssignment, StandaloneNode
from app import mirror_tokens
from app.nodes import identity, registry
from app.workloads.builders import llama_settings_env
from tests.fakes.k8s import FakeK8s

pytestmark = pytest.mark.docker


def r_text(value) -> str:
    return repr(value)

AUTH = {"Authorization": "Bearer agent-test"}  # the agent token; tok-test is the k3s server token
LEGACY = {"Authorization": "Bearer tok-test"}
PAYLOAD = {
    "name": "lab-1",
    "url": "http://10.0.0.5:18080",
    "accel": "cpu",
    "vram_mib": None,
    "ram_mib": 16000,
    "version": "0.1.0",
}


def test_token_matches_raw_and_k3s_full_form():
    assert registry.token_matches("secret", "secret", "server")
    assert registry.token_matches("K10abcdef::server:secret", "secret", "server")
    assert registry.token_matches("K10abcdef::node:secret", "secret", "node")
    assert not registry.token_matches("K10abcdef::server:secret", "secret", "node")
    assert not registry.token_matches("K10abcdef::node:secret", "secret", "server")
    assert not registry.token_matches("other", "secret", "server")
    assert not registry.token_matches("K10abcdef::agent:secret", "secret", "server")
    assert not registry.token_matches("K10abcdef::server:other", "secret", "server")
    assert not registry.token_matches("", "", "server")
    assert not registry.token_matches("anything", "", "node")
    assert not registry.token_matches("::server:", "", "server")
    assert not registry.token_matches("::node:", "", "node")


async def test_register_requires_bearer(client, db_session):
    assert (await client.post("/api/nodes/register", json=PAYLOAD)).status_code == 401
    r = await client.post("/api/nodes/register", json=PAYLOAD, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = await client.post("/api/nodes/register", json=PAYLOAD, headers={"Authorization": "Basic agent-test"})
    assert r.status_code == 401
    assert (await client.post("/api/nodes/heartbeat", json={"name": "lab-1"})).status_code == 401
    assert (await client.delete("/api/nodes/lab-1")).status_code == 401
    assert await db_session.scalar(select(StandaloneNode)) is None

    r = await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    assert r.status_code == 200, r.text
    reply = r.json()
    # a new name gets its own key once (approval is off in the tests' settings, tests/test_node_identity.py has it on)
    assert reply == {"ok": True, "heartbeat_interval": 15, "approved": True, "node_key": reply["node_key"]}
    assert identity.KEY_RE.fullmatch(reply["node_key"])
    row = await db_session.get(StandaloneNode, "lab-1")
    assert (row.url, row.accel, row.vram_mib, row.ram_mib, row.version) == (
        "http://10.0.0.5:18080",
        "cpu",
        None,
        16000,
        "0.1.0",
    )

    r = await client.post(
        "/api/nodes/register",
        json={**PAYLOAD, "name": "lab-2"},
        headers={"Authorization": "Bearer K10abc::node:agent-test"},
    )
    assert r.status_code == 200, r.text
    # the agent token in the server form, or the server token in the agent form, is not a valid k3s token
    for bad in ("K10abc::server:agent-test", "K10abc::node:tok-test"):
        r = await client.post("/api/nodes/register", json=PAYLOAD, headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401, bad


async def test_legacy_server_token_is_off_by_default(client):
    """Infra finding L3: a fresh main does not take the k3s server token from nodes; only an upgraded install turns it on."""
    from app.config import Settings

    assert Settings().node_legacy_token is False
    assert (await client.post("/api/nodes/register", json=PAYLOAD, headers=LEGACY)).status_code == 401


async def test_legacy_server_token_is_accepted_for_one_release(client, app, db_session, caplog, settings):
    settings.node_legacy_token = True  # an install upgraded from before the agent token (main's entrypoint sets it)
    # nodes installed before the agent token keep talking to the portal with the old token, with a warning
    caplog.set_level("WARNING", logger="app.nodes.routes")
    for auth in (LEGACY, {"Authorization": "Bearer K10abc::server:tok-test"}):
        r = await client.post("/api/nodes/register", json=PAYLOAD, headers=auth)
        assert r.status_code == 200, r.text
    assert await db_session.get(StandaloneNode, "lab-1") is not None
    warnings = [r.getMessage() for r in caplog.records if "server token" in r.getMessage()]
    assert len(warnings) == 1  # once per client address, not on every heartbeat
    assert "tok-test" not in warnings[0]

    # NODE_LEGACY_TOKEN=false closes the old token once every node has switched
    app.state.settings = app.state.settings.model_copy(update={"node_legacy_token": False})
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=LEGACY)
    assert r.status_code == 401
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)
    assert r.status_code == 200, r.text


async def test_register_is_closed_without_tokens(client, app):
    app.state.settings = app.state.settings.model_copy(
        update={"cluster_token": SecretStr(""), "agent_token": SecretStr("")}
    )
    for presented in ("", "::server:", "::node:", "K10abc::node:"):
        r = await client.post("/api/nodes/register", json=PAYLOAD, headers={"Authorization": f"Bearer {presented}"})
        assert r.status_code == 401, presented


@pytest.mark.parametrize(
    "patch",
    [
        {"url": "https://10.0.0.5:18080"},
        {"url": "http://10.0.0.5"},
        {"url": "http://10.0.0.5:18080/v1"},
        {"url": "http://:18080"},
        {"url": "http://10.0.0.5:99999"},
        {"accel": "amd"},
        {"accel": "auto"},
        {"name": "Lab_1"},
        {"name": "-lab"},
        {"name": "lab.1"},
        {"name": ""},
        {"name": "a" * 64},
        {"vram_mib": -1},
    ],
)
async def test_register_validates_payload(client, db_session, patch):
    r = await client.post("/api/nodes/register", json={**PAYLOAD, **patch}, headers=AUTH)
    assert r.status_code == 422, r.text
    assert await db_session.scalar(select(StandaloneNode)) is None


async def test_register_accepts_nvidia_and_hostname_urls(client, db_session):
    body = {**PAYLOAD, "name": "gpu-1", "url": "http://gpu-1.lab.nsu.ru:18080", "accel": "nvidia", "vram_mib": 8192}
    assert (await client.post("/api/nodes/register", json=body, headers=AUTH)).status_code == 200
    row = await db_session.get(StandaloneNode, "gpu-1")
    assert (row.accel, row.vram_mib) == ("nvidia", 8192)


async def test_register_creates_assignment_and_keeps_existing_one(client, db_session):
    dep_id = (await service.get_by_name(db_session, "gemma-4-e4b")).id
    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    row = await db_session.get(NodeAssignment, "lab-1")
    assert (row.source, row.deployment_id, row.manual) == ("standalone", None, False)

    await service.assign(db_session, "lab-1", dep_id)
    r = await client.post("/api/nodes/register", json={**PAYLOAD, "url": "http://10.0.0.6:18080"}, headers=AUTH)
    assert r.status_code == 200
    db_session.expire_all()
    row = await db_session.get(NodeAssignment, "lab-1")
    assert (row.source, row.deployment_id, row.manual) == ("standalone", dep_id, True)
    assert (await db_session.get(StandaloneNode, "lab-1")).url == "http://10.0.0.6:18080"


async def test_register_flips_k8s_assignment_to_standalone(client, db_session):
    db_session.add(NodeAssignment(node_name="lab-1", deployment_id=None, source="k8s", manual=False))
    await db_session.commit()
    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    db_session.expire_all()
    assert (await db_session.get(NodeAssignment, "lab-1")).source == "standalone"


async def test_heartbeat_returns_desired_and_404_unknown(client, db_session, node_keys):
    r = await client.post("/api/nodes/heartbeat", json={"name": "ghost", "applied_spec_hash": None}, headers=AUTH)
    assert r.status_code == 404
    assert r.json() == {"detail": "unknown node"}

    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1", "applied_spec_hash": None}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "heartbeat_interval": 15, "approved": True, "desired": None}

    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    await service.assign(db_session, "lab-1", dep.id)
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1", "applied_spec_hash": "abc"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["desired"] == {
        "deployment": "gemma-4-e4b",
        "model_repo": dep.repo,
        "model_file": dep.file,
        "public_model_name": "gemma-4-e4b",
        "ctx_size": dep.ctx_per_request * dep.parallel,
        "parallel": dep.parallel,
        "spec_hash": service.spec_hash(dep),
        "model_revision": dep.revision or "",
        "backend_api_key": identity.backend_key_of(node_keys.keys["lab-1"]),  # the node's own (task SEC-1)
        # the HF mirror on main by its IP (MAIN_ADVERTISE_IP), with this node's name for the progress report (no mirror key)
        "hf_endpoint": "https://192.168.3.21/hf/c/lab-1",
        "hf_token": "",
        "llm_env": dict(llama_settings_env(dep)),  # the llama.cpp settings, as the k3s pods get them
    }
    db_session.expire_all()
    assert (await db_session.get(StandaloneNode, "lab-1")).applied_spec_hash == "abc"

    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)
    assert r.status_code == 200
    db_session.expire_all()
    assert (await db_session.get(StandaloneNode, "lab-1")).applied_spec_hash is None


async def test_heartbeat_refreshes_last_seen(client, db_session):
    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    old = datetime.now(UTC) - timedelta(minutes=10)
    await db_session.execute(update(StandaloneNode).values(last_seen_at=old))
    await db_session.commit()
    assert await registry.live_nodes(db_session, 45) == []
    await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)
    assert [n.name for n in await registry.live_nodes(db_session, 45)] == ["lab-1"]


async def test_delete_removes_the_assignment_and_deletes_k8s_node(client, app, db_session):
    app.state.k8s = k8s = FakeK8s()
    k8s.add_node("lab-1", accel="cpu")
    dep_id = (await service.get_by_name(db_session, "gemma-4-e4b")).id
    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    await service.assign(db_session, "lab-1", dep_id)

    r = await client.delete("/api/nodes/lab-1", headers=AUTH)
    assert r.status_code == 204
    assert r.content == b""
    assert k8s.deleted_nodes == ["lab-1"]
    db_session.expire_all()
    assert await db_session.get(StandaloneNode, "lab-1") is None
    assert await db_session.get(NodeAssignment, "lab-1") is None  # task 2-31: the node's removal is complete

    # unknown name and an unavailable cluster are not errors
    k8s.unavailable = True
    assert (await client.delete("/api/nodes/lab-1", headers=AUTH)).status_code == 204
    assert (await client.delete("/api/nodes/ghost", headers=AUTH)).status_code == 204
    assert k8s.deleted_nodes == ["lab-1"]


async def test_re_registered_node_keeps_assignment_unless_it_was_deleted(client, db_session):
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    await service.assign(db_session, "lab-1", dep.id)
    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)  # a restart of the container
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)
    assert r.json()["desired"]["spec_hash"] == service.spec_hash(dep)
    await client.delete("/api/nodes/lab-1", headers=AUTH)  # -Uninstall: a node installed again starts without a model
    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)
    assert r.json()["desired"] is None


async def test_live_nodes_respects_ttl(db_session):
    await registry.upsert(db_session, "fresh", "http://10.0.0.1:18080", "cpu", None, 1000, "0.1.0")
    await registry.upsert(db_session, "stale", "http://10.0.0.2:18080", "cpu", None, 1000, "0.1.0")
    await db_session.execute(
        update(StandaloneNode)
        .where(StandaloneNode.name == "stale")
        .values(last_seen_at=datetime.now(UTC) - timedelta(seconds=60))
    )
    await db_session.commit()
    assert [n.name for n in await registry.live_nodes(db_session, 45)] == ["fresh"]
    assert [n.name for n in await registry.live_nodes(db_session, 120)] == ["fresh", "stale"]
    assert await registry.heartbeat(db_session, "ghost", None) is False
    assert await registry.remove(db_session, "ghost") is False
    assert await registry.remove(db_session, "stale") is True


async def test_heartbeat_hands_the_node_its_own_backend_key(client, app, db_session, monkeypatch, node_keys):
    """Rule 3 of task SEC-1: the shared BACKEND_API_KEY never goes to a node that has its own key; the heartbeat hands out
    the node's own backend key, derived from its key (the node computes the same, registrar.sh)."""
    monkeypatch.setattr(app.state.settings, "backend_api_key", SecretStr("hex-backend-key"))
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    await client.post("/api/nodes/register", json={**PAYLOAD, "name": "lab-2", "url": "http://10.0.0.6:18080"}, headers=AUTH)
    await service.assign(db_session, "lab-1", dep.id)
    await service.assign(db_session, "lab-2", dep.id)
    one = (await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)).json()["desired"]
    two = (await client.post("/api/nodes/heartbeat", json={"name": "lab-2"}, headers=AUTH)).json()["desired"]
    assert one["backend_api_key"] == identity.backend_key_of(node_keys.keys["lab-1"])
    assert two["backend_api_key"] == identity.backend_key_of(node_keys.keys["lab-2"])
    assert len({one["backend_api_key"], two["backend_api_key"], "hex-backend-key"}) == 3
    assert service.spec_hash(dep) == one["spec_hash"]  # the key is not part of the spec


async def test_main_node_cannot_be_deleted_with_the_cluster_token(client, app):
    app.state.k8s = k8s = FakeK8s()
    k8s.add_node("llm-main", accel="cpu")
    for name in ("llm-main", "LLM-Main"):
        r = await client.delete(f"/api/nodes/{name}", headers=AUTH)
        assert r.status_code == 409 and r.json()["detail"] == "Главный сервер нельзя удалить"
    assert k8s.deleted_nodes == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:18080",
        "http://127.8.8.8:18080",
        "http://0.0.0.0:18080",
        "http://169.254.169.254:80",
        "http://localhost:18080",
        "http://foo.localhost:18080",
        "http://127.1:18080",
        "http://2130706433:18080",
        "http://224.0.0.1:18080",
    ],
)
async def test_register_rejects_loopback_and_link_local_urls(client, db_session, url):
    r = await client.post("/api/nodes/register", json={**PAYLOAD, "url": url}, headers=AUTH)
    assert r.status_code == 422
    assert await db_session.get(StandaloneNode, "lab-1") is None


@pytest.mark.parametrize("url", ["http://10.0.0.5:18080", "http://192.168.3.7:18080", "http://lab-1.nsu.ru:18080"])
async def test_register_accepts_lan_urls(client, url):
    r = await client.post("/api/nodes/register", json={**PAYLOAD, "url": url}, headers=AUTH)
    assert r.status_code == 200, r.text


async def test_heartbeat_overlong_fields_are_422_not_500(client):
    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    for body in (
        {"name": "lab-1", "applied_spec_hash": "a" * 65},
        {"name": "a" * 64},
        {"name": "Not Valid!"},
    ):
        r = await client.post("/api/nodes/heartbeat", json=body, headers=AUTH)
        assert r.status_code == 422, body
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1", "applied_spec_hash": "a" * 64}, headers=AUTH)
    assert r.status_code == 200


async def test_register_overlong_fields_are_422(client):
    for field, value in (("name", "a" * 64), ("url", "http://10.0.0.5:" + "1" * 300), ("version", "v" * 65)):
        r = await client.post("/api/nodes/register", json={**PAYLOAD, field: value}, headers=AUTH)
        assert r.status_code == 422, field


async def test_heartbeat_points_a_standalone_node_at_the_mirror(app, client, db_session, monkeypatch):
    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    await service.assign(db_session, "lab-1", dep.id)
    settings = app.state.settings
    monkeypatch.setattr(settings, "hf_mirror_token", SecretStr("hf_" + "ab" * 20))
    monkeypatch.setattr(settings, "hf_mirror_url", "https://192.168.3.21/hf/")
    desired = (await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)).json()["desired"]
    # a token for the repositories of its deployment only, never the mirror's own key (rule 4 of task SEC-1)
    scoped = mirror_tokens.mint("hf_" + "ab" * 20, "lab-1", {dep.repo})
    assert (desired["hf_endpoint"], desired["hf_token"]) == ("https://192.168.3.21/hf/c/lab-1", scoped)
    assert "hf_" + "ab" * 20 not in r_text(desired)
    assert service.spec_hash(dep) == desired["spec_hash"]  # the mirror address is not part of the spec
    monkeypatch.setattr(settings, "hf_mirror_enabled", False)
    desired = (await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)).json()["desired"]
    assert (desired["hf_endpoint"], desired["hf_token"]) == ("", "")  # the node's own HF_ENDPOINT / HF_TOKEN
    # an own model file (local/<name>) exists on the mirror only: the node is sent there even with the mirror off
    dep.repo = "local/own"
    await db_session.commit()
    desired = (await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)).json()["desired"]
    assert desired["model_repo"] == "local/own"
    scoped = mirror_tokens.mint("hf_" + "ab" * 20, "lab-1", {"local/own"})
    assert (desired["hf_endpoint"], desired["hf_token"]) == ("https://192.168.3.21/hf/c/lab-1", scoped)


async def test_heartbeat_carries_the_llama_settings_to_a_standalone_node(app, client, db_session, monkeypatch):
    """Standalone nodes run serve-llama.sh with the same settings as the k3s pods (builders.llama_settings_env)."""
    await client.post("/api/nodes/register", json=PAYLOAD, headers=AUTH)
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    await service.assign(db_session, "lab-1", dep.id)
    dep.reasoning, dep.kv_cache_type = "auto", "q4_0"
    dep.sampling = {"temperature": 0.5, "top_k": 30}
    dep.draft_repo, dep.draft_file, dep.draft_revision = "org/draft-GGUF", "d.gguf", "d" * 40
    dep.mmproj_file, dep.extra_args = "mmproj-F16.gguf", "--no-warmup --seed 3"
    dep.system_prompt, dep.experimental = "hi", True  # the gateway's: not for the node
    await db_session.commit()
    desired = (await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)).json()["desired"]
    assert desired["llm_env"] == {
        "THINKING": "auto",
        "KV_CACHE_TYPE": "q4_0",
        "SAMPLING_ARGS": "--temp 0.5 --top-k 30",
        "DRAFT_REPO": "org/draft-GGUF", "DRAFT_FILE": "d.gguf", "DRAFT_REVISION": "d" * 40,
        "MMPROJ_FILE": "mmproj-F16.gguf",
        "EXTRA_ARGS": "--no-warmup\n--seed\n3",
    }
    assert desired["spec_hash"] == service.spec_hash(dep)  # a changed setting restarts the node's server
    # an own draft file next to a Hugging Face model: the node is sent to the mirror even with it off
    monkeypatch.setattr(app.state.settings, "hf_mirror_enabled", False)
    desired = (await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)).json()["desired"]
    assert desired["hf_endpoint"] == ""
    dep.draft_repo = "local/small"
    await db_session.commit()
    desired = (await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=AUTH)).json()["desired"]
    assert desired["hf_endpoint"] == "https://192.168.3.21/hf/c/lab-1"
