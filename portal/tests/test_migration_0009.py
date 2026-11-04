"""Migration 0009 (users: role source, blocking, own limits; audit log, runtime settings, monitoring samples)."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app import db

pytestmark = pytest.mark.docker

USER_FIELDS = ("role_source", "blocked", "blocked_reason", "rpm_limit", "tpm_limit", "max_keys")
NEW_TABLES = {"audit_log", "system_settings", "metric_samples"}


async def columns(conn, table: str) -> dict[str, str]:
    rows = await conn.execute(
        text("select column_name, is_nullable from information_schema.columns where table_name = :t"), {"t": table}
    )
    return {r[0]: r[1] for r in rows}


async def test_migration_0009_keeps_the_users_on_auto_and_downgrades(pg_url):
    cfg = db._alembic_config(pg_url)
    engine = create_async_engine(pg_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("drop schema public cascade"))
            await conn.execute(text("create schema public"))
        await asyncio.to_thread(command.upgrade, cfg, "0008")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into users (id, sub, email, username, full_name, role)"
                    " values (gen_random_uuid(), 's1', 'a@g.nsu.ru', 'a', 'A', 'admin'),"
                    " (gen_random_uuid(), 's2', 'b@g.nsu.ru', 'b', 'B', 'user')"
                )
            )
        await asyncio.to_thread(command.upgrade, cfg, "0009")

        async with engine.connect() as conn:
            rows = (
                await conn.execute(text(f"select role, {', '.join(USER_FIELDS)} from users order by sub"))
            ).all()
            # today's behaviour for everyone who exists: the role follows ADMIN_EMAILS, nobody is blocked
            assert [tuple(r) for r in rows] == [
                ("admin", "auto", False, None, None, None, None),
                ("user", "auto", False, None, None, None, None),
            ]
            cols = await columns(conn, "users")
            assert {f: cols[f] for f in USER_FIELDS} == {
                "role_source": "NO", "blocked": "NO", "blocked_reason": "YES",
                "rpm_limit": "YES", "tpm_limit": "YES", "max_keys": "YES",
            }
            tables = {r[0] for r in await conn.execute(text("select tablename from pg_tables where schemaname = 'public'"))}
            assert NEW_TABLES <= tables
            assert (await columns(conn, "audit_log")) == {
                "id": "NO", "at": "NO", "actor_id": "YES", "actor_email": "YES", "action": "NO",
                "target_type": "NO", "target": "NO", "details": "YES", "ip": "YES",
            }
            assert (await columns(conn, "system_settings")) == {
                "key": "NO", "value": "NO", "updated_at": "NO", "updated_by": "YES",
            }
            assert (await columns(conn, "metric_samples")) == {
                "id": "NO", "at": "NO", "kind": "NO", "name": "NO", "data": "NO",
            }
            indexes = {
                r[0]
                for r in await conn.execute(
                    text("select indexname from pg_indexes where tablename in ('audit_log', 'metric_samples')")
                )
            }
            assert {"ix_audit_log_at", "ix_audit_log_action", "ix_metric_samples_kind_name_at"} <= indexes

        # the checks of the new columns
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(text("update users set role_source = 'robot' where sub = 's1'"))
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text("insert into metric_samples (at, kind, name, data) values (now(), 'pod', 'x', '{}')")
                )

        # an audit row outlives its actor: the user is deleted, the email stays
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into audit_log (actor_id, actor_email, action, target_type, target)"
                    " select id, email, 'user.role', 'user', 'b@g.nsu.ru' from users where sub = 's1'"
                )
            )
            await conn.execute(text("delete from users where sub = 's1'"))
        async with engine.connect() as conn:
            row = (await conn.execute(text("select actor_id, actor_email, at is not null from audit_log"))).one()
            assert tuple(row) == (None, "a@g.nsu.ru", True)

        await asyncio.to_thread(command.downgrade, cfg, "0008")
        async with engine.connect() as conn:
            assert not set(await columns(conn, "users")) & set(USER_FIELDS)
            tables = {r[0] for r in await conn.execute(text("select tablename from pg_tables where schemaname = 'public'"))}
            assert not NEW_TABLES & tables
            assert (await conn.execute(text("select count(*) from users"))).scalar() == 1  # the user table survives
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await engine.dispose()
