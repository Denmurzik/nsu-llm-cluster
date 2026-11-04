"""app.audit: scrub of secrets and the record() contract (adds a row, never commits)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from starlette.requests import Request

from app import audit
from app.models import AuditLog, User

# ---- scrub (no database) -------------------------------------------------------------------------------


def test_scrub_drops_keys_with_a_secret_word_in_the_name():
    details = {
        "repo": "o/r",
        "api_key": "sk-1",
        "KEY": "sk-2",
        "hf_token": "hf_x",
        "Password": "p",
        "client_secret": "s",
        "private-key": "p",
        "Authorization": "Bearer x",
        "key_value": "v",
        "ok": 1,
    }
    assert audit.scrub(details) == {"repo": "o/r", "ok": 1}


def test_scrub_keeps_names_that_only_look_like_secrets():
    """max_keys, rpm_limit and tpm_limit are the per-user limits of task 2-32: they must reach the log."""
    details = {
        "max_keys": 3, "rpm_limit": 10, "tpm_limit": 1000, "keys_limit": 2, "key_id": "abc", "keyboard": "k",
        "max_tokens": 256, "tokens_30d": 5, "monkey": 1, "key_rpm_limit": 60,
    }
    assert audit.scrub(details) == details


def test_scrub_works_through_nested_dicts_and_lists():
    details = {
        "changes": {"ctx": 4096, "extra": {"token": "t", "mode": "split"}},
        "items": [{"name": "a", "secret": "x"}, "plain", 3, None],
    }
    assert audit.scrub(details) == {
        "changes": {"ctx": 4096, "extra": {"mode": "split"}},
        "items": [{"name": "a"}, "plain", 3, None],
    }


def test_scrub_makes_values_json_safe_and_cuts_long_text():
    uid = uuid.uuid4()
    at = datetime(2026, 11, 4, tzinfo=UTC)
    out = audit.scrub({"id": uid, "at": at, "nodes": {"b", "a"}, "pair": ("x", 1), "text": "я" * 2000, "flag": True})
    assert out["id"] == str(uid) and out["at"] == str(at)
    assert out["nodes"] == ["a", "b"] and out["pair"] == ["x", 1] and out["flag"] is True
    assert len(out["text"]) == audit.MAX_STR and out["text"].endswith("…")


def test_scrub_of_nothing_is_nothing():
    assert audit.scrub(None) is None
    assert audit.scrub({}) == {}


def make_request(headers: dict[str, str], peer: str | None) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 1234) if peer else None,
    }
    return Request(scope)


@pytest.mark.parametrize(
    ("headers", "peer", "expected"),
    [
        ({}, "10.1.2.3", "10.1.2.3"),
        ({}, None, None),
        # behind Caddy on the same host the client is the first entry of X-Forwarded-For
        ({"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}, "127.0.0.1", "203.0.113.7"),
        ({"X-Forwarded-For": "203.0.113.7"}, "::1", "203.0.113.7"),
        ({}, "127.0.0.1", "127.0.0.1"),
        # any other peer could forge the header
        ({"X-Forwarded-For": "1.2.3.4"}, "192.168.3.50", "192.168.3.50"),
    ],
)
def test_client_ip(headers, peer, expected):
    assert audit.client_ip(make_request(headers, peer)) == expected


# ---- record (database) ---------------------------------------------------------------------------------


async def actor(db, email="admin@g.nsu.ru") -> User:
    user = User(sub=f"sub-{email}", email=email, role="admin")
    db.add(user)
    await db.commit()
    return user


@pytest.mark.docker
async def test_record_adds_a_row_and_leaves_the_commit_to_the_caller(app, db_session):
    admin = await actor(db_session)
    await audit.record(
        db_session, admin, "deployment.create", "deployment", "m1",
        {"repo": "o/r", "api_key": "sk-secret"}, "10.0.0.5",
    )
    async with app.state.sessionmaker() as other:
        assert (await other.scalars(select(AuditLog))).all() == []  # nothing committed yet
    await db_session.commit()
    async with app.state.sessionmaker() as other:
        (row,) = (await other.scalars(select(AuditLog))).all()
    assert (row.actor_id, row.actor_email) == (admin.id, "admin@g.nsu.ru")
    assert (row.action, row.target_type, row.target, row.ip) == ("deployment.create", "deployment", "m1", "10.0.0.5")
    assert row.details == {"repo": "o/r"}  # the secret never reaches the table
    assert abs((datetime.now(UTC) - row.at).total_seconds()) < 60 and row.id > 0


@pytest.mark.docker
async def test_a_rolled_back_change_leaves_no_audit_row(app, db_session):
    admin = await actor(db_session)
    await audit.record(db_session, admin, "user.block", "user", "u@g.nsu.ru")
    await db_session.rollback()
    assert (await db_session.scalars(select(AuditLog))).all() == []


@pytest.mark.docker
async def test_record_without_an_actor_is_a_system_action(app, db_session):
    await audit.record(db_session, None, "backup.run", "backup", "nightly")
    await db_session.commit()
    (row,) = (await db_session.scalars(select(AuditLog))).all()
    assert (row.actor_id, row.actor_email, row.details, row.ip) == (None, None, None, None)


@pytest.mark.docker
async def test_an_overlong_value_is_cut_not_refused(app, db_session):
    """The audit row shares the transaction of the change: a long target must not fail the change itself."""
    admin = await actor(db_session)
    await audit.record(db_session, admin, "x" * 100, "t" * 100, "n" * 1000, ip="9" * 100)
    await db_session.commit()
    (row,) = (await db_session.scalars(select(AuditLog))).all()
    assert (len(row.action), len(row.target_type), len(row.target), len(row.ip)) == (64, 32, 255, 64)


@pytest.mark.docker
async def test_the_row_outlives_its_actor(app, db_session):
    admin = await actor(db_session)
    await audit.record(db_session, admin, "user.role", "user", "x@g.nsu.ru")
    await db_session.commit()
    await db_session.delete(admin)
    await db_session.commit()
    db_session.expire_all()
    (row,) = (await db_session.scalars(select(AuditLog))).all()
    assert (row.actor_id, row.actor_email) == (None, "admin@g.nsu.ru")
