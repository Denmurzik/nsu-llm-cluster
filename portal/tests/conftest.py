import json
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
        oidc_client_id="nsu-llm-test",  # no provider defaults in the code: the tests name their own
        oidc_client_secret="secret",
        admin_emails="admin@g.nsu.ru",
        main_advertise_ip="192.168.3.21",
        web_dist=str(web),
        hf_endpoint="http://127.0.0.1:9",  # discard port: tests never reach huggingface.co
        hf_mirror_port=9,  # nor a mirror: the inspector and the progress report go there
        hf_mirror_data_dir=tempfile.mkdtemp(prefix="hf-data-"),  # own model files: never /var/lib/llm-hf
        settings_cache_ttl_s=0,  # tests change a setting and expect the next request to see it
        status_cache_ttl_s=0,  # tests flip the gateway state between calls; test_portal_hardening covers the cache
        # a new node serves at once, as before task SEC-1; tests/test_node_identity.py turns the approval on
        node_approval_required=False,
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


_NODE_BODY_PATHS = {"/api/nodes/register", "/api/nodes/enroll", "/api/nodes/heartbeat", "/api/nodes/wake", "/api/nodes/models"}


class NodeKeys:
    """Plays registrar.sh for the tests: keeps the key the portal issues a node (the reply of register/enroll) and sends
    it as X-Node-Key with that node's later calls (app.nodes.identity). A node call with a bearer token for a name that
    has no key yet enrolls the name first, as the node container does at its start (registrar.sh --report). A request
    that names X-Node-Key itself (also empty: no key) is left as it is, so tests/test_node_identity.py controls the
    header."""

    def __init__(self):
        self.keys: dict[str, str] = {}
        self.transport: httpx.AsyncBaseTransport | None = None

    async def _enroll(self, name: str, authorization: str) -> None:
        async with httpx.AsyncClient(transport=self.transport, base_url="https://testserver") as c:
            r = await c.post("/api/nodes/enroll", json={"name": name}, headers={"Authorization": authorization})
        if r.status_code == 200 and r.json().get("node_key"):
            self.keys[name] = r.json()["node_key"]

    @staticmethod
    def _name(request: httpx.Request) -> str | None:
        path = request.url.path
        if path in _NODE_BODY_PATHS:
            try:
                return str(json.loads(request.content or b"{}").get("name") or "").lower() or None
            except (ValueError, AttributeError):
                return None
        parts = path.split("/")
        return parts[3].lower() if len(parts) > 3 and path.startswith("/api/nodes/") else None

    async def on_request(self, request: httpx.Request) -> None:
        if not request.url.path.startswith("/api/nodes/") or "x-node-key" in request.headers:
            return
        name = self._name(request)
        enrolling = request.url.path in ("/api/nodes/register", "/api/nodes/enroll")
        auth = request.headers.get("authorization", "")
        if name and name not in self.keys and not enrolling and auth.lower().startswith("bearer ") and self.transport:
            await self._enroll(name, auth)
        if name in self.keys:
            request.headers["X-Node-Key"] = self.keys[name]

    async def on_response(self, response: httpx.Response) -> None:
        request = response.request
        if request.url.path in ("/api/nodes/register", "/api/nodes/enroll") and response.status_code == 200:
            await response.aread()
            key = response.json().get("node_key")
            name = self._name(request)
            if key and name:
                self.keys[name] = key
        elif request.method == "DELETE" and response.status_code == 204:
            self.keys.pop(self._name(request) or "", None)


@pytest_asyncio.fixture
async def node_keys():
    return NodeKeys()


@pytest_asyncio.fixture
async def client(app, node_keys):
    transport = httpx.ASGITransport(app=app)
    node_keys.transport = transport
    hooks = {"request": [node_keys.on_request], "response": [node_keys.on_response]}
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver", event_hooks=hooks) as c:
        yield c


@pytest_asyncio.fixture
async def db_session(app):
    async with app.state.sessionmaker() as session:
        yield session


def pytest_configure(config):
    config.addinivalue_line("markers", "network: test needs internet access (huggingface.co)")


@pytest.fixture(autouse=True)
def _no_registry_of_main(monkeypatch):
    """Tests have no image registry of main: the workload controller must not wait for one (app.nodes.versions.refresh_pins).
    The tests of the registry client call `versions.REGISTRY` directly."""
    from app.nodes import versions

    async def settled(state):
        return True

    monkeypatch.setattr(versions, "refresh_pins", settled)
