import asyncio

import pytest
from sqlalchemy import inspect, text

from app.db import make_engine, startup_lock

from app.models import ModelDeployment, NodeAssignment

pytestmark = pytest.mark.docker


async def test_migrations_create_tables(db_session):
    conn = await db_session.connection()
    names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    assert {
        "users",
        "web_sessions",
        "oauth_states",
        "api_keys",
        "model_deployments",
        "node_assignments",
        "standalone_nodes",
        "portal_flags",
        "audit_log",
        "system_settings",
        "metric_samples",
        "alembic_version",
    } <= names


async def test_assignment_fk_set_null_on_deployment_delete(db_session):
    dep = ModelDeployment(
        name="m1", engine="llamacpp", repo="r/x", file="x.gguf", mode="replicas", group_size=2,
        accel="any", ctx_per_request=8192, parallel=2, auto_assign=True, enabled=True,
    )
    db_session.add(dep)
    await db_session.flush()
    db_session.add(NodeAssignment(node_name="n1", deployment_id=dep.id, source="k8s"))
    await db_session.commit()

    await db_session.execute(text("delete from model_deployments where id = :i"), {"i": dep.id})
    await db_session.commit()

    row = (await db_session.execute(text("select deployment_id, manual from node_assignments where node_name = 'n1'"))).one()
    assert row.deployment_id is None
    assert row.manual is False


async def test_models_match_migration(db_session):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db import Base

    conn = await db_session.connection()
    diff = await conn.run_sync(lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata))
    assert diff == []


async def test_engine_pool_follows_settings(app, pg_url):
    assert app.state.engine.pool.size() == app.state.settings.db_pool_size == 10
    assert app.state.engine.pool._max_overflow == app.state.settings.db_max_overflow == 20
    engine = make_engine(pg_url, pool_size=3, max_overflow=7)
    try:
        assert engine.pool.size() == 3 and engine.pool._max_overflow == 7
    finally:
        await engine.dispose()


async def test_startup_lock_serializes_processes(pg_url):
    """API workers and the controllers process migrate and seed one after another, never at once."""
    engines = [make_engine(pg_url), make_engine(pg_url)]
    order = []

    async def worker(i):
        async with startup_lock(engines[i]):
            order.append(("in", i))
            await asyncio.sleep(0.3)
            order.append(("out", i))

    try:
        await asyncio.gather(worker(0), worker(1))
    finally:
        for e in engines:
            await e.dispose()
    assert [kind for kind, _ in order] == ["in", "out", "in", "out"]
