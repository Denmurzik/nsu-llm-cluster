import uuid

import httpx
import pytest
from sqlalchemy import select

from app.litellm_client import Activity, KeyUsage
from app.models import ApiKeyRef
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

GATEWAY_DOWN = "Шлюз моделей недоступен"
OTHER_CLAIMS = {"sub": "fake-user-2", "email": "other@g.nsu.ru", "preferred_username": "other"}


def other_client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


@pytest.fixture
async def fake(app):
    app.state.litellm = FakeLiteLLM()
    return app.state.litellm


@pytest.fixture
async def me(client, app, fake):
    return await login(client, app)


async def create(client, me, alias="ноутбук", expires_days=None):
    return await client.post(
        "/api/keys", json={"alias": alias, "expires_days": expires_days}, headers=csrf_headers(me)
    )


async def test_create_key_returns_value_once_and_lists_masked(client, app, fake, me, db_session):
    r = await create(client, me, "мой ключ", 30)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"id", "alias", "key", "masked", "created_at", "expires_at"}
    assert body["alias"] == "мой ключ"
    assert body["key"].startswith("sk-")
    assert body["masked"] == body["key"][:3] + "…" + body["key"][-4:]
    assert body["expires_at"] is not None

    (record,) = fake.keys.values()
    uid = me["id"]
    settings = app.state.settings
    assert record["user_id"] == uid
    assert record["alias"] == f"{uid[:8]}-мой ключ"
    assert record["rpm_limit"] == settings.key_rpm_limit
    assert record["tpm_limit"] == settings.key_tpm_limit
    assert record["metadata"] == {"portal": True, "user_email": "user@g.nsu.ru"}

    row = (await db_session.scalars(select(ApiKeyRef))).one()
    assert body["key"] not in {row.litellm_token, row.alias, row.masked}
    assert row.masked == body["masked"] and not row.is_playground

    listing = await client.get("/api/keys")
    assert listing.status_code == 200
    assert body["key"] not in listing.text
    assert listing.json() == [
        {
            "id": body["id"],
            "alias": "мой ключ",
            "masked": body["masked"],
            "created_at": body["created_at"],
            "expires_at": body["expires_at"],
            "tokens_30d": 0,
            "requests_30d": 0,
        }
    ]


async def test_create_key_without_expiry_and_90_days(client, fake, me):
    r = await create(client, me, "бессрочный")
    assert r.status_code == 201 and r.json()["expires_at"] is None
    r = await create(client, me, "три месяца", 90)
    assert r.status_code == 201 and r.json()["expires_at"] is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"alias": "", "expires_days": None},
        {"alias": "x" * 41, "expires_days": None},
        {"alias": "bad/alias", "expires_days": None},
        {"alias": "ok", "expires_days": 7},
        {"alias": "__playground", "expires_days": None},
        {"alias": "__любой", "expires_days": None},
    ],
)
async def test_create_key_validates_payload(client, fake, me, payload):
    r = await client.post("/api/keys", json=payload, headers=csrf_headers(me))
    assert r.status_code == 422
    assert not fake.keys


async def test_list_orders_newest_first_with_usage(client, fake, me):
    first = (await create(client, me, "первый")).json()
    second = (await create(client, me, "второй")).json()
    token = next(t for t, k in fake.keys.items() if k["alias"].endswith("первый"))
    fake.activity[me["id"]] = Activity(by_key={token: KeyUsage(tokens=1234, requests=7)})
    listing = (await client.get("/api/keys")).json()
    assert [k["id"] for k in listing] == [second["id"], first["id"]]
    assert (listing[1]["tokens_30d"], listing[1]["requests_30d"]) == (1234, 7)
    assert (listing[0]["tokens_30d"], listing[0]["requests_30d"]) == (0, 0)


async def test_create_key_requires_csrf(client, fake, me):
    r = await client.post("/api/keys", json={"alias": "a", "expires_days": None})
    assert r.status_code == 403
    r = await client.post("/api/keys", json={"alias": "a", "expires_days": None}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    assert not fake.keys
    key = (await create(client, me, "a")).json()
    assert (await client.delete(f"/api/keys/{key['id']}")).status_code == 403
    assert fake.keys


async def test_keys_require_login(app, fake):
    async with other_client(app) as anon:
        assert (await anon.get("/api/keys")).status_code == 401
        assert (await anon.get("/api/usage")).status_code == 401
        assert (await anon.post("/api/keys", json={"alias": "a", "expires_days": None})).status_code == 401


async def test_key_limit_409_excludes_playground(client, app, fake, me, db_session):
    db_session.add(
        ApiKeyRef(
            user_id=uuid.UUID(me["id"]),
            litellm_token="pg-token",
            alias="x-__playground",
            masked="sk-…abcd",
            is_playground=True,
        )
    )
    await db_session.commit()
    limit = app.state.settings.max_keys_per_user
    for i in range(limit):
        r = await create(client, me, f"key {i}")
        assert r.status_code == 201, r.text
    r = await create(client, me, "лишний")
    assert r.status_code == 409
    assert r.json() == {"detail": f"Достигнут лимит ключей: {limit}"}
    assert len(fake.keys) == limit
    assert len((await client.get("/api/keys")).json()) == limit


async def test_limit_is_per_user(client, app, fake, me):
    for i in range(app.state.settings.max_keys_per_user):
        assert (await create(client, me, f"k{i}")).status_code == 201
    async with other_client(app) as other:
        other_me = await login(other, app, OTHER_CLAIMS)
        assert (await create(other, other_me, "k0")).status_code == 201


async def test_duplicate_alias_409(client, fake, me):
    assert (await create(client, me, "один")).status_code == 201
    r = await create(client, me, "один")
    assert r.status_code == 409
    assert r.json() == {"detail": "Ключ с таким названием уже есть"}
    assert len(fake.keys) == 1


async def test_delete_key_removes_it(client, fake, me):
    key = (await create(client, me, "удалить")).json()
    r = await client.delete(f"/api/keys/{key['id']}", headers=csrf_headers(me))
    assert r.status_code == 204
    assert not fake.keys
    assert (await client.get("/api/keys")).json() == []
    assert (await client.delete(f"/api/keys/{key['id']}", headers=csrf_headers(me))).status_code == 404
    assert (await client.delete("/api/keys/not-a-uuid", headers=csrf_headers(me))).status_code == 404


async def test_delete_other_users_key_404(client, app, fake, me):
    key = (await create(client, me, "чужой")).json()
    async with other_client(app) as other:
        other_me = await login(other, app, OTHER_CLAIMS)
        r = await other.delete(f"/api/keys/{key['id']}", headers=csrf_headers(other_me))
        assert r.status_code == 404
        assert (await other.get("/api/keys")).json() == []
    assert len(fake.keys) == 1
    assert len((await client.get("/api/keys")).json()) == 1


async def test_litellm_down_create_503_and_list_degrades(client, fake, me, db_session):
    key = (await create(client, me, "живой")).json()
    fake.unavailable = True

    r = await create(client, me, "новый")
    assert r.status_code == 503
    assert r.json() == {"detail": GATEWAY_DOWN}
    assert len((await db_session.scalars(select(ApiKeyRef))).all()) == 1

    listing = await client.get("/api/keys")
    assert listing.status_code == 200
    assert listing.headers["X-Usage-Unavailable"] == "1"
    (item,) = listing.json()
    assert item["id"] == key["id"] and item["tokens_30d"] == 0 and item["requests_30d"] == 0

    r = await client.delete(f"/api/keys/{key['id']}", headers=csrf_headers(me))
    assert r.status_code == 503
    assert r.json() == {"detail": GATEWAY_DOWN}
    assert len((await db_session.scalars(select(ApiKeyRef))).all()) == 1

    fake.unavailable = False
    listing = await client.get("/api/keys")
    assert "X-Usage-Unavailable" not in listing.headers


async def test_reserved_alias_422_keeps_playground_working(client, fake, me):
    r = await create(client, me, "__playground")
    assert r.status_code == 422
    assert "__" in r.text
    assert not fake.keys
    r = await client.post(
        "/api/playground/chat",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=csrf_headers(me),
    )
    assert r.status_code == 200 and r.text.rstrip().endswith("data: [DONE]")
