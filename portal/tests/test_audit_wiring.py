"""The admin mutations that already exist write an audit row (task 2-31a): deployments, node assignment and
maintenance, own models. A refused or empty action writes nothing; the row carries the actor, the address and no secrets."""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.gguf import GgufMeta
from app.catalog.hf import HfFile
from app.models import AuditLog
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login
from tests.test_local_models_api import API as LOCAL_API
from tests.test_local_models_api import create as create_upload
from tests.test_local_models_api import upload_all, verify
from tests.test_local_store import gguf_bytes

pytestmark = pytest.mark.docker

REPO = "org/model-GGUF"
FILE = "model-Q4_K_M.gguf"
SHA1, SHA2 = "a" * 40, "b" * 40
GiB = 2**30
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru"}
SMALL = GgufMeta("llama", 32, 32, 8, 128, 4096, 32768)


@pytest.fixture
def settings(settings, tmp_path):
    settings.hf_mirror_data_dir = str(tmp_path / "hf")  # own models: never the real volume
    settings.upload_chunk_mib = 1
    settings.local_models_settle_s = 0
    return settings


@pytest.fixture(autouse=True)
def _disk_reserve(monkeypatch):
    from app.localmodels.store import DISK_RESERVE, LocalStore

    monkeypatch.setattr(LocalStore, "reserve_bytes", lambda self: DISK_RESERVE)


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    app.state.hf = FakeHf(
        files={REPO: [HfFile(FILE, 4 * GiB)]}, metas={(REPO, FILE): SMALL}, commits={REPO: SHA1}
    )
    app.state.k8s = FakeK8s()
    app.state.k8s.add_node("pc01", accel="cpu")
    app.state.litellm = FakeLiteLLM()


async def as_admin(client, app) -> dict:
    return csrf_headers(await login(client, app, ADMIN))


async def rows(app) -> list[AuditLog]:
    async with app.state.sessionmaker() as db:
        return list((await db.scalars(select(AuditLog).order_by(AuditLog.id))).all())


async def new_dep(client, h, name="m1") -> int:
    r = await client.post("/api/admin/deployments", json={"name": name, "repo": REPO, "file": FILE}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_create_patch_and_delete_of_a_deployment_are_audited(client, app):
    h = await as_admin(client, app)
    h_ip = {**h, "X-Forwarded-For": "203.0.113.9"}  # the loopback peer is the proxy: the client is in the header
    r = await client.post("/api/admin/deployments", json={"name": "m1", "repo": REPO, "file": FILE}, headers=h_ip)
    dep_id = r.json()["id"]
    (created,) = await rows(app)
    assert (created.action, created.target_type, created.target) == ("deployment.create", "deployment", "m1")
    assert (created.actor_email, created.ip) == ("admin@g.nsu.ru", "203.0.113.9")
    assert created.actor_id is not None and created.at is not None
    assert created.details == {"engine": "llamacpp", "repo": REPO, "file": FILE, "mode": "replicas"}

    r = await client.patch(
        f"/api/admin/deployments/{dep_id}", json={"ctx_per_request": 4096, "enabled": False}, headers=h
    )
    assert r.status_code == 200
    updated = (await rows(app))[1]
    assert (updated.action, updated.target) == ("deployment.update", "m1")
    assert updated.details == {"changes": {"ctx_per_request": 4096, "enabled": False}}
    assert updated.ip == "127.0.0.1"  # no proxy header: the peer itself

    assert (await client.delete(f"/api/admin/deployments/{dep_id}", headers=h)).status_code == 204
    deleted = (await rows(app))[2]
    assert (deleted.action, deleted.target, deleted.details) == ("deployment.delete", "m1", {"repo": REPO, "file": FILE})
    assert len(await rows(app)) == 3


async def test_a_refused_or_forbidden_action_writes_no_row(client, app):
    h = await as_admin(client, app)
    dep_id = await new_dep(client, h)
    before = len(await rows(app))
    r = await client.post("/api/admin/deployments", json={"name": "m1", "repo": REPO, "file": FILE}, headers=h)
    assert r.status_code == 409  # the name is taken
    assert (await client.patch(f"/api/admin/deployments/{dep_id}", json={"ctx_per_request": 99999}, headers=h)).status_code == 422
    assert (await client.patch("/api/admin/deployments/999", json={"enabled": False}, headers=h)).status_code == 404
    assert (await client.delete("/api/admin/deployments/999", headers=h)).status_code == 404
    assert (await client.put("/api/admin/nodes/pc01/assignment", json={"deployment_id": 999}, headers=h)).status_code == 404
    assert (await client.put("/api/admin/nodes/ghost/maintenance", json={"maintenance": True}, headers=h)).status_code == 404
    assert (await client.delete(f"/api/admin/deployments/{dep_id}")).status_code == 403  # no CSRF header
    client.cookies.clear()
    h_user = csrf_headers(await login(client, app, USER))
    assert (await client.delete(f"/api/admin/deployments/{dep_id}", headers=h_user)).status_code == 403
    assert len(await rows(app)) == before


async def test_a_refresh_that_found_nothing_new_is_not_logged(client, app):
    h = await as_admin(client, app)
    dep_id = await new_dep(client, h)
    before = len(await rows(app))
    r = await client.post(f"/api/admin/deployments/{dep_id}/refresh-revision", headers=h)
    assert r.json()["changed"] is False
    assert len(await rows(app)) == before


def _assign(on: bool):
    return lambda d: ("PUT", "/api/admin/nodes/pc01/assignment", {"deployment_id": d if on else None})


@pytest.mark.parametrize(
    ("request_of", "expect"),
    [
        pytest.param(
            _assign(True), ("node.assign", "node", "pc01", {"deployment_id": "DEP", "deployment": "m1"}), id="assign"
        ),
        pytest.param(
            _assign(False), ("node.assign", "node", "pc01", {"deployment_id": None, "deployment": None}), id="unassign"
        ),
        pytest.param(
            lambda d: ("PUT", "/api/admin/nodes/PC01/maintenance", {"maintenance": True}),
            ("node.maintenance", "node", "pc01", {"maintenance": True}),
            id="maintenance-on",
        ),
        pytest.param(
            lambda d: ("PUT", "/api/admin/nodes/pc01/maintenance", {"maintenance": False}),
            ("node.maintenance", "node", "pc01", {"maintenance": False}),
            id="maintenance-off",
        ),
        pytest.param(
            lambda d: ("POST", f"/api/admin/deployments/{d}/refresh-revision", None),
            ("deployment.refresh_revision", "deployment", "m1", {"revision": SHA2, "draft_revision": None}),
            id="refresh-revision",
        ),
    ],
)
async def test_each_remaining_mutation_writes_exactly_one_row(client, app, request_of, expect):
    h = await as_admin(client, app)
    dep_id = await new_dep(client, h)
    app.state.hf.commits[REPO] = SHA2  # a new commit on main, for the refresh
    before = len(await rows(app))
    method, url, body = request_of(dep_id)
    r = await client.request(method, url, json=body, headers=h)
    assert r.status_code == 200, r.text
    (row,) = (await rows(app))[before:]
    action, target_type, target, details = expect
    details = {k: dep_id if v == "DEP" else v for k, v in details.items()}
    assert (row.action, row.target_type, row.target, row.details) == (action, target_type, target, details)
    assert (row.actor_email, row.ip) == ("admin@g.nsu.ru", "127.0.0.1")


async def test_own_model_upload_finish_and_delete_are_audited(client, app):
    h = await as_admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create_upload(client, h, content=content)).json()
    assert (await upload_all(client, h, up["id"], content)).status_code == 202
    (uploaded,) = await rows(app)
    assert (uploaded.action, uploaded.target_type, uploaded.target) == ("local_model.upload", "local_model", "m/m-Q4_K_M.gguf")
    assert uploaded.details == {"size_bytes": len(content), "sha256_expected": None}
    assert uploaded.actor_email == "admin@g.nsu.ru"

    await verify(app)
    assert (await client.delete(f"{LOCAL_API}/m?file=m-Q4_K_M.gguf", headers=h)).status_code == 204
    deleted = (await rows(app))[1]
    assert (deleted.action, deleted.target, deleted.details) == ("local_model.delete", "m", {"file": "m-Q4_K_M.gguf"})

    assert (await client.delete(f"{LOCAL_API}/other", headers=h)).status_code == 404  # no such model: nothing to log
    assert len(await rows(app)) == 2


@pytest.fixture
def commit_fails(monkeypatch):
    """The commit that would carry an audit row fails (the row is in the same transaction as the change)."""
    real = AsyncSession.commit

    async def flaky(self):
        if any(isinstance(o, AuditLog) for o in self.new):
            await self.rollback()
            raise RuntimeError("commit failed")
        await real(self)

    class Switch:
        def on(self):
            monkeypatch.setattr(AsyncSession, "commit", flaky)

        def off(self):
            monkeypatch.setattr(AsyncSession, "commit", real)

    return Switch()


@pytest.mark.parametrize("what", ["create", "patch", "delete", "assign", "maintenance", "refresh"])
async def test_a_failing_change_leaves_no_audit_row_and_no_change(client, app, commit_fails, what):
    h = await as_admin(client, app)
    dep_id = await new_dep(client, h)
    app.state.hf.commits[REPO] = SHA2
    before = len(await rows(app))
    calls = {
        "create": ("POST", "/api/admin/deployments", {"name": "m2", "repo": REPO, "file": FILE}),
        "patch": ("PATCH", f"/api/admin/deployments/{dep_id}", {"ctx_per_request": 4096}),
        "delete": ("DELETE", f"/api/admin/deployments/{dep_id}", None),
        "assign": ("PUT", "/api/admin/nodes/pc01/assignment", {"deployment_id": dep_id}),
        "maintenance": ("PUT", "/api/admin/nodes/pc01/maintenance", {"maintenance": True}),
        "refresh": ("POST", f"/api/admin/deployments/{dep_id}/refresh-revision", None),
    }
    method, url, body = calls[what]
    commit_fails.on()
    with pytest.raises(RuntimeError, match="commit failed"):
        await client.request(method, url, json=body, headers=h)
    commit_fails.off()

    assert len(await rows(app)) == before  # no audit row of a change that did not happen
    deps = {d["name"]: d for d in (await client.get("/api/admin/deployments")).json()}
    assert set(deps) - {"gemma-4-e4b"} == {"m1"}  # not created, not deleted (gemma-4-e4b is the seed deployment)
    assert deps["m1"]["ctx_per_request"] == 8192 and deps["m1"]["revision"] == SHA1 and deps["m1"]["assigned_nodes"] == []
    node = await client.put("/api/admin/nodes/pc01/maintenance", json={"maintenance": False}, headers=h)
    assert node.json()["deployment_id"] is None  # the assignment did not happen either


async def test_a_failing_upload_finish_leaves_no_audit_row(client, app, commit_fails):
    h = await as_admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create_upload(client, h, content=content)).json()
    for off in range(0, len(content), 400_000):
        r = await client.put(
            f"{LOCAL_API}/uploads/{up['id']}?offset={off}", content=content[off : off + 400_000],
            headers={**h, "Content-Type": "application/octet-stream"},
        )
        assert r.status_code == 200
    commit_fails.on()
    with pytest.raises(RuntimeError, match="commit failed"):
        await client.post(f"{LOCAL_API}/uploads/{up['id']}/finish", headers=h)
    commit_fails.off()
    assert await rows(app) == []
    assert (await client.get(f"{LOCAL_API}/uploads/{up['id']}", headers=h)).json()["status"] == "uploading"


async def test_a_half_finished_upload_is_not_logged(client, app):
    h = await as_admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create_upload(client, h, content=content)).json()
    r = await client.post(f"{LOCAL_API}/uploads/{up['id']}/finish", headers=h)
    assert r.status_code == 409  # nothing was received
    assert await rows(app) == []
