"""Experimental models are for admins (task 2-23): visibility, key models by role, the migration of old keys,
role changes and the Playground. The gateway itself (access groups, the hook) is covered against the real
LiteLLM image in tests/integration/test_litellm_real.py."""

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app import key_access
from app.catalog.gguf import GgufMeta
from app.catalog.hf import HfFile
from app.controllers.backends import BackendController
from app.litellm_client import LiteLLMError
from app.models import ModelDeployment, NodeAssignment, PortalFlag, User
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

GiB = 2**30
REPO, FILE, MMPROJ = "org/model-GGUF", "model-Q4_K_M.gguf", "mmproj-F16.gguf"
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru"}
META = GgufMeta("llama", 32, 32, 8, 128, 4096, 32768)
CHAT = {"messages": [{"role": "user", "content": "Привет"}]}


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
    app.state.hf = FakeHf(files={REPO: [HfFile(FILE, 4 * GiB), HfFile(MMPROJ, GiB // 2)]}, metas={(REPO, FILE): META})
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()


@pytest.fixture
def fake(app):
    return app.state.litellm


def second_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


@pytest_asyncio.fixture
async def admin(client, app):
    return await login(client, app, ADMIN)


@pytest_asyncio.fixture
async def user_client(app, admin):
    """A logged-in ordinary user next to the admin (`client`)."""
    async with second_client(app) as c:
        c.me = await login(c, app, USER)
        yield c


async def make_models(client, admin):
    h = csrf_headers(admin)
    for name, extra in (("pub", {}), ("exp", {"experimental": True, "mmproj_file": MMPROJ})):
        r = await client.post("/api/admin/deployments", json={"name": name, "repo": REPO, "file": FILE, **extra}, headers=h)
        assert r.status_code == 201, r.text


# ---------- visibility ----------


async def test_the_catalog_hides_experimental_models_from_users(client, user_client, admin):
    await make_models(client, admin)
    by_name = {m["name"]: m for m in (await client.get("/api/models")).json()}
    assert set(by_name) == {"pub", "exp"}
    assert (by_name["exp"]["experimental"], by_name["exp"]["vision"]) == (True, True)
    assert (by_name["pub"]["experimental"], by_name["pub"]["vision"]) == (False, False)
    mine = (await user_client.get("/api/models")).json()
    assert [m["name"] for m in mine] == ["pub"] and mine[0]["experimental"] is False


async def test_the_login_page_status_never_lists_experimental_models(client, admin, app):
    await make_models(client, admin)
    async with second_client(app) as anonymous:
        assert [m["name"] for m in (await anonymous.get("/api/public/status")).json()["models"]] == ["pub"]
    # an admin's own session does not change the public answer
    assert [m["name"] for m in (await client.get("/api/public/status")).json()["models"]] == ["pub"]


async def test_a_deployment_that_becomes_experimental_vanishes_for_users_at_once(client, user_client, admin):
    await make_models(client, admin)
    dep_id = next(m for m in (await client.get("/api/admin/deployments")).json() if m["name"] == "pub")["id"]
    assert [m["name"] for m in (await user_client.get("/api/models")).json()] == ["pub"]
    r = await client.patch(f"/api/admin/deployments/{dep_id}", json={"experimental": True}, headers=csrf_headers(admin))
    assert r.status_code == 200, r.text
    assert (await user_client.get("/api/models")).json() == []


# ---------- keys by role ----------


async def test_a_key_is_created_with_the_models_of_its_owners_role(client, user_client, admin, fake):
    r = await user_client.post("/api/keys", json={"alias": "мой"}, headers=csrf_headers(user_client.me))
    assert r.status_code == 201, r.text
    r = await client.post("/api/keys", json={"alias": "админский"}, headers=csrf_headers(admin))
    assert r.status_code == 201, r.text
    models = {k["user_id"]: k["models"] for k in fake.keys.values()}
    assert models == {user_client.me["id"]: ["public"], admin["id"]: ["public", "experimental"]}


async def test_playground_keys_follow_the_same_rule(client, user_client, admin, fake):
    await make_models(client, admin)
    for c, who in ((user_client, user_client.me), (client, admin)):
        r = await c.post("/api/playground/chat", json={"model": "pub", **CHAT}, headers=csrf_headers(who))
        assert r.status_code == 200, r.text
    models = {k["user_id"]: k["models"] for k in fake.keys.values()}
    assert models == {user_client.me["id"]: ["public"], admin["id"]: ["public", "experimental"]}


# ---------- Playground ----------


async def test_playground_refuses_an_experimental_model_to_a_user_and_serves_it_to_an_admin(
    client, user_client, admin, fake
):
    await make_models(client, admin)
    await fake.add_model("pm-e", "exp", "http://h/v1", "k8s", "n1", 4096, experimental=True)
    r = await user_client.post("/api/playground/chat", json={"model": "exp", **CHAT}, headers=csrf_headers(user_client.me))
    assert r.status_code == 403 and r.json()["detail"] == "Модель доступна только администраторам"
    assert fake.chat_calls == [] and fake.keys == {}  # refused before the gateway and before a key was issued
    r = await client.post("/api/playground/chat", json={"model": "exp", **CHAT}, headers=csrf_headers(admin))
    assert r.status_code == 200, r.text
    assert len(fake.chat_calls) == 1


async def test_a_gateway_refusal_of_the_model_is_a_russian_403_and_does_not_reissue_the_key(user_client, fake):
    # not in the portal's catalog, so only the gateway knows it is experimental
    await fake.add_model("pm-e", "secret", "http://h/v1", "k8s", "n1", 4096, experimental=True)
    r = await user_client.post(
        "/api/playground/chat", json={"model": "secret", **CHAT}, headers=csrf_headers(user_client.me)
    )
    assert r.status_code == 403 and r.json()["detail"] == "Эта модель вам недоступна"
    assert len(fake.keys) == 1  # a refusal for the model is not an expired key: no second key


# ---------- the migration of old keys and role changes ----------


def old_style_keys(fake):
    """Keys as before this feature: `models: []` meant every model."""
    for record in fake.keys.values():
        record["models"] = []


async def users_with_keys(client, user_client, admin):
    for c, who in ((user_client, user_client.me), (client, admin)):
        r = await c.post("/api/keys", json={"alias": "k"}, headers=csrf_headers(who))
        assert r.status_code == 201, r.text


def models_of(fake, user_id):
    return sorted({tuple(k["models"]) for k in fake.keys.values() if k["user_id"] == user_id})


async def test_old_keys_are_migrated_once_and_the_second_round_changes_nothing(client, user_client, admin, app, fake):
    await users_with_keys(client, user_client, admin)
    old_style_keys(fake)
    fake.key_model_updates.clear()
    assert await key_access.sweep(app.state) == 2
    assert models_of(fake, user_client.me["id"]) == [("public",)]
    assert models_of(fake, admin["id"]) == [("public", "experimental")]
    calls = len(fake.key_model_updates)
    assert await key_access.sweep(app.state) == 0
    assert len(fake.key_model_updates) == calls  # idempotent: nothing is sent again
    async with app.state.sessionmaker() as db:
        flags = {f.key: f.value for f in (await db.scalars(select(PortalFlag))).all()}
    assert flags == {f"keyaccess:{user_client.me['id']}": "user", f"keyaccess:{admin['id']}": "admin"}


async def test_the_migration_reaches_every_key_of_a_user_the_hidden_playground_one_too(
    client, user_client, admin, app, fake
):
    await make_models(client, admin)
    await users_with_keys(client, user_client, admin)
    for c, who in ((user_client, user_client.me), (client, admin)):
        await c.post("/api/playground/chat", json={"model": "pub", **CHAT}, headers=csrf_headers(who))
    old_style_keys(fake)
    await key_access.sweep(app.state)
    assert len(fake.keys) == 4
    assert models_of(fake, user_client.me["id"]) == [("public",)]
    assert models_of(fake, admin["id"]) == [("public", "experimental")]


async def test_removing_the_admin_role_cuts_experimental_access_at_once(client, admin, app, fake):
    await make_models(client, admin)
    await fake.add_model("pm-e", "exp", "http://h/v1", "k8s", "n1", 4096, experimental=True)
    r = await client.post("/api/keys", json={"alias": "k"}, headers=csrf_headers(admin))
    key = r.json()["key"]
    await key_access.sweep(app.state)
    assert [c async for c in fake.stream_chat(key, {"model": "exp", **CHAT})]  # works while an admin

    app.state.settings.admin_emails = ""  # ADMIN_EMAILS no longer lists them
    assert await key_access.sweep(app.state) == 1
    with pytest.raises(LiteLLMError) as exc:
        async for _ in fake.stream_chat(key, {"model": "exp", **CHAT}):
            pass
    assert exc.value.status == 403
    assert models_of(fake, admin["id"]) == [("public",)]


async def test_a_database_role_change_is_picked_up_too(client, user_client, admin, app, fake):
    await users_with_keys(client, user_client, admin)
    await key_access.sweep(app.state)
    async with app.state.sessionmaker() as db:
        row = await db.scalar(select(User).where(User.email == "admin@g.nsu.ru"))
        row.role, row.role_source = "user", "manual"  # what the panel writes
        await db.commit()
    assert await key_access.sweep(app.state) == 1
    assert models_of(fake, admin["id"]) == [("public",)]


async def test_a_new_admin_gets_the_experimental_group(user_client, app, fake):
    r = await user_client.post("/api/keys", json={"alias": "k"}, headers=csrf_headers(user_client.me))
    assert r.status_code == 201
    await key_access.sweep(app.state)
    assert models_of(fake, user_client.me["id"]) == [("public",)]
    app.state.settings.admin_emails = "admin@g.nsu.ru,ivan@g.nsu.ru"
    async with second_client(app) as c:  # the next (verified) login makes them an admin
        await login(c, app, {**USER, "email_verified": True})
    assert await key_access.sweep(app.state) == 1
    assert models_of(fake, user_client.me["id"]) == [("public", "experimental")]


async def test_demotions_are_handled_before_promotions(client, user_client, admin, app, fake, monkeypatch):
    await users_with_keys(client, user_client, admin)
    await key_access.sweep(app.state)
    app.state.settings.admin_emails = "ivan@g.nsu.ru"  # the admin loses the role, the user is listed
    async with second_client(app) as c:
        await login(c, app, {**USER, "email_verified": True})
    monkeypatch.setattr(key_access, "BATCH", 1)
    assert await key_access.sweep(app.state) == 1
    assert models_of(fake, admin["id"]) == [("public",)]  # the cut came first
    assert models_of(fake, user_client.me["id"]) == [("public",)]  # the grant waits for the next round
    assert await key_access.sweep(app.state) == 1
    assert models_of(fake, user_client.me["id"]) == [("public", "experimental")]


async def test_a_gateway_that_is_down_leaves_the_flags_and_the_next_round_retries(
    client, user_client, admin, app, fake
):
    await users_with_keys(client, user_client, admin)
    old_style_keys(fake)
    fake.unavailable = True
    assert await key_access.sweep(app.state) == 0
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(PortalFlag))).all() == []
    fake.unavailable = False
    assert await key_access.sweep(app.state) == 2


async def test_users_without_keys_cost_nothing(client, user_client, admin, app, fake):
    fake.key_model_updates.clear()
    assert await key_access.sweep(app.state) == 0
    assert fake.key_model_updates == []


async def test_the_backend_controller_runs_the_sweep_every_round(client, user_client, admin, app, fake):
    await users_with_keys(client, user_client, admin)
    old_style_keys(fake)
    await BackendController(app.state).reconcile_once()
    assert models_of(fake, user_client.me["id"]) == [("public",)]
