import shutil
from pathlib import Path

import pytest

from app import db
from app.config import Settings

PORTAL = Path(__file__).resolve().parent.parent


def test_configured_dir_wins(tmp_path):
    assert db.resolve_migrations_dir(str(tmp_path)) == tmp_path


def test_package_relative_dir_used_when_alembic_ini_exists():
    assert db.resolve_migrations_dir() == PORTAL


def test_falls_back_to_cwd_for_non_editable_install(tmp_path, monkeypatch):
    # site-packages layout: no alembic.ini next to the package
    monkeypatch.setattr(db, "_ROOT", tmp_path / "site-packages")
    work = tmp_path / "opt-llm-portal"
    work.mkdir()
    monkeypatch.chdir(work)
    assert db.resolve_migrations_dir() == work


def test_settings_reads_migrations_dir_from_env(monkeypatch):
    monkeypatch.setenv("MIGRATIONS_DIR", "/opt/llm/portal")
    assert Settings().migrations_dir == "/opt/llm/portal"
    monkeypatch.delenv("MIGRATIONS_DIR")
    assert Settings().migrations_dir == ""


def test_alembic_config_uses_resolved_dir_and_passed_url(tmp_path):
    root = tmp_path / "portal"
    root.mkdir()
    shutil.copy(PORTAL / "alembic.ini", root / "alembic.ini")
    shutil.copytree(PORTAL / "migrations", root / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    cfg = db._alembic_config("postgresql+asyncpg://u:p%40ss@h/db", str(root))
    assert cfg.get_main_option("script_location") == str(root / "migrations")
    assert cfg.attributes["url"] == "postgresql+asyncpg://u:p%40ss@h/db"


@pytest.mark.docker
async def test_run_migrations_from_copied_dir(pg_url, tmp_path, monkeypatch):
    """Task 14 layout: alembic.ini + migrations/ copied away from the package, found via MIGRATIONS_DIR."""
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    root = tmp_path / "portal"
    root.mkdir()
    shutil.copy(PORTAL / "alembic.ini", root / "alembic.ini")
    shutil.copytree(PORTAL / "migrations", root / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(db, "_ROOT", tmp_path / "site-packages")  # package-relative path is useless
    monkeypatch.chdir(tmp_path)  # cwd has no alembic.ini either

    engine = create_async_engine(pg_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("drop schema public cascade"))
            await conn.execute(text("create schema public"))
        await db.run_migrations(pg_url, str(root))
        async with engine.connect() as conn:
            version = (await conn.execute(text("select version_num from alembic_version"))).scalar()
        head = ScriptDirectory.from_config(db._alembic_config(pg_url, str(root))).get_current_head()
        assert version == head and head >= "0003"
    finally:
        await engine.dispose()
