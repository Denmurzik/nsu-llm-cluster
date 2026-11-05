"""Task SEC-1: per-node identity, approval of new nodes, per-node backend keys, scoped mirror tokens (rules 1-4) and the
DNS check of node urls (L5). Real Postgres and the real routers and controllers; Redis is fakeredis, the cluster FakeK8s,
LiteLLM FakeLiteLLM. Every call here sets X-Node-Key itself (an empty one: no key), so the tests' NodeKeys helper
(conftest) stays out of the way."""

import asyncio
import hashlib
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select

from app import mirror_tokens, settings_store
from app.controllers.backends import BackendController
from app.controllers.workloads import WorkloadController
from app.deployments import service
from app.k8s import K8sUnavailable
from app.models import AuditLog, NodeAssignment, NodeIdentity, StandaloneNode
from app.nodes import identity, routes
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

TOKEN = {"Authorization": "Bearer agent-test"}
NO_KEY = {**TOKEN, "X-Node-Key": ""}
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
REG = {"name": "lab-1", "url": "http://10.0.0.5:18080", "accel": "cpu", "ram_mib": 16000, "version": "0.1.0"}
FROM_ELSEWHERE = {"X-Forwarded-For": "10.66.66.66"}  # Caddy's peer: another machine than the node's
IMAGE = {"version": "0.1.0", "revision": "abc", "digest": "", "tag": "nsu-llm-node:0.2.1-cpu"}
SAMPLE = {"gpus": [], "ram_used_mib": 1, "ram_total_mib": 2, "cpu_pct": 1.0, "disk_used_bytes": 0, "disk_free_bytes": 1,
          "llama_up": False, "rpc_up": False}


def with_key(key: str, **extra) -> dict:
    return {**TOKEN, "X-Node-Key": key, **extra}


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()
    app.state.backend_status = {"at": "t", "backends": []}
    app.state.k8s.add_node("llm-main", accel="cpu", internal_ip="192.168.3.21", is_main=True)
    return app


async def approval(app, on: bool) -> None:
    async with app.state.sessionmaker() as db:
        await settings_store.set(db, identity.APPROVAL_SETTING, on, None)
        await db.commit()


async def register(client, body=None, headers=None) -> dict:
    r = await client.post("/api/nodes/register", json=body or REG, headers=headers or NO_KEY)
    assert r.status_code == 200, r.text
    return r.json()


async def admin(client, app) -> dict:
    return csrf_headers(await login(client, app, ADMIN))


# ---- rule 1: the agent token only joins ---------------------------------------------------------------------------


async def test_a_new_name_gets_its_own_key_once_and_only_its_hash_is_kept(client, app, db_session):
    reply = await register(client)
    key = reply["node_key"]
    assert identity.KEY_RE.fullmatch(key) and reply["approved"] is True  # approval is off in the tests' settings
    row = await db_session.get(NodeIdentity, "lab-1")
    assert row.key_hash == hashlib.sha256(key.encode()).hexdigest() and row.backend_key == identity.backend_key_of(key)
    assert key not in repr(row.__dict__)
    again = await register(client, headers=with_key(key))  # the same node registering again: no new key
    assert "node_key" not in again and again["approved"] is True
    audit = (await db_session.scalars(select(AuditLog).where(AuditLog.action == "node.enroll"))).all()
    assert [a.target for a in audit] == ["lab-1"] and key not in str(audit[0].details)


async def test_the_agent_token_alone_cannot_take_over_or_remove_a_name_that_has_a_key(client, app, db_session):
    """Finding H1: with the token of every class PC an attacker re-registered a known name with his own url (LiteLLM sent
    him the users' chats with the backend key), read its desired config and deleted any node."""
    app.state.k8s.add_node("lab-1", accel="cpu", internal_ip="10.0.0.5")
    key = (await register(client))["node_key"]
    evil = {**REG, "url": "http://10.66.66.66:8080"}
    # without the key the registration is only a claim for the admin (task B2): the name, its key and its url stay
    reply = await register(client, evil, {**NO_KEY, **FROM_ELSEWHERE})
    assert reply["approved"] is False
    claim_key = reply["node_key"]
    r = await client.post("/api/nodes/lab-1/stop", headers=with_key(claim_key))
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.PENDING
    r = await client.post("/api/nodes/register", json=evil, headers=with_key("x" * 43))
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.INVALID
    db_session.expire_all()
    assert (await db_session.get(StandaloneNode, "lab-1")).url == "http://10.0.0.5:18080"

    nic = {"name": "lab-1", "mac": "00:1a:2b:3c:4d:5e", "ip": "10.0.0.5", "prefix": 24}
    calls = [
        ("POST", "/api/nodes/heartbeat", {"name": "lab-1"}),
        ("POST", "/api/nodes/wake", nic),
        ("POST", "/api/nodes/models", {"name": "lab-1", "models_bytes": 5}),
        ("POST", "/api/nodes/lab-1/image", IMAGE),
        ("GET", "/api/nodes/lab-1/desired", None),
        ("POST", "/api/nodes/lab-1/metrics", SAMPLE),
        ("POST", "/api/nodes/lab-1/stop", None),
        ("DELETE", "/api/nodes/lab-1", None),
    ]
    for method, path, body in calls:
        for headers, code in ((NO_KEY, identity.REQUIRED), (with_key("y" * 43), identity.INVALID)):
            r = await client.request(method, path, json=body, headers=headers)
            assert r.status_code == 403 and r.json()["detail"]["code"] == code, (method, path, r.text)
    assert app.state.k8s.deleted_nodes == []
    db_session.expire_all()
    assert await db_session.get(StandaloneNode, "lab-1") is not None and await db_session.get(NodeIdentity, "lab-1")

    # the node itself, with its key
    for method, path, body in calls[:-2]:
        r = await client.request(method, path, json=body, headers=with_key(key))
        assert r.status_code in (200, 204), (method, path, r.text)
    assert (await client.delete("/api/nodes/lab-1", headers=with_key(key))).status_code == 204
    assert app.state.k8s.deleted_nodes == ["lab-1"]
    db_session.expire_all()
    assert await db_session.get(NodeIdentity, "lab-1") is None  # a node that comes back is a new one


async def test_a_name_the_portal_does_not_know_is_refused_without_a_key(client):
    r = await client.post("/api/nodes/models", json={"name": "ghost", "models_bytes": 1}, headers=NO_KEY)
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.UNKNOWN
    r = await client.delete("/api/nodes/ghost", headers=NO_KEY)
    assert r.status_code == 403
    r = await client.post("/api/nodes/heartbeat", json={"name": "ghost"}, headers=NO_KEY)
    assert r.status_code == 404  # the registrar registers on 404
    r = await client.post("/api/nodes/heartbeat", json={"name": "ghost"}, headers=with_key("z" * 43))
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.INVALID  # the node drops the key and registers


async def test_k3s_nodes_enroll_and_concurrent_enrolls_of_one_name_give_one_key(client, app, db_session):
    replies = await asyncio.gather(
        *(client.post("/api/nodes/enroll", json={"name": "pc01"}, headers=NO_KEY) for _ in range(4))
    )
    # one gets the name; the others are claims (task B2) or, in a race on the first insert, 409: the keeper tries again
    assert all(r.status_code in (200, 409) for r in replies)
    keys = [r.json()["node_key"] for r in replies if r.status_code == 200]
    taken = [
        k for k in keys
        if (await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 7}, headers=with_key(k))).status_code == 200
    ]
    assert len(taken) == 1


# ---- migration: nodes known before identities -------------------------------------------------------------------


async def legacy(db_session, name: str, ip: str | None) -> None:
    db_session.add(NodeIdentity(name=name, approved=True, approved_at=datetime.now(UTC), legacy=True, last_ip=ip))
    await db_session.commit()


async def test_a_legacy_node_without_a_key_works_from_its_own_address_and_gets_a_key_there(client, app, db_session):
    """The first registration after the upgrade from the address the portal last saw for the node gets a key and keeps
    the approval; until then an old image (no key) keeps working from that address."""
    await legacy(db_session, "pc01", "127.0.0.1")  # the address the test client calls from
    r = await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 1}, headers=NO_KEY)
    assert r.status_code == 200
    r = await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 1}, headers={**NO_KEY, **FROM_ELSEWHERE})
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.REQUIRED
    reply = (await client.post("/api/nodes/enroll", json={"name": "pc01"}, headers=NO_KEY)).json()
    assert reply["approved"] is True and identity.KEY_RE.fullmatch(reply["node_key"])
    # with a key issued the keyless way is closed, also from the node's own address
    r = await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 1}, headers=NO_KEY)
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.REQUIRED
    db_session.expire_all()
    row = await db_session.get(NodeIdentity, "pc01")
    assert row.legacy is False and row.approved is True


async def test_a_legacy_name_registered_from_elsewhere_is_a_claim_that_changes_nothing_until_approved(
    client, app, db_session
):
    """Fix round 1, Important 1: a registration of a known name without a key from an unknown address does not take the
    name nor make it wait: it is a claim. Its key only heartbeats (`approved: false`); it neither stops, deletes nor
    reports for the name; the name's record and approval stay. «Подтвердить» makes the claimant the node."""
    await legacy(db_session, "lab-1", "10.0.0.5")
    db_session.add(StandaloneNode(name="lab-1", url="http://10.0.0.5:18080", accel="cpu", last_seen_at=datetime.now(UTC)))
    db_session.add(NodeAssignment(node_name="lab-1", deployment_id=None, source="standalone", manual=False))
    await db_session.commit()
    app.state.k8s.add_node("lab-1", accel="cpu", internal_ip="10.0.0.5")
    evil = {**REG, "url": "http://10.66.66.66:8080"}
    reply = await register(client, body=evil, headers={**NO_KEY, **FROM_ELSEWHERE})
    claim = reply["node_key"]
    assert reply["approved"] is False
    db_session.expire_all()
    row = await db_session.get(NodeIdentity, "lab-1")
    assert (row.approved, row.legacy, row.key_hash) == (True, True, None)
    assert [c.ip for c in await identity.live_claims(db_session, "lab-1")] == ["10.66.66.66"]
    assert (await db_session.get(StandaloneNode, "lab-1")).url == "http://10.0.0.5:18080"
    # the node of before keeps working from its address
    r = await client.post("/api/nodes/models", json={"name": "lab-1", "models_bytes": 1}, headers={**NO_KEY, "X-Forwarded-For": "10.0.0.5"})
    assert r.status_code == 200
    # the claim: a heartbeat that gets nothing and touches nothing, everything else refused
    hb = await client.post("/api/nodes/heartbeat", json={"name": "lab-1", "applied_spec_hash": "x"}, headers=with_key(claim, **FROM_ELSEWHERE))
    assert hb.json() == {"ok": True, "heartbeat_interval": 15, "approved": False, "desired": None}
    db_session.expire_all()
    assert (await db_session.get(StandaloneNode, "lab-1")).applied_spec_hash is None
    for method, path, body in (
        ("DELETE", "/api/nodes/lab-1", None),
        ("POST", "/api/nodes/lab-1/stop", None),
        ("POST", "/api/nodes/models", {"name": "lab-1", "models_bytes": 9}),
        ("POST", "/api/nodes/lab-1/image", IMAGE),
    ):
        r = await client.request(method, path, json=body, headers=with_key(claim, **FROM_ELSEWHERE))
        assert r.status_code == 403 and r.json()["detail"]["code"] == identity.PENDING, (method, path)
    assert app.state.k8s.deleted_nodes == []
    # the admin sees the claim next to the name's known addresses and picks it: the claimant is the node, it registers
    # again with its own url
    h = await admin(client, app)
    nodes = {n["name"]: n for n in (await client.get("/api/admin/nodes", headers=h)).json()["nodes"]}
    (c,) = nodes["lab-1"]["claims"]
    assert (c["ip"], c["known"], nodes["lab-1"]["legacy"], nodes["lab-1"]["approved"]) == ("10.66.66.66", False, True, True)
    assert set(nodes["lab-1"]["known_addresses"]) == {"10.0.0.5"}
    assert (await client.post("/api/admin/nodes/lab-1/approve", headers=h)).status_code == 409  # pick a claim
    r = await client.post("/api/admin/nodes/lab-1/approve", json={"claim_id": c["id"]}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=with_key(claim, **FROM_ELSEWHERE))
    assert r.status_code == 404  # its record went with the approval: the registrar registers again
    again = await register(client, body=evil, headers=with_key(claim, **FROM_ELSEWHERE))
    assert again["approved"] is True and "node_key" not in again
    db_session.expire_all()
    assert (await db_session.get(StandaloneNode, "lab-1")).url == "http://10.66.66.66:8080"
    # without a key the name is closed now, also from the old address
    r = await client.post("/api/nodes/models", json={"name": "lab-1", "models_bytes": 1}, headers={**NO_KEY, "X-Forwarded-For": "10.0.0.5"})
    assert r.status_code == 403


async def test_stop_and_delete_need_the_key_of_an_approved_node(client, app, db_session):
    """Fix round 1, Important 1: a legacy call without a key (even from the known address) and a waiting node never stop or
    delete a node; the approved node with its own key does."""
    await legacy(db_session, "pc01", "127.0.0.1")
    app.state.k8s.add_node("pc01", accel="cpu", internal_ip="127.0.0.1")
    for method, path in (("POST", "/api/nodes/pc01/stop"), ("DELETE", "/api/nodes/pc01")):
        r = await client.request(method, path, headers=NO_KEY)
        assert r.status_code == 403 and r.json()["detail"]["code"] == identity.REQUIRED, path
    assert app.state.k8s.deleted_nodes == []
    await approval(app, True)
    key = (await client.post("/api/nodes/enroll", json={"name": "pc09"}, headers=NO_KEY)).json()["node_key"]  # waits
    app.state.k8s.add_node("pc09", accel="cpu")
    r = await client.delete("/api/nodes/pc09", headers=with_key(key))
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.PENDING
    h = await admin(client, app)
    assert (await client.post("/api/admin/nodes/pc09/approve", headers=h)).status_code == 200
    assert (await client.delete("/api/nodes/pc09", headers=with_key(key))).status_code == 204
    assert app.state.k8s.deleted_nodes == ["pc09"]


async def test_the_migration_window_closes(client, app, db_session):
    """Fix round 1, Important 2: past `node_key_migration_days` a legacy node without a key is refused, and its registration
    from its known address is a claim for the admin; 0 days closes the window at once."""
    from datetime import timedelta

    db_session.add(NodeIdentity(
        name="pc01", approved=True, legacy=True, last_ip="127.0.0.1", created_at=datetime.now(UTC) - timedelta(days=8),
    ))
    db_session.add(NodeIdentity(name="pc02", approved=True, legacy=True, last_ip="127.0.0.1"))
    await db_session.commit()
    body = {"models_bytes": 1}
    assert (await client.post("/api/nodes/models", json={"name": "pc01", **body}, headers=NO_KEY)).status_code == 403
    assert (await client.post("/api/nodes/models", json={"name": "pc02", **body}, headers=NO_KEY)).status_code == 200
    reply = (await client.post("/api/nodes/enroll", json={"name": "pc01"}, headers=NO_KEY)).json()
    assert reply["approved"] is False  # a claim, although from the known address
    async with app.state.sessionmaker() as db:
        await settings_store.set(db, identity.MIGRATION_SETTING, 0, None)
        await db.commit()
    assert (await client.post("/api/nodes/models", json={"name": "pc02", **body}, headers=NO_KEY)).status_code == 403


async def test_a_lost_reply_of_the_first_registration_does_not_lock_the_node(client, app, db_session):
    """Fix round 1, Important 3: the same registration again (the node's nonce), while the key it got was never used, gets a
    new key of the name; once the key was used, or with another nonce, it is a claim for the admin (task B2)."""
    nonce = {**NO_KEY, "X-Node-Enroll-Nonce": "n" * 24}
    first = (await client.post("/api/nodes/enroll", json={"name": "pc01"}, headers=nonce)).json()["node_key"]
    other = {**NO_KEY, "X-Node-Enroll-Nonce": "m" * 24}
    r = await client.post("/api/nodes/enroll", json={"name": "pc01"}, headers=other)
    assert r.status_code == 200 and len(await identity.live_claims(db_session, "pc01")) == 1
    second = await client.post("/api/nodes/enroll", json={"name": "pc01"}, headers=nonce)
    assert second.status_code == 200 and second.json()["node_key"] != first
    key = second.json()["node_key"]
    r = await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 1}, headers=with_key(first))
    assert r.status_code == 403  # the lost key is no key any more
    assert (await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 1}, headers=with_key(key))).status_code == 200
    late = await client.post("/api/nodes/enroll", json={"name": "pc01"}, headers=nonce)  # used now: not the name's key any more
    assert late.status_code == 200 and late.json()["approved"] is False
    r = await client.post("/api/nodes/models", json={"name": "pc01", "models_bytes": 1}, headers=with_key(late.json()["node_key"]))
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.PENDING


async def test_a_known_name_keeps_every_claim_and_the_admin_picks_one(client, app, db_session):
    """Fix round 2, New 1: claims on a working name are never first-wins: one per address (the same address replaces its
    own), up to MAX_CLAIMS, one more refused and audited; never approved in bulk; picking one rejects the others."""
    key = (await register(client))["node_key"]  # lab-1, approved, with its key; then a reset makes it keyless
    h = await admin(client, app)
    assert (await client.post("/api/admin/nodes/lab-1/reset-key", headers=h)).status_code == 200
    # approve after the reset, before it registered again: nothing to approve (the registration comes as a claim)
    assert (await client.post("/api/admin/nodes/lab-1/approve", headers=h)).status_code == 404
    keys = {}
    for ip in ("10.66.66.66", "10.0.0.5", "10.66.66.66"):  # the attacker twice: the same address replaces its claim
        reply = await register(client, headers={**NO_KEY, "X-Forwarded-For": ip})
        assert reply["approved"] is False
        keys[ip] = reply["node_key"]
    claims = await identity.live_claims(db_session, "lab-1")
    assert sorted(c.ip for c in claims) == ["10.0.0.5", "10.66.66.66"]
    # the cap: one more address is refused and written to the audit log
    for i in range(identity.MAX_CLAIMS - 2):
        await register(client, headers={**NO_KEY, "X-Forwarded-For": f"10.9.9.{i + 1}"})
    r = await client.post("/api/nodes/register", json=REG, headers={**NO_KEY, "X-Forwarded-For": "10.9.9.200"})
    assert r.status_code == 409
    refused = (await db_session.scalars(select(AuditLog).where(AuditLog.action == "node.claim_refused"))).all()
    assert [(a.target, a.ip) for a in refused] == [("lab-1", "10.9.9.200")]
    nodes = {n["name"]: n for n in (await client.get("/api/admin/nodes", headers=h)).json()["nodes"]}
    row = nodes["lab-1"]
    assert len(row["claims"]) == identity.MAX_CLAIMS and row["claims_refused"] == 1
    assert [c["known"] for c in row["claims"] if c["ip"] == "10.0.0.5"] == [True]  # the host of its url
    # never in bulk
    r = await client.post("/api/admin/nodes/approve", json={"names": ["lab-1"]}, headers=h)
    assert r.status_code == 409
    # the admin picks the claim from the known address; the others are rejected, their keys no longer work
    real = next(c["id"] for c in row["claims"] if c["ip"] == "10.0.0.5")
    assert (await client.post("/api/admin/nodes/lab-1/approve", json={"claim_id": real}, headers=h)).status_code == 200
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=with_key(keys["10.66.66.66"]))
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.INVALID
    assert await identity.live_claims(db_session, "lab-1") == []
    (row,) = (await db_session.scalars(select(AuditLog).where(AuditLog.action == "node.approve"))).all()
    assert row.details["claim_ip"] == "10.0.0.5" and "10.66.66.66" in row.details["rejected"]
    again = await register(client, headers=with_key(keys["10.0.0.5"], **{"X-Forwarded-For": "10.0.0.5"}))
    assert again["approved"] is True
    assert key != keys["10.0.0.5"]


async def test_claims_can_be_rejected_one_by_one_or_all(client, app, db_session):
    await register(client)
    h = await admin(client, app)
    await client.post("/api/admin/nodes/lab-1/reset-key", headers=h)
    for ip in ("10.1.1.1", "10.1.1.2"):
        await register(client, headers={**NO_KEY, "X-Forwarded-For": ip})
    first, second = await identity.live_claims(db_session, "lab-1")
    r = await client.delete(f"/api/admin/nodes/lab-1/claims?claim_id={first.id}", headers=h)
    assert r.json() == {"node": "lab-1", "rejected": 1}
    r = await client.delete("/api/admin/nodes/lab-1/claims", headers=h)
    assert r.json() == {"node": "lab-1", "rejected": 1}
    actions = (await db_session.scalars(select(AuditLog.action).where(AuditLog.action == "node.claim_reject"))).all()
    assert len(actions) == 2


async def test_approve_is_only_for_names_that_registered(client, app, db_session):
    h = await admin(client, app)
    r = await client.post("/api/admin/nodes/ghost/approve", headers=h)
    assert r.status_code == 404
    r = await client.post("/api/admin/nodes/approve", json={"names": ["ghost"]}, headers=h)
    assert r.status_code == 404
    # task B2: a Kubernetes node that never registered is no exception: an approval must not leave a row without a key
    await approval(app, True)
    app.state.k8s.add_node("lab-2", accel="cpu", internal_ip="10.0.0.7")
    r = await client.post("/api/admin/nodes/lab-2/approve", headers=h)
    assert r.status_code == 404 and "ещё не получила свой ключ" in r.json()["detail"]
    assert await db_session.get(NodeIdentity, "lab-2") is None
    # and its registration then is an ordinary waiting node with its key
    reply = (await client.post("/api/nodes/enroll", json={"name": "lab-2"}, headers=NO_KEY)).json()
    assert reply["approved"] is False and identity.KEY_RE.fullmatch(reply["node_key"])
    assert (await client.post("/api/admin/nodes/lab-2/approve", headers=h)).status_code == 200


@pytest.mark.parametrize(
    "legacy, approved, days, reset",
    [
        (False, True, 0, False),  # the live case of 2026-11-05: a row of an older portal's approval, window closed
        (False, True, 7, False),  # the same from an address the portal does not know
        (False, False, 7, True),  # after «Сбросить ключ»
        (True, True, 0, False),  # legacy, window closed
        (True, False, 7, False),  # legacy, not approved
    ],
)
async def test_a_known_name_without_a_key_always_ends_in_a_claim_the_admin_sees(
    client, app, db_session, legacy, approved, days, reset
):
    """Task B2, point 3: no combination leaves a node without a key and the admin without anything to click."""
    await approval(app, True)
    async with app.state.sessionmaker() as db:
        await settings_store.set(db, identity.MIGRATION_SETTING, days, None)
        await db.commit()
    app.state.k8s.add_node("lab-1", accel="cpu", internal_ip="10.0.0.5")
    db_session.add(NodeIdentity(name="lab-1", approved=approved, approved_at=datetime.now(UTC), legacy=legacy))
    await db_session.commit()
    h = await admin(client, app)
    if reset:
        assert (await client.post("/api/admin/nodes/lab-1/reset-key", headers=h)).status_code == 200
    r = await client.post("/api/nodes/enroll", json={"name": "lab-1"}, headers={**NO_KEY, **FROM_ELSEWHERE})
    assert r.status_code == 200 and r.json()["approved"] is False
    key = r.json()["node_key"]
    assert identity.KEY_RE.fullmatch(key)
    nodes = {n["name"]: n for n in (await client.get("/api/admin/nodes", headers=h)).json()["nodes"]}
    (claim,) = nodes["lab-1"]["claims"]
    r = await client.post("/api/admin/nodes/lab-1/approve", json={"claim_id": claim["id"]}, headers=h)
    assert r.status_code == 200
    r = await client.post("/api/nodes/lab-1/metrics", json=SAMPLE, headers=with_key(key))
    assert r.status_code == 204


async def test_the_messages_say_what_to_do(client, app):
    await register(client)
    r = await client.post("/api/nodes/models", json={"name": "lab-1", "models_bytes": 1}, headers=NO_KEY)
    assert r.status_code == 403 and "Сделать нодой" in r.json()["detail"]["message"]


async def test_a_reinstalled_pc_rejoins_under_its_name(client, app, db_session):
    """Task B2, point 6: install -> data loss (no uninstall: new k3s password, no node.key) -> reinstall under the same name ->
    enrol -> claim -> the admin picks it -> heartbeat 200, metrics taken; the old key is dead, the old k3s password is gone."""
    await approval(app, True)
    app.state.k8s.add_node("lab-1", accel="cpu", internal_ip="10.0.0.5", ready=False)
    h = await admin(client, app)
    old = (await register(client))["node_key"]
    assert (await client.post("/api/admin/nodes/lab-1/approve", headers=h)).status_code == 200
    # the reinstalled PC: no key, no nonce of before, the same address
    state = (await client.get("/api/nodes/lab-1/known", headers=NO_KEY)).json()
    assert state["known"] is True and state["own_key"] is False and "Сделать нодой" in state["message"]
    reply = await register(client)
    assert reply["approved"] is False
    key = reply["node_key"]
    state = (await client.get("/api/nodes/lab-1/known", headers=with_key(key))).json()
    assert state["own_claim"] is True and "Сделать нодой" in state["message"]
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=with_key(key))
    assert r.status_code == 200 and r.json()["approved"] is False and r.json()["desired"] is None
    r = await client.post("/api/nodes/lab-1/metrics", json=SAMPLE, headers=with_key(key))
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.PENDING
    nodes = {n["name"]: n for n in (await client.get("/api/admin/nodes", headers=h)).json()["nodes"]}
    (claim,) = nodes["lab-1"]["claims"]
    assert claim["known"] is True  # the same address as before
    assert app.state.k8s.deleted_node_passwords == []
    r = await client.post("/api/admin/nodes/lab-1/approve", json={"claim_id": claim["id"]}, headers=h)
    assert r.status_code == 200
    assert app.state.k8s.deleted_node_passwords == ["lab-1"]  # k3s lets the new agent in
    again = await register(client, headers=with_key(key))  # standalone: its record went with the approval
    assert again["approved"] is True and "node_key" not in again
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=with_key(key))
    assert r.status_code == 200 and r.json()["approved"] is True
    assert (await client.post("/api/nodes/lab-1/metrics", json=SAMPLE, headers=with_key(key))).status_code == 204
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=with_key(old))
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.INVALID


async def test_a_deleted_node_leaves_nothing_that_blocks_its_name(client, app, db_session):
    """Task B2, point 4: «Удалить из кластера» and the node's own DELETE take the k3s node password, the identity and the
    claims too; a reinstall is a new node."""
    await approval(app, True)
    h = await admin(client, app)
    for delete in ("admin", "node"):
        app.state.k8s.add_node("lab-1", accel="cpu", internal_ip="10.0.0.5")
        key = (await register(client))["node_key"]
        assert (await client.post("/api/admin/nodes/lab-1/approve", headers=h)).status_code == 200
        await register(client, headers={**NO_KEY, **FROM_ELSEWHERE})  # a claim on it
        app.state.k8s.deleted_node_passwords.clear()
        if delete == "admin":
            r = await client.delete("/api/admin/nodes/lab-1", headers=h)
            assert r.status_code == 200, r.text
        else:
            assert (await client.delete("/api/nodes/lab-1", headers=with_key(key))).status_code == 204
        assert app.state.k8s.deleted_node_passwords == ["lab-1"], delete
        db_session.expire_all()
        assert await db_session.get(NodeIdentity, "lab-1") is None
        assert await identity.live_claims(db_session, "lab-1") == []
        state = (await client.get("/api/nodes/lab-1/known", headers=NO_KEY)).json()
        assert state["known"] is False and "Подтвердить" in state["message"]


async def test_a_name_that_registered_but_is_not_in_the_cluster_has_a_row_on_the_nodes_page(client, app):
    """k3s refuses the agent or it has not joined yet: without this row the admin would have no «Подтвердить» and no claim."""
    await approval(app, True)
    h = await admin(client, app)
    await client.post("/api/nodes/enroll", json={"name": "lab-3"}, headers=NO_KEY)
    rows = {n["name"]: n for n in (await client.get("/api/admin/nodes?enrolled=1", headers=h)).json()["nodes"]}
    assert rows["lab-3"]["source"] == "enrolled" and rows["lab-3"]["approved"] is False and rows["lab-3"]["has_key"] is True
    plain = {n["name"] for n in (await client.get("/api/admin/nodes", headers=h)).json()["nodes"]}
    assert "lab-3" not in plain  # the pickers of deployments do not offer it
    assert (await client.post("/api/admin/nodes/lab-3/approve", headers=h)).status_code == 200
    assert (await client.delete("/api/admin/nodes/lab-3", headers=h)).status_code == 200


async def test_a_claim_does_not_stop_the_approval_of_a_waiting_node(client, app, db_session):
    """Review B-4: the token is on every PC, one POST from anywhere must not hold up a new node: «Подтвердить» approves the
    holder of the name's key, the claim stays for the admin and its key is still good for nothing but a heartbeat."""
    await approval(app, True)
    h = await admin(client, app)
    key = (await client.post("/api/nodes/enroll", json={"name": "lab-4"}, headers=NO_KEY)).json()["node_key"]
    claim = await client.post("/api/nodes/enroll", json={"name": "lab-4"}, headers={**NO_KEY, **FROM_ELSEWHERE})
    assert claim.json()["approved"] is False
    assert (await client.post("/api/admin/nodes/lab-4/approve", headers=h)).status_code == 200
    r = await client.post("/api/nodes/lab-4/metrics", json=SAMPLE, headers=with_key(key))
    assert r.status_code == 204
    r = await client.post("/api/nodes/lab-4/metrics", json=SAMPLE, headers=with_key(claim.json()["node_key"]))
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.PENDING
    assert len(await identity.live_claims(db_session, "lab-4")) == 1


async def test_a_picked_claim_with_the_cluster_down_spends_nothing(client, app, db_session):
    """Review B-3: the secret goes before the commit: without it k3s would refuse the agent and the claim would be gone."""
    await approval(app, True)
    app.state.k8s.add_node("lab-1", accel="cpu", internal_ip="10.0.0.5")
    h = await admin(client, app)
    old = (await register(client))["node_key"]
    assert (await client.post("/api/admin/nodes/lab-1/approve", headers=h)).status_code == 200
    key = (await register(client))["node_key"]  # the reinstalled PC
    (claim,) = await identity.live_claims(db_session, "lab-1")
    claim_id = claim.id
    app.state.k8s.unavailable = True
    r = await client.post("/api/admin/nodes/lab-1/approve", json={"claim_id": claim_id}, headers=h)
    assert r.status_code == 503 and "Кластер не ответил" in r.json()["detail"]
    db_session.expire_all()
    assert [c.id for c in await identity.live_claims(db_session, "lab-1")] == [claim_id]
    r = await client.post("/api/nodes/models", json={"name": "lab-1", "models_bytes": 1}, headers=with_key(old))
    assert r.status_code == 200  # the name's key is still the old one
    app.state.k8s.unavailable = False
    assert (await client.post("/api/admin/nodes/lab-1/approve", json={"claim_id": claim_id}, headers=h)).status_code == 200
    assert app.state.k8s.deleted_node_passwords == ["lab-1"]
    assert (await client.post("/api/nodes/lab-1/metrics", json=SAMPLE, headers=with_key(key))).status_code == 204


async def test_a_deletion_with_the_cluster_down_says_the_password_stayed_and_approve_clears_it(client, app, db_session):
    """Review B-3: no report of success, and no kubectl: the name comes back as a new waiting node, its «Подтвердить» clears it."""
    await approval(app, True)
    h = await admin(client, app)
    app.state.k8s.add_node("lab-1", accel="cpu", internal_ip="10.0.0.5")
    await register(client)
    assert (await client.post("/api/admin/nodes/lab-1/approve", headers=h)).status_code == 200
    app.state.k8s.unavailable = True
    r = await client.delete("/api/admin/nodes/lab-1", headers=h)
    assert r.status_code == 200 and r.json()["k8s"] == "unavailable" and r.json()["k3s_password"] == "left"
    (row,) = (await db_session.scalars(select(AuditLog).where(AuditLog.action == "node.delete"))).all()
    assert row.details["k3s_cleanup"] == "left"
    app.state.k8s.unavailable = False
    app.state.k8s.nodes.pop("lab-1")  # the Node went while the portal could not see it: the PC is being reinstalled
    await client.post("/api/nodes/enroll", json={"name": "lab-1"}, headers=NO_KEY)
    assert (await client.post("/api/admin/nodes/lab-1/approve", headers=h)).status_code == 200
    assert app.state.k8s.deleted_node_passwords == ["lab-1"]


async def test_only_the_password_of_a_call_that_failed_is_reported(client, app, monkeypatch):
    """Review B-3: the Node went, the secret did not: `left`, not a plain success."""
    h = await admin(client, app)
    app.state.k8s.add_node("lab-1", accel="cpu", internal_ip="10.0.0.5")
    await register(client)

    async def broken(name):
        raise K8sUnavailable("timeout")

    monkeypatch.setattr(app.state.k8s, "delete_node_password", broken)
    r = await client.delete("/api/admin/nodes/lab-1", headers=h)
    assert r.status_code == 200 and r.json()["k8s"] == "deleted" and r.json()["k3s_password"] == "left"


async def test_approve_leaves_the_password_of_a_node_in_the_cluster_alone_and_a_standalone_node_needs_no_cluster(client, app):
    await approval(app, True)
    h = await admin(client, app)
    app.state.k8s.add_node("lab-2", accel="cpu", internal_ip="10.0.0.7")
    await client.post("/api/nodes/enroll", json={"name": "lab-2"}, headers=NO_KEY)
    assert (await client.post("/api/admin/nodes/lab-2/approve", headers=h)).status_code == 200
    assert app.state.k8s.deleted_node_passwords == []
    await register(client)  # standalone lab-1, waiting
    app.state.k8s.unavailable = True
    assert (await client.post("/api/admin/nodes/lab-1/approve", headers=h)).status_code == 200


async def test_known_needs_the_agent_token_and_a_name(client, app):
    assert (await client.get("/api/nodes/lab-1/known")).status_code == 401
    assert (await client.get("/api/nodes/Bad_Name/known", headers=NO_KEY)).status_code == 422


async def test_the_known_address_of_a_legacy_standalone_node_is_the_host_of_its_url(client, app, db_session):
    await legacy(db_session, "lab-1", None)
    db_session.add(StandaloneNode(name="lab-1", url="http://10.0.0.5:18080", accel="cpu", last_seen_at=datetime.now(UTC)))
    await db_session.commit()
    reply = await register(client, headers={**NO_KEY, "X-Forwarded-For": "10.0.0.5"})
    assert reply["approved"] is True


async def test_migration_0011_makes_every_known_node_an_approved_legacy_identity(pg_url):
    from alembic import command
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app import db

    cfg = db._alembic_config(pg_url)
    engine = create_async_engine(pg_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("drop schema public cascade"))
            await conn.execute(text("create schema public"))
        await asyncio.to_thread(command.upgrade, cfg, "0010")
        async with engine.begin() as conn:
            await conn.execute(text(
                "insert into standalone_nodes (name, url, accel, version) values ('win-07', 'http://192.168.3.77:18080', 'cpu', '')"
            ))
            await conn.execute(text(
                "insert into node_assignments (node_name, source, manual, maintenance)"
                " values ('pc01', 'k8s', false, false), ('llm-main', 'k8s', false, false)"
            ))
            await conn.execute(text("insert into node_wake (node_name, mac, ip, prefix) values ('pc01', 'aa:bb:cc:dd:ee:ff', '192.168.3.41', 24)"))
        await asyncio.to_thread(command.upgrade, cfg, "0011")
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "select name, approved, legacy, last_ip, key_hash from node_identities order by name"
            ))).all()
        assert [tuple(r) for r in rows] == [
            ("pc01", True, True, "192.168.3.41", None),
            ("win-07", True, True, "192.168.3.77", None),
        ]
        await asyncio.to_thread(command.downgrade, cfg, "0010")
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await engine.dispose()


# ---- rule 2: new nodes need approval -----------------------------------------------------------------------------


async def test_approval_is_required_by_default():
    from app.config import Settings

    assert Settings().node_approval_required is True
    assert settings_store.SETTINGS[identity.APPROVAL_SETTING].default(Settings()) is True


async def test_a_new_node_waits_for_approval_without_deployment_backend_or_keys(client, app, db_session, monkeypatch):
    await approval(app, True)
    monkeypatch.setattr(app.state.settings, "backend_api_key", SecretStr("shared-key"))
    monkeypatch.setattr(app.state.settings, "hf_mirror_token", SecretStr("mirror-key"))
    reply = await register(client)
    key = reply["node_key"]
    assert reply["approved"] is False
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    dep.auto_assign = True
    await db_session.commit()
    await service.assign(db_session, "lab-1", dep.id)  # even an admin's assignment does not reach it before approval
    dep_id, dep_hash = dep.id, service.spec_hash(dep)
    hb = (await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=with_key(key))).json()
    assert hb == {"ok": True, "heartbeat_interval": 15, "approved": False, "desired": None}
    assert "shared-key" not in str(hb) and "mirror-key" not in str(hb)

    # the controllers: no LiteLLM backend, no auto-assignment of a pending name
    await client.post("/api/nodes/register", json={**REG, "name": "lab-2", "url": "http://10.0.0.6:18080"}, headers=NO_KEY)
    await WorkloadController(app.state).reconcile_once()
    db_session.expire_all()
    assert (await db_session.get(NodeAssignment, "lab-2")).deployment_id is None
    async with app.state.sessionmaker() as db:
        await db.execute(StandaloneNode.__table__.update().values(applied_spec_hash=dep_hash))
        await db.commit()
    controller = BackendController(app.state)
    assert await controller._standalone_candidates() == []

    # approve one, then both at once: an audit row each
    h = await admin(client, app)
    r = await client.post("/api/admin/nodes/lab-1/approve", headers=h)
    assert r.status_code == 200 and r.json() == {"approved": ["lab-1"]}
    running = {"name": "lab-1", "applied_spec_hash": dep_hash}
    hb = (await client.post("/api/nodes/heartbeat", json=running, headers=with_key(key))).json()
    assert hb["approved"] is True and hb["desired"]["deployment"] == "gemma-4-e4b"
    assert hb["desired"]["backend_api_key"] == identity.backend_key_of(key)
    (cand,) = await controller._standalone_candidates()
    assert cand.node == "lab-1" and cand.api_key == identity.backend_key_of(key)
    r = await client.post("/api/admin/nodes/approve", json={"names": ["lab-1", "LAB-2"]}, headers=h)
    assert r.json() == {"approved": ["lab-1", "lab-2"]}
    await WorkloadController(app.state).reconcile_once()
    db_session.expire_all()
    assert (await db_session.get(NodeAssignment, "lab-2")).deployment_id == dep_id
    rows = (await db_session.scalars(select(AuditLog).where(AuditLog.action == "node.approve").order_by(AuditLog.id))).all()
    assert [(a.target, a.actor_email) for a in rows] == [("lab-1", "admin@g.nsu.ru")] * 2 + [("lab-2", "admin@g.nsu.ru")]

    nodes = {n["name"]: n for n in (await client.get("/api/admin/nodes", headers=h)).json()["nodes"]}
    assert (nodes["lab-1"]["approved"], nodes["lab-1"]["has_key"], nodes["lab-1"]["claim_ip"]) == (True, True, "127.0.0.1")


async def test_an_unapproved_cluster_node_gets_no_label_and_no_backend(client, app, db_session):
    await approval(app, True)
    k8s = app.state.k8s
    k8s.add_node("pc01", accel="cpu", internal_ip="10.0.0.41")
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    await service.assign(db_session, "pc01", dep.id)
    await WorkloadController(app.state).reconcile_once()
    assert next(n for n in await k8s.list_nodes() if n.name == "pc01").deployment is None
    # a row without a key that an older portal's approval left (today approve refuses a name that never registered)
    db_session.add(NodeIdentity(name="pc01", approved=True, approved_at=datetime.now(UTC), legacy=False))
    await db_session.commit()
    await WorkloadController(app.state).reconcile_once()
    assert next(n for n in await k8s.list_nodes() if n.name == "pc01").deployment == "gemma-4-e4b"
    # approved but without its own key (it never enrolled): its pods cannot have a key, LiteLLM gets no entry for it
    k8s.add_pod("p1", "pc01", "inference", "gemma-4-e4b", port=1, model_name="gemma-4-e4b", max_input_tokens=8192)
    assert await BackendController(app.state)._k8s_candidates() == []


async def test_reset_key_takes_the_key_and_the_approval_away(client, app, db_session):
    key = (await register(client))["node_key"]
    await approval(app, True)
    h = await admin(client, app)
    r = await client.post("/api/admin/nodes/lab-1/reset-key", headers=h)
    assert r.status_code == 200 and r.json() == {"node": "lab-1", "approved": False}
    assert (await client.post("/api/admin/nodes/ghost/reset-key", headers=h)).status_code == 404
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=with_key(key))
    assert r.status_code == 403 and r.json()["detail"]["code"] == identity.INVALID
    again = await register(client)  # what registrar.sh does next: it drops the key and registers
    assert again["approved"] is False and again["node_key"] != key  # a claim for the admin
    r = await client.post("/api/nodes/heartbeat", json={"name": "lab-1"}, headers=with_key(again["node_key"]))
    assert r.json()["approved"] is False
    (claim,) = await identity.live_claims(db_session, "lab-1")
    r = await client.post("/api/admin/nodes/lab-1/approve", json={"claim_id": claim.id}, headers=h)
    assert r.status_code == 200
    actions = (await db_session.scalars(select(AuditLog.action).where(AuditLog.target == "lab-1"))).all()
    assert "node.reset_key" in actions
    assert (await client.post("/api/admin/nodes/llm-main/approve", headers=h)).status_code == 409


async def test_approval_endpoints_are_admin_only_with_csrf(client, app):
    assert (await client.post("/api/admin/nodes/lab-1/approve")).status_code == 401
    await login(client, app, ADMIN)
    assert (await client.post("/api/admin/nodes/lab-1/approve")).status_code == 403  # no CSRF token


# ---- rule 4: the mirror token ---------------------------------------------------------------------------------


def test_the_mirror_token_is_a_signed_list_of_repositories():
    import base64
    import hmac
    import json

    token = mirror_tokens.mint("secret", "lab-1", {"org/b", "org/a", ""})
    prefix, payload, sig = token.split(".")
    want = hmac.new(b"secret", f"{prefix}.{payload}".encode(), hashlib.sha256).digest()
    assert prefix == "nsm1" and base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)) == want
    assert json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))) == {"c": "lab-1", "r": ["org/a", "org/b"]}
    assert mirror_tokens.mint("", "lab-1", {"org/a"}) == ""
    assert mirror_tokens.repos_of("org/m", "org/d", "local/own/mm.gguf") == {"org/m", "org/d", "local/own"}
    assert mirror_tokens.repos_of("org/m", None, "mmproj.gguf") == {"org/m"}
    # a test vector for main/hf_mirror.py (sec-infra-fix)
    assert mirror_tokens.mint("k", "*", ["a/b"]) == "nsm1.eyJjIjoiKiIsInIiOlsiYS9iIl19." + base64.urlsafe_b64encode(
        hmac.new(b"k", b"nsm1.eyJjIjoiKiIsInIiOlsiYS9iIl19", hashlib.sha256).digest()
    ).rstrip(b"=").decode()


# ---- L5: a node url by a name that resolves to the portal ---------------------------------------------------------


async def test_a_node_url_by_a_name_that_resolves_to_loopback_is_refused(client, db_session, monkeypatch):
    async def fake_lookup(host):
        return {
            "localtest.me": ["127.0.0.1"], "v6.example": ["::1"], "mapped.example": ["::ffff:127.0.0.1"],
            "lab.example": ["10.0.0.9"],
        }.get(host, [])

    monkeypatch.setattr(routes, "_lookup", fake_lookup)
    for host in ("localtest.me", "v6.example", "mapped.example"):
        r = await client.post("/api/nodes/register", json={**REG, "url": f"http://{host}:8000"}, headers=NO_KEY)
        assert r.status_code == 422, host
    assert await db_session.get(StandaloneNode, "lab-1") is None
    for host in ("lab.example", "unresolvable.example"):
        r = await client.post("/api/nodes/register", json={**REG, "url": f"http://{host}:8000"}, headers=NO_KEY)
        assert r.status_code in (200, 409), host


async def test_the_spa_does_not_answer_health(client):
    """The backend probe asks GET /health: the portal's own address must not pass it (L5)."""
    assert (await client.get("/health")).status_code == 404
    assert (await client.get("/somewhere")).status_code == 200  # the SPA still serves its routes


# ---- the portal's own key to the mirror (SEC-2 of main: HF_MIRROR_ADMIN_TOKEN) -------------------------------------


def test_the_portal_talks_to_the_mirror_with_its_admin_key_and_never_hands_it_out():
    from app.catalog.mirror_admin import MirrorAdmin
    from app.config import Settings

    s = Settings(hf_mirror_token="node-side", hf_mirror_admin_token="admin-side")
    assert MirrorAdmin.from_settings(s).token == "admin-side" and s.mirror_admin_key == "admin-side"
    assert Settings(hf_mirror_token="node-side").mirror_admin_key == "node-side"  # an older main without the admin key
    token = mirror_tokens.mint(s.hf_mirror_token.get_secret_value(), "lab-1", {"o/r"})
    assert "admin-side" not in token and "node-side" not in token
