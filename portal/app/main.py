import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import (
    admin,
    admin_backups,
    admin_cache,
    admin_deployments,
    admin_local_models,
    admin_metrics,
    admin_node_delete,
    admin_node_identity,
    admin_settings,
    admin_status,
    admin_usage,
    admin_versions,
    admin_users,
    keys,
    models,
    playground,
    usage,
)
from app.auth import routes as auth_routes
from app.auth.routes import missing_oidc
from app.auth.sessions import SlidingCookieMiddleware
from app.catalog.hf import HfClient, MirrorFirstHf
from app.config import Settings, get_settings
from app.controllers import runner
from app.db import make_engine, run_migrations, startup_lock, wait_for_db
from app.deployments import service as deployments_service
from app.deployments.counts import BackendCounts
from app.deployments.progress import ProgressSource
from app.deps import make_redis
from app.k8s import K8sClient
from app.litellm_client import LiteLLMClient
from app.localmodels.store import LocalStore
from app.nodes import metrics_routes as node_metrics_routes
from app.nodes import routes as node_routes
from app.settings_store import RuntimeSettings

log = logging.getLogger(__name__)

# never the SPA: `health` too, or a node url pointing at the portal would pass the backend probe (GET /health -> index.html)
_API_PREFIXES = ("api", "auth", "v1", "healthz", "readyz", "health")
_VALUE_ERROR_PREFIX = "Value error, "


def validation_errors(exc: RequestValidationError) -> list[dict]:
    """The 422 entries as the browser needs them: our own validator messages without pydantic's English
    prefix, and without the request values (a rejected 1 MB message is not echoed back)."""
    errors = []
    for e in exc.errors():
        msg = e["msg"]
        if e["type"] == "value_error":
            msg = msg.removeprefix(_VALUE_ERROR_PREFIX)
        item = {"type": e["type"], "loc": list(e["loc"]), "msg": msg}
        ctx = {k: v for k, v in (e.get("ctx") or {}).items() if k != "error"}
        if ctx:
            item["ctx"] = ctx
        errors.append(item)
    return errors


async def open_state(state, settings: Settings) -> None:
    """Clients and the database for one process: an API worker (lifespan) or the controllers process.

    A Redis client already set on `state` is kept (tests inject an in-memory one).
    """
    await wait_for_db(settings.database_url)
    engine = make_engine(settings.database_url, settings.db_pool_size, settings.db_max_overflow)
    state.settings = settings
    state.engine = engine
    state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    state.runtime = RuntimeSettings(state, settings.settings_cache_ttl_s)
    if getattr(state, "redis", None) is None:
        state.redis = make_redis(settings.redis_url)
    state.litellm = LiteLLMClient(
        settings.litellm_url,
        settings.litellm_master_key.get_secret_value(),
        backend_api_key=settings.backend_api_key.get_secret_value(),
        stream_connections=settings.litellm_stream_connections,
    )
    state.backend_counts = BackendCounts(state, settings.status_cache_ttl_s, settings.controller_interval_s * 3)
    state.k8s = K8sClient(settings.kubeconfig, settings.k8s_namespace)
    # with the mirror on main the inspector goes through it too: metadata and GGUF headers then also work without
    # internet, for files the admin put into the mirror's volume (docs/hf-mirror.md); Hugging Face directly while the
    # mirror is down
    mirror = f"http://127.0.0.1:{settings.hf_mirror_port}" if settings.hf_mirror_enabled else None
    direct = HfClient(settings.hf_endpoint, settings.hf_token.get_secret_value() or None)
    if mirror:
        state.hf = MirrorFirstHf(HfClient(mirror, settings.mirror_admin_key or None), direct)
    else:
        state.hf = direct
    state.progress = ProgressSource(mirror, settings.mirror_admin_key)
    state.local_models = LocalStore(settings.hf_mirror_data_dir)
    # several processes start at once: one at a time migrates and seeds
    async with startup_lock(engine):
        await run_migrations(settings.database_url, settings.migrations_dir)
        try:
            async with state.sessionmaker() as db:
                await deployments_service.seed_default(db, settings, state.hf)
        except NotImplementedError:
            pass


async def close_state(state) -> None:
    for name in ("litellm", "k8s", "hf", "redis"):
        close = getattr(getattr(state, name, None), "aclose", None)
        if close is not None:
            await close()
    await state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await open_state(app.state, settings)
        if missing := missing_oidc(settings):
            log.error("login is not configured: %s is empty; «Войти» answers with an error until it is set", ", ".join(missing))
        # API workers run with CONTROLLERS_ENABLED=false: the controllers have their own process
        if settings.controllers_enabled:
            runner.start(app)
        try:
            yield
        finally:
            if settings.controllers_enabled:
                await runner.stop(app)
            await close_state(app.state)

    app = FastAPI(title="NSU LLM Portal", lifespan=lifespan)
    app.add_middleware(SlidingCookieMiddleware, settings=settings)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": validation_errors(exc)}, status_code=422)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        state = app.state
        try:
            async with state.sessionmaker() as db:
                await db.execute(text("select 1"))
            db_ok = True
        except Exception:
            db_ok = False
        try:
            litellm_ok = bool(await state.litellm.health())
        except Exception:
            litellm_ok = False
        return JSONResponse({"db": db_ok, "litellm": litellm_ok}, status_code=200 if db_ok and litellm_ok else 503)

    for module in (
        auth_routes,
        node_routes,
        node_metrics_routes,
        keys,
        usage,
        playground,
        models,
        admin_deployments,
        admin_local_models,
        admin,
        admin_status,
        admin_backups,
        admin_usage,
        admin_metrics,
        admin_cache,
        admin_versions,
        admin_node_delete,
        admin_node_identity,
        admin_settings,
        admin_users,
    ):
        app.include_router(module.router)
    app.include_router(admin_versions.public_router)  # no token: the name of the node image for the installer
    app.include_router(admin_versions.node_router)  # the node side of the same task: image report, «should I update»

    web_dist = Path(settings.web_dist)
    if web_dist.is_dir():
        assets = web_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.split("/", 1)[0] in _API_PREFIXES:
                raise HTTPException(status_code=404, detail="Не найдено")
            return FileResponse(web_dist / "index.html")

    return app
