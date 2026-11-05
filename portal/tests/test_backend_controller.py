import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import uvicorn
from kubernetes_asyncio.client import ApiException
from pydantic import SecretStr
from sqlalchemy import update
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config import Settings, key_fingerprint
from app.controllers import runner
from app.controllers.backends import BACKEND_STATUS_KEY, BackendController, managed_model_id, max_parallel_requests
from app.controllers.workloads import WorkloadController
from app.deployments import service
from app.litellm_client import ManagedModel
from app.models import NodeIdentity, StandaloneNode
from app.nodes import registry
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.fakes.openai_backend import create_fake_openai_app

pytestmark = pytest.mark.docker

HOST = "127.0.0.1"


def unhealthy_app() -> Starlette:
    async def health(request):
        return JSONResponse({"status": "loading"}, status_code=503)

    return Starlette(routes=[Route("/health", health)])


class Backend:
    """A real HTTP server on a free local port; `stop()` makes the port refuse connections."""

    def __init__(self, app: Starlette | None = None):
        self.server = uvicorn.Server(
            uvicorn.Config(app or create_fake_openai_app(), host=HOST, port=0, log_level="warning")
        )
        self.task: asyncio.Task | None = None
        self.port = 0

    async def start(self) -> "Backend":
        self.task = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                break
            await asyncio.sleep(0.025)
        else:
            raise RuntimeError("fake backend did not start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self.task is not None:
            self.server.should_exit = True
            await self.task
            self.task = None

    @property
    def base(self) -> str:
        return f"http://{HOST}:{self.port}"


@asynccontextmanager
async def backend(app: Starlette | None = None):
    b = await Backend(app).start()
    try:
        yield b
    finally:
        await b.stop()


def model_id(api_base: str, name: str, tokens: int | None) -> str:
    return "pm-" + hashlib.sha1(f"{api_base}|{name}|{tokens}".encode()).hexdigest()[:12]


@pytest.fixture
async def litellm(app):
    app.state.litellm = FakeLiteLLM()
    return app.state.litellm


NODE_KEYS = {"worker-1": "bk-worker-1", "worker-2": "bk-worker-2"}  # the nodes' own backend keys (app.nodes.identity)


async def set_node_key(app, name: str, key: str | None) -> None:
    async with app.state.sessionmaker() as db:
        row = await db.get(NodeIdentity, name)
        if row is None:
            row = NodeIdentity(name=name, approved=True)
            db.add(row)
        row.key_hash = None if key is None else "h-" + key
        row.backend_key = key
        await db.commit()


@pytest.fixture
async def k8s(app):
    app.state.k8s = FakeK8s()
    app.state.k8s.add_node("worker-1", accel="cpu", internal_ip=HOST)
    for name, key in NODE_KEYS.items():  # the pods of a cluster node read its own key (builders.BACKEND_KEY_FILE)
        await set_node_key(app, name, key)
    return app.state.k8s


@pytest.fixture
def controller(app, litellm, k8s, monkeypatch):
    # the fake answers like a single up-to-date LiteLLM worker: no need to wait out stale listings
    monkeypatch.setattr(app.state.settings, "backend_recent_window_s", 0.0)
    return BackendController(app.state)


class StaleLiteLLM(FakeLiteLLM):
    """`list_managed_models` answers like a LiteLLM worker that has not synced yet: it shows the entries as
    they were before the last `lag` writes."""

    def __init__(self):
        super().__init__()
        self.snapshot = None

    def freeze(self):
        self.snapshot = dict(self.models)

    async def list_managed_models(self):
        self._guard()
        return list((self.snapshot if self.snapshot is not None else self.models).values())


def add_ready_pod(k8s, port, name="gemma-4-e4b", tokens=8192, role="inference", pod="pod-1", node="worker-1"):
    k8s.add_pod(
        pod, node, role, "gemma-4-e4b", port=port, model_name=name, max_input_tokens=tokens, spec_hash="h1"
    )


def test_managed_model_id_follows_the_global_formula():
    base = "http://10.0.0.1:18080/v1"
    assert managed_model_id(base, "m", 8192) == model_id(base, "m", 8192)
    assert managed_model_id(base, "m", 8192) != managed_model_id(base, "m", 16384)
    assert managed_model_id(base, "m", 8192).startswith("pm-") and len(managed_model_id(base, "m", 8192)) == 15


def test_default_queue_factor_is_2():
    # measured in the 1000-user load test (docs/research/parallelism.md): with 8 a slow CPU backend queued
    # requests past the clients' 120 s limit; 2 keeps one queued request per slot
    assert Settings().queue_factor == 2


def test_max_parallel_requests_spreads_the_queue_over_litellm_workers():
    # LiteLLM counts max_parallel_requests per worker process: the per-worker share keeps the backend's
    # total (slots plus queue) at about parallel x QUEUE_FACTOR
    s = Settings(queue_factor=8, litellm_workers=4)
    assert [max_parallel_requests(p, s) for p in (1, 2, 3, 4)] == [2, 4, 6, 8]
    assert max_parallel_requests(2, Settings(queue_factor=8, litellm_workers=1)) == 16
    assert max_parallel_requests(1, Settings(queue_factor=1, litellm_workers=4)) == 1


def test_max_parallel_requests_lets_one_worker_fill_every_slot():
    # LiteLLM answers 429 at once (no retries), so a per-worker bound below the slot count would refuse a
    # request while the backend still has a free slot: 2 slots, QUEUE_FACTOR 2, 4 workers is 2, not 1
    assert max_parallel_requests(2, Settings(queue_factor=2, litellm_workers=4)) == 2
    assert max_parallel_requests(4, Settings(queue_factor=1, litellm_workers=4)) == 4
    assert max_parallel_requests(3, Settings(queue_factor=2, litellm_workers=4)) == 3
    assert max_parallel_requests(None, Settings(queue_factor=2, litellm_workers=4, default_parallel=2)) == 2
    # a backend of an unknown deployment counts as DEFAULT_PARALLEL slots
    assert max_parallel_requests(None, Settings(queue_factor=8, litellm_workers=4, default_parallel=3)) == 6


async def test_probe_ignores_the_proxy_of_the_environment(controller, litellm, k8s, monkeypatch):
    """HTTP_PROXY in main's environment must not send the node probes into a proxy: every backend would look dead."""
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    for var in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:1")  # nothing listens there
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
    assert len(litellm.models) == 1


async def test_litellm_client_ignores_the_proxy_of_the_environment(monkeypatch):
    from app.litellm_client import LiteLLMClient

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    client = LiteLLMClient("http://127.0.0.1:4000", "k", backend_api_key="b")
    try:
        assert not client._http._mounts and not client._stream_http._mounts
    finally:
        await client._http.aclose()
        await client._stream_http.aclose()


async def test_k8s_backend_gets_the_bound_of_its_deployment(controller, app, litellm, k8s, db_session):
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    dep.parallel = 3
    await db_session.commit()
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        k8s.add_pod("pod-x", "worker-1", "inference", "not-in-db", port=b.port, model_name="x", max_input_tokens=1)
        await controller.reconcile_once()
    by_name = {m.model_name: m.max_parallel_requests for m in litellm.models.values()}
    assert by_name == {"gemma-4-e4b": 3, "x": 2}  # a pod of an unknown deployment: DEFAULT_PARALLEL (2)


async def test_changed_bound_is_updated_in_place(controller, app, litellm, k8s, monkeypatch):
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
        (mid,) = litellm.models
        assert litellm.models[mid].max_parallel_requests == 2

        calls = []
        for name in ("delete_model", "add_model", "update_model_limit"):
            original = getattr(litellm, name)

            async def spy(*args, _name=name, _original=original, **kwargs):
                calls.append((_name, args[0]))
                await _original(*args, **kwargs)

            setattr(litellm, name, spy)

        monkeypatch.setattr(app.state.settings, "queue_factor", 8)
        await controller.reconcile_once()
        assert calls == [("update_model_limit", mid)]  # same id, never out of the router
        assert list(litellm.models) == [mid] and litellm.models[mid].max_parallel_requests == 4

        calls.clear()
        await controller.reconcile_once()
        assert calls == []  # an entry with the current bound is left alone


async def test_backend_status_is_shared_through_redis(controller, app, litellm, k8s):
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
    raw = await app.state.redis.get(BACKEND_STATUS_KEY)
    assert json.loads(raw) == app.state.backend_status
    assert 0 < await app.state.redis.ttl(BACKEND_STATUS_KEY) <= 120


async def test_redis_failure_does_not_stop_reconcile(controller, app, litellm, k8s, monkeypatch):
    import redis.exceptions

    async def broken(*args, **kwargs):
        raise redis.exceptions.ConnectionError("down")

    monkeypatch.setattr(app.state.redis, "set", broken)
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
    assert len(litellm.models) == 1 and app.state.backend_status["backends"]


async def test_entries_get_the_current_backend_key_in_place(controller, app, litellm, k8s, monkeypatch):
    """Each entry carries its node's own key (rule 3 of task SEC-1); a node that gets a new key (reset and registered
    again) has its entries patched in place, never deleted and added."""
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        base = f"{b.base}/v1"
        mid = model_id(base, "gemma-4-e4b", 8192)
        monkeypatch.setattr(app.state.settings, "backend_api_key", SecretStr("shared-key"))
        litellm.backend_api_key = "shared-key"
        await controller.reconcile_once()
        assert litellm.models[mid].key_fingerprint == key_fingerprint("bk-worker-1")
        assert litellm.api_keys[mid] == "bk-worker-1"  # the node's own key, never the shared one

        deleted, updated = [], []
        original_delete, original_update = litellm.delete_model, litellm.update_model_key

        async def spy_delete(model_id):
            deleted.append(model_id)
            await original_delete(model_id)

        async def spy_update(model_id, api_key=None):
            updated.append(model_id)
            await original_update(model_id, api_key)

        litellm.delete_model, litellm.update_model_key = spy_delete, spy_update

        await controller.reconcile_once()
        assert updated == []  # an entry that carries its key is left alone

        await set_node_key(app, "worker-1", "bk-new")
        await controller.reconcile_once()
        assert updated == [mid] and deleted == []  # the same entry, patched: it never leaves the router
        assert litellm.api_keys[mid] == "bk-new" and litellm.models[mid].key_fingerprint == key_fingerprint("bk-new")


async def test_a_cluster_node_without_its_own_key_gets_no_entry(controller, app, litellm, k8s, monkeypatch):
    """A pod reads its node's own key; a node that has none (it never registered: an old node image) is not given the
    shared key of before: it gets no LiteLLM entry at all."""
    monkeypatch.setattr(app.state.settings, "backend_api_key", SecretStr("shared-key"))
    litellm.backend_api_key = "shared-key"
    await set_node_key(app, "worker-1", None)
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
    assert litellm.models == {}


async def test_a_failed_key_update_is_logged_and_retried(controller, app, litellm, k8s, monkeypatch):
    from app.litellm_client import LiteLLMError

    async with backend() as b:
        add_ready_pod(k8s, b.port)
        mid = model_id(f"{b.base}/v1", "gemma-4-e4b", 8192)
        await controller.reconcile_once()
        await set_node_key(app, "worker-1", "k")
        original = litellm.update_model_key

        async def refuse(model_id, api_key=None):
            raise LiteLLMError(400, "nope")

        litellm.update_model_key = refuse
        await controller.reconcile_once()  # no exception, the entry is still registered
        assert mid in litellm.models and litellm.models[mid].key_fingerprint == key_fingerprint("bk-worker-1")
        litellm.update_model_key = original
        await controller.reconcile_once()
        assert litellm.models[mid].key_fingerprint == key_fingerprint("k")


async def test_adds_ready_k8s_endpoint_with_max_tokens(controller, app, litellm, k8s):
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
        await controller.reconcile_once()  # idempotent: the same id is not added twice

        base = f"{b.base}/v1"
        assert litellm.models == {
            model_id(base, "gemma-4-e4b", 8192): ManagedModel(
                model_id(base, "gemma-4-e4b", 8192), "gemma-4-e4b", base, "k8s", "worker-1", 8192,
                key_fingerprint("bk-worker-1"), max_parallel_requests=2, access_groups=("public",),
            )
        }
        status = app.state.backend_status
        assert status["at"]
        assert status["backends"] == [
            {
                "id": model_id(base, "gemma-4-e4b", 8192),
                "model_name": "gemma-4-e4b",
                "api_base": base,
                "source": "k8s",
                "node": "worker-1",
                "max_input_tokens": 8192,
            }
        ]


async def test_split_leader_endpoint_is_added(controller, litellm, k8s):
    async with backend() as b:
        add_ready_pod(k8s, b.port, role="split-leader", pod="leader-0")
        await controller.reconcile_once()
        (model,) = litellm.models.values()
        assert (model.source, model.node, model.max_input_tokens) == ("k8s", "worker-1", 8192)


async def test_the_leader_of_a_held_split_group_leaves_litellm(app, controller, litellm, k8s):
    """A group waiting for a lost member: its leader still answers /health, but a request hangs in a recv on the dead
    rpc connection. The workload controller names such leaders (`held_split_leaders`); they are not offered."""
    async with backend() as b:
        k8s.add_pod(
            "llm-sp-g0-7c9d8f-x", "worker-1", "split-leader", "sp", port=b.port, model_name="sp", max_input_tokens=8192,
            spec_hash="h1", workload="llm-sp-g0",
        )
        await controller.reconcile_once()
        assert len(litellm.models) == 1
        try:
            app.state.held_split_leaders = {"llm-sp-g0"}
            await controller.reconcile_once()
            assert litellm.models == {}

            app.state.held_split_leaders = set()
            await controller.reconcile_once()
            assert len(litellm.models) == 1
        finally:
            del app.state.held_split_leaders


async def test_endpoint_without_model_name_is_skipped(controller, litellm, k8s):
    async with backend() as b:
        add_ready_pod(k8s, b.port, name=None)
        await controller.reconcile_once()
    assert litellm.models == {}


async def test_removes_endpoint_when_pod_gone(controller, litellm, k8s):
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
        assert len(litellm.models) == 1
        del k8s.pods["pod-1"]
        await controller.reconcile_once()
        assert litellm.models == {}


async def test_ready_pod_failing_health_is_removed(controller, app, litellm, k8s):
    async with backend() as good, backend(unhealthy_app()) as bad:
        k8s.add_node("worker-2", accel="cpu", internal_ip=HOST)
        add_ready_pod(k8s, good.port)
        add_ready_pod(k8s, bad.port, pod="pod-2", node="worker-2")
        await controller.reconcile_once()
        assert {m.node for m in litellm.models.values()} == {"worker-1"}
        assert [x["node"] for x in app.state.backend_status["backends"]] == ["worker-1"]

        # a backend that was registered earlier and now fails its probe leaves LiteLLM
        base = f"{bad.base}/v1"
        await litellm.add_model(model_id(base, "gemma-4-e4b", 8192), "gemma-4-e4b", base, "k8s", "worker-2", 8192)
        await controller.reconcile_once()
        assert {m.node for m in litellm.models.values()} == {"worker-1"}


async def test_ctx_change_replaces_model_entry(controller, litellm, k8s):
    async with backend() as b:
        add_ready_pod(k8s, b.port, tokens=8192)
        await controller.reconcile_once()
        (old,) = litellm.models
        k8s.pods["pod-1"].max_input_tokens = 16384
        await controller.reconcile_once()
        (new_model,) = litellm.models.values()
        assert new_model.id != old
        assert new_model.id == model_id(f"{b.base}/v1", "gemma-4-e4b", 16384)
        assert new_model.max_input_tokens == 16384


async def test_standalone_only_when_applied_hash_matches(controller, app, litellm, k8s, db_session):
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    async with backend() as b:
        await registry.upsert(db_session, "lab-1", b.base, "cpu", None, 16000, "0.1.0")
        await service.assign(db_session, "lab-1", dep.id)

        await registry.heartbeat(db_session, "lab-1", None)
        await controller.reconcile_once()
        assert litellm.models == {}

        await registry.heartbeat(db_session, "lab-1", "stale-hash")
        await controller.reconcile_once()
        assert litellm.models == {}

        await registry.heartbeat(db_session, "lab-1", service.spec_hash(dep))
        await controller.reconcile_once()
        base = f"{b.base}/v1"
        (model,) = litellm.models.values()
        assert model == ManagedModel(
            model_id(base, "gemma-4-e4b", dep.ctx_per_request),
            "gemma-4-e4b",
            base,
            "standalone",
            "lab-1",
            dep.ctx_per_request,
            max_parallel_requests=2,
            access_groups=("public",),
        )
        assert app.state.backend_status["backends"][0]["source"] == "standalone"

        # the node loses its assignment: the next heartbeat carries no desired config and the entry goes
        await service.assign(db_session, "lab-1", None)
        await controller.reconcile_once()
        assert litellm.models == {}


async def test_standalone_node_with_expired_heartbeat_is_removed(controller, litellm, k8s, db_session):
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    async with backend() as b:
        await registry.upsert(db_session, "lab-1", b.base, "cpu", None, 16000, "0.1.0")
        await service.assign(db_session, "lab-1", dep.id)
        await registry.heartbeat(db_session, "lab-1", service.spec_hash(dep))
        await controller.reconcile_once()
        assert len(litellm.models) == 1

        await db_session.execute(
            update(StandaloneNode).values(last_seen_at=datetime.now(UTC) - timedelta(minutes=10))
        )
        await db_session.commit()
        await controller.reconcile_once()
        assert litellm.models == {}


async def test_litellm_down_makes_no_changes(controller, app, litellm, k8s):
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        stale = ManagedModel("pm-stale", "gone", "http://10.9.9.9:18080/v1", "k8s", "ghost", 4096)
        litellm.models["pm-stale"] = stale

        litellm.fail_health = True
        await controller.reconcile_once()
        assert litellm.models == {"pm-stale": stale}
        assert not hasattr(app.state, "backend_status")

        litellm.fail_health = False
        litellm.unavailable = True
        await controller.reconcile_once()
        litellm.unavailable = False
        assert litellm.models == {"pm-stale": stale}


@pytest.mark.parametrize("failure", ["unavailable", "api_error"])
async def test_k8s_unavailable_keeps_k8s_backends_but_syncs_standalone(
    controller, app, litellm, k8s, db_session, monkeypatch, failure
):
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    async with backend() as pod_backend, backend() as node_backend:
        add_ready_pod(k8s, pod_backend.port)
        await controller.reconcile_once()
        (k8s_id,) = litellm.models
        # a standalone entry whose node never existed is removed even while the cluster is unreachable
        litellm.models["pm-old-standalone"] = ManagedModel(
            "pm-old-standalone", "gemma-4-e4b", "http://10.9.9.9:18080/v1", "standalone", "ghost", 8192
        )

        if failure == "unavailable":
            k8s.unavailable = True
        else:

            async def forbidden(role, deployment=None):
                raise ApiException(status=403, reason="Forbidden")

            monkeypatch.setattr(k8s, "list_endpoints", forbidden)

        await registry.upsert(db_session, "lab-1", node_backend.base, "cpu", None, 16000, "0.1.0")
        await service.assign(db_session, "lab-1", dep.id)
        await registry.heartbeat(db_session, "lab-1", service.spec_hash(dep))
        await controller.reconcile_once()

        sources = {m.id: m.source for m in litellm.models.values()}
        assert k8s_id in sources and sources[k8s_id] == "k8s"
        assert "pm-old-standalone" not in sources
        assert sorted(sources.values()) == ["k8s", "standalone"]
        assert {b["source"] for b in app.state.backend_status["backends"]} == {"k8s", "standalone"}


async def test_dead_node_removed_within_20s(app, litellm, k8s, monkeypatch):
    async def idle(self):
        return None

    monkeypatch.setattr(WorkloadController, "reconcile_once", idle)
    app.state.settings = app.state.settings.model_copy(update={"controller_interval_s": 0.5})
    b = await Backend().start()
    try:
        add_ready_pod(k8s, b.port)
        runner.start(app)
        try:
            deadline = time.monotonic() + 10
            while not litellm.models and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            assert len(litellm.models) == 1, "the live backend was not registered"

            await b.stop()  # the node dies, its pod is still "Ready" for the Kubernetes API
            died = time.monotonic()
            while litellm.models and time.monotonic() - died < 20:
                await asyncio.sleep(0.1)
            assert litellm.models == {}
            assert time.monotonic() - died < 20
        finally:
            await runner.stop(app)
    finally:
        await b.stop()


async def test_stale_listing_does_not_cause_churn(app, k8s, monkeypatch):
    """A worker that has not seen the controller's last writes yet: nothing is added, updated or deleted twice."""
    litellm = app.state.litellm = StaleLiteLLM()
    monkeypatch.setattr(app.state.settings, "backend_recent_window_s", 30.0)
    controller = BackendController(app.state)
    calls = []
    for name in ("add_model", "delete_model", "update_model_limit", "update_model_key"):
        original = getattr(litellm, name)

        async def spy(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            await _original(*args, **kwargs)

        setattr(litellm, name, spy)
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        litellm.freeze()  # the listing keeps answering "empty"
        await controller.reconcile_once()
        await controller.reconcile_once()
        assert calls == ["add_model"]

        litellm.snapshot = None
        (mid,) = litellm.models
        monkeypatch.setattr(app.state.settings, "queue_factor", 8)
        litellm.freeze()  # still shows the old bound
        calls.clear()
        await controller.reconcile_once()  # inside the window after the add: the update waits
        assert calls == []

        monkeypatch.setattr(app.state.settings, "backend_recent_window_s", 0.0)
        await controller.reconcile_once()
        assert calls == ["update_model_limit"] and litellm.models[mid].max_parallel_requests == 4

        monkeypatch.setattr(app.state.settings, "backend_recent_window_s", 30.0)
        litellm.freeze()
        del k8s.pods["pod-1"]
        calls.clear()
        await controller.reconcile_once()
        await controller.reconcile_once()  # the stale listing still has the deleted entry
        assert calls == ["delete_model"] and litellm.models == {}


async def test_dead_backend_is_removed_even_right_after_it_was_added(app, k8s, monkeypatch):
    litellm = app.state.litellm = FakeLiteLLM()
    monkeypatch.setattr(app.state.settings, "backend_recent_window_s", 30.0)
    controller = BackendController(app.state)
    b = await Backend().start()
    try:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
        assert len(litellm.models) == 1
    finally:
        await b.stop()
    await controller.reconcile_once()
    assert litellm.models == {}


async def test_failed_add_does_not_stop_deletes_or_the_status(controller, app, litellm, k8s, monkeypatch):
    from app.litellm_client import LiteLLMUnavailable

    stale = ManagedModel("pm-stale", "gone", "http://10.9.9.9:18080/v1", "k8s", "ghost", 4096)
    litellm.models["pm-stale"] = stale

    async def broken_add(*args, **kwargs):
        raise LiteLLMUnavailable("LiteLLM returned 500: boom")

    monkeypatch.setattr(litellm, "add_model", broken_add)
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
    assert litellm.models == {}  # the dead entry went although the add failed
    assert app.state.backend_status["backends"] == []
    assert json.loads(await app.state.redis.get(BACKEND_STATUS_KEY))["backends"] == []


async def test_failed_add_is_not_retried_within_the_window(app, k8s, monkeypatch):
    from app.litellm_client import LiteLLMUnavailable

    litellm = app.state.litellm = FakeLiteLLM()
    monkeypatch.setattr(app.state.settings, "backend_recent_window_s", 30.0)
    controller = BackendController(app.state)
    attempts = []

    async def duplicate(*args, **kwargs):  # what v1.106.6 answers for an id another worker already added
        attempts.append(args[0])
        raise LiteLLMUnavailable("LiteLLM returned 500: Failed to add model to db")

    monkeypatch.setattr(litellm, "add_model", duplicate)
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
        await controller.reconcile_once()
        assert len(attempts) == 1
        monkeypatch.setattr(app.state.settings, "backend_recent_window_s", 0.0)
        await controller.reconcile_once()
        assert len(attempts) == 2


async def test_status_publish_warning_is_rate_limited(controller, app, litellm, k8s, monkeypatch, caplog):
    import redis.exceptions

    async def broken(*args, **kwargs):
        raise redis.exceptions.ConnectionError("down")

    monkeypatch.setattr(app.state.redis, "set", broken)
    for _ in range(3):
        await controller.reconcile_once()
    assert len([r for r in caplog.records if "cannot publish the status" in r.getMessage()]) == 1


async def test_a_deployment_outside_its_window_leaves_litellm_at_once(controller, litellm, k8s, db_session, monkeypatch):
    from app.deployments import schedule

    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    dep.schedule = {"days": [1, 2, 3, 4, 5], "start": "08:00", "end": "20:00"}
    await db_session.commit()
    end = datetime(2026, 11, 6, 13, 0, tzinfo=UTC)  # Friday 20:00 in Novosibirsk
    now = {"t": end - timedelta(minutes=1)}
    monkeypatch.setattr(schedule, "now_utc", lambda: now["t"])
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
        assert len(litellm.models) == 1
        now["t"] = end + timedelta(seconds=1)  # its pods drain (SCHEDULE_DRAIN_S), no new request reaches them
        await controller.reconcile_once()
        assert litellm.models == {}


# ---------- access groups and the system prompt (2-23) ----------


async def test_entries_carry_the_access_group_and_the_system_prompt_of_their_deployment(
    controller, app, litellm, k8s, db_session
):
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    dep.system_prompt = "Отвечай коротко"
    await db_session.commit()
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
    (m,) = litellm.models.values()
    assert m.access_groups == ("public",) and m.system_prompt == "Отвечай коротко"

    # a pod of a deployment the portal does not know fails closed: no prompt, and never `public`
    async with backend() as b:
        k8s.add_pod("pod-x", "worker-1", "inference", "not-in-db", port=b.port, model_name="x", max_input_tokens=1)
        await controller.reconcile_once()
    unknown = next(m for m in litellm.models.values() if m.model_name == "x")
    assert unknown.access_groups == ("experimental",) and unknown.system_prompt == ""


async def test_a_deleted_experimental_deployment_never_turns_public_while_its_pods_drain(
    controller, app, litellm, k8s, db_session
):
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    dep.experimental = True
    await db_session.commit()
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
        (mid,) = litellm.models
        assert litellm.models[mid].access_groups == ("experimental",)

        await service.delete(db_session, dep.id)  # the row is gone at once, the pod is removed later
        for _ in range(3):
            await controller.reconcile_once()
            assert list(litellm.models) == [mid]  # the drain keeps being served (admins' keys)
            assert litellm.models[mid].access_groups == ("experimental",)


async def test_a_deleted_public_deployment_is_closed_to_users_while_its_pods_drain(controller, app, litellm, k8s, db_session):
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
        await service.delete(db_session, dep.id)
        await controller.reconcile_once()
    # closed rather than dropped: the entry is still there and only admins' keys reach it until the pod goes
    (m,) = litellm.models.values()
    assert m.access_groups == ("experimental",)


async def test_a_flag_or_prompt_change_updates_the_live_entry_in_place(controller, app, litellm, k8s, db_session):
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        await controller.reconcile_once()
        (mid,) = litellm.models
        assert litellm.models[mid].access_groups == ("public",) and litellm.models[mid].system_prompt == ""

        calls = []
        for name in ("delete_model", "add_model", "update_model_access", "update_model_limit"):
            original = getattr(litellm, name)

            async def spy(*args, _name=name, _original=original, **kwargs):
                calls.append((_name, args[0]))
                await _original(*args, **kwargs)

            setattr(litellm, name, spy)

        dep.experimental = True
        dep.system_prompt = "Новый"
        await db_session.commit()
        await controller.reconcile_once()
        assert calls == [("update_model_access", mid)]  # the same entry, never out of the router
        assert litellm.models[mid].access_groups == ("experimental",) and litellm.models[mid].system_prompt == "Новый"

        calls.clear()
        await controller.reconcile_once()
        assert calls == []  # up to date: left alone

        dep.experimental = False
        dep.system_prompt = None
        await db_session.commit()
        await controller.reconcile_once()
        assert calls == [("update_model_access", mid)]
        assert litellm.models[mid].access_groups == ("public",) and litellm.models[mid].system_prompt == ""


async def test_entries_from_before_the_feature_are_brought_into_the_public_group(controller, app, litellm, k8s):
    async with backend() as b:
        add_ready_pod(k8s, b.port)
        base = f"{b.base}/v1"
        mid = model_id(base, "gemma-4-e4b", 8192)
        litellm.models[mid] = ManagedModel(
            mid, "gemma-4-e4b", base, "k8s", "worker-1", 8192, max_parallel_requests=2
        )  # no access_groups: a key limited to `public` could not call it
        await controller.reconcile_once()
    assert litellm.models[mid].access_groups == ("public",)


async def test_standalone_entries_follow_their_deployment_too(controller, app, litellm, k8s, db_session):
    dep = await service.get_by_name(db_session, "gemma-4-e4b")
    dep.experimental = True
    dep.system_prompt = "П"
    await db_session.commit()
    async with backend() as b:
        await registry.upsert(db_session, "lab-1", b.base, "cpu", None, 16000, "0.1.0")
        await service.assign(db_session, "lab-1", dep.id)
        await registry.heartbeat(db_session, "lab-1", service.spec_hash(dep))
        await controller.reconcile_once()
    (m,) = litellm.models.values()
    assert m.source == "standalone" and m.access_groups == ("experimental",) and m.system_prompt == "П"
