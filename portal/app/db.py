import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent


class Base(DeclarativeBase):
    pass


# pg_advisory_lock key held while a process migrates and seeds (API workers and the controllers start together)
STARTUP_LOCK_KEY = 0x6E73756C6C6D  # "nsullm"


def make_engine(url: str, pool_size: int = 5, max_overflow: int = 10) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow)


@asynccontextmanager
async def startup_lock(engine: AsyncEngine) -> AsyncIterator[None]:
    """Session-level advisory lock: concurrent `alembic upgrade` or seeding from several processes would collide."""
    async with engine.connect() as conn:
        await conn.execute(text("select pg_advisory_lock(:k)"), {"k": STARTUP_LOCK_KEY})
        await conn.commit()
        try:
            yield
        finally:
            await conn.execute(text("select pg_advisory_unlock(:k)"), {"k": STARTUP_LOCK_KEY})
            await conn.commit()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def resolve_migrations_dir(configured: str = "") -> Path:
    """Directory that holds alembic.ini and migrations/.

    Order: the configured path (MIGRATIONS_DIR); the directory next to the app package when it has
    alembic.ini (source checkout, editable install); the current working directory (non-editable
    install, where alembic.ini and migrations/ are copied next to the venv).
    """
    if configured:
        return Path(configured)
    if (_ROOT / "alembic.ini").is_file():
        return _ROOT
    return Path.cwd()


def _alembic_config(url: str, migrations_dir: str = "") -> Config:
    root = resolve_migrations_dir(migrations_dir)
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["url"] = url
    return cfg


def _upgrade(url: str, migrations_dir: str = "") -> None:
    command.upgrade(_alembic_config(url, migrations_dir), "head")


async def run_migrations(url: str, migrations_dir: str = "") -> None:
    await asyncio.to_thread(_upgrade, url, migrations_dir)


async def wait_for_db(url: str, timeout_s: float | None = None) -> None:
    """Retry every 2 s until the database answers. None waits forever, logging every 30 s."""
    started = time.monotonic()
    last_log: float | None = None
    engine = make_engine(url)
    try:
        while True:
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("select 1"))
                return
            except Exception as exc:
                now = time.monotonic()
                if timeout_s is not None and now - started >= timeout_s:
                    raise TimeoutError(f"database is not available after {timeout_s} s") from exc
                if last_log is None or now - last_log >= 30:
                    log.warning("waiting for database: %s", exc)
                    last_log = now
                await asyncio.sleep(2)
    finally:
        await engine.dispose()
