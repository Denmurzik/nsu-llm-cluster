"""Migration 0008 (llama.cpp settings of a deployment): rows made before keep the old behaviour; downgrade drops the columns."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import db

pytestmark = pytest.mark.docker

NEW_FIELDS = (
    "reasoning", "kv_cache_type", "sampling", "draft_repo", "draft_file", "draft_revision", "mmproj_file",
    "extra_args", "system_prompt", "experimental",
)



async def test_migration_0008_keeps_the_rows_on_old_behaviour_and_downgrades(pg_url):
    cfg = db._alembic_config(pg_url)
    engine = create_async_engine(pg_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("drop schema public cascade"))
            await conn.execute(text("create schema public"))
        await asyncio.to_thread(command.upgrade, cfg, "0007")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "insert into model_deployments (name, engine, repo, file, mode, group_size, accel,"
                    " ctx_per_request, parallel, auto_assign, enabled)"
                    " values ('m1', 'llamacpp', 'o/r', 'f.gguf', 'replicas', 2, 'any', 8192, 2, true, true)"
                )
            )
        await asyncio.to_thread(command.upgrade, cfg, "0008")
        async with engine.connect() as conn:
            row = (
                await conn.execute(text(f"select {', '.join(NEW_FIELDS)} from model_deployments where name = 'm1'"))
            ).one()
            assert tuple(row) == ("off", "f16", None, None, None, None, None, None, None, False)
        await asyncio.to_thread(command.downgrade, cfg, "0007")
        async with engine.connect() as conn:
            cols = {
                r[0]
                for r in await conn.execute(
                    text("select column_name from information_schema.columns where table_name = 'model_deployments'")
                )
            }
            assert not cols & set(NEW_FIELDS) and "revision" in cols
            assert (await conn.execute(text("select count(*) from model_deployments"))).scalar() == 1
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        await engine.dispose()
