"""Migration 0003 on a database that already holds data: the rows stay, the new columns take their defaults."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import db

pytestmark = pytest.mark.docker


async def test_upgrade_from_0002_keeps_the_rows_and_fills_the_new_columns_with_defaults(pg_url):
    cfg = db._alembic_config(pg_url)
    engine = create_async_engine(pg_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("drop schema public cascade"))
            await conn.execute(text("create schema public"))
        await asyncio.to_thread(command.upgrade, cfg, "0002")
        async with engine.begin() as conn:
            dep_id = (
                await conn.execute(
                    text(
                        "insert into model_deployments (name, engine, repo, file, mode, group_size, accel,"
                        " ctx_per_request, parallel, auto_assign, enabled)"
                        " values ('m1', 'llamacpp', 'o/r', 'f.gguf', 'split', 3, 'cpu', 8192, 2, true, true)"
                        " returning id"
                    )
                )
            ).scalar()
            await conn.execute(
                text("insert into node_assignments (node_name, deployment_id, source, manual) values ('pc01', :d, 'k8s', true)"),
                {"d": dep_id},
            )

        await asyncio.to_thread(command.upgrade, cfg, "0003")

        async with engine.connect() as conn:
            dep = (
                await conn.execute(
                    text("select name, group_size, spare_nodes, regroup_grace_s, schedule from model_deployments")
                )
            ).one()
            assert tuple(dep) == ("m1", 3, 0, None, None)  # spare 0, the mode's grace, around the clock
            row = (await conn.execute(text("select node_name, deployment_id, manual, maintenance from node_assignments"))).one()
            assert tuple(row) == ("pc01", dep_id, True, False)
            assert (await conn.execute(text("select count(*) from controller_timers"))).scalar() == 0
    finally:
        await asyncio.to_thread(command.upgrade, cfg, "head")  # the shared test database goes back to head
        await engine.dispose()
