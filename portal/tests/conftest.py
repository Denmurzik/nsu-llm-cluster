import tempfile
from pathlib import Path

import fakeredis
import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from app.config import Settings
from app.main import create_app


@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:17-alpine", driver="asyncpg") as pg:
        # "localhost" costs ~21 s per connection on Windows (IPv6 fallback); use the IPv4 address
        yield pg.get_connection_url().replace("@localhost:", "@127.0.0.1:")


@pytest.fixture
def settings(pg_url):
    web = Path(tempfile.mkdtemp(prefix="web-dist-"))
    (web / "index.html").write_text("<html><body><!--spa-marker--></body></html>", encoding="utf-8")
    return Settings(
        database_url=pg_url,
        controllers_enabled=False,
        cookie_secure=False,
        litellm_master_key="sk-test",
        cluster_token="tok-test",
        agent_token="agent-test",
        oidc_issuer="http://fake-oidc",
        oidc_client_secret="secret",
        admin_emails="admin@g.nsu.ru",
        main_advertise_ip="192.168.3.21",
        web_dist=str(web),
        hf_endpoint="http://127.0.0.1:9",  # discard port: tests never reach huggingface.co
        hf_mirror_port=9,  # nor a mirror: the inspector and the progress report go there
        hf_mirror_data_dir=tempfile.mkdtemp(prefix="hf-data-"),  # own model files: never /var/lib/llm-hf
        status_cache_ttl_s=0,  # tests flip the gateway state between calls; test_portal_hardening covers the cache
    )


@pytest_asyncio.fixture
async def app(settings):
    application = create_app(settings)
    # each app gets its own in-memory Redis (the lifespan keeps a client that is already set)
    application.state.redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    async with LifespanManager(application):
        yield application
    # truncate every table except alembic_version between tests
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text("select tablename from pg_tables where schemaname = 'public' and tablename <> 'alembic_version'")
            )
            names = [r[0] for r in rows]
            if names:
                await conn.execute(text("truncate " + ", ".join(f'"{n}"' for n in names) + " restart identity cascade"))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def db_session(app):
    async with app.state.sessionmaker() as session:
        yield session


def pytest_configure(config):
    config.addinivalue_line("markers", "network: test needs internet access (huggingface.co)")
