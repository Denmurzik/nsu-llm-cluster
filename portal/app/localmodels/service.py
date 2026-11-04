"""«Мои модели»: resumable chunked uploads, the drop folder on main, the catalog and deletion of own model files.

An upload is created with the file's name and size (and optionally its sha256), then receives parts in order: a
part is accepted only at the offset the server already has, so after a broken connection or a pause the browser
asks for the upload and continues from `received_bytes`. `finish` hands the complete file to the controllers
process, which computes sha256 (compared with the expected one when given), checks the GGUF header and renames the
file into models/local/<model>/. Files the admin copies onto the volume are found by `scan` (controllers, on a timer,
and the «Найти файлы» button), checked the same way and shown next to the uploaded ones.

Errors are `LocalModelError` with the HTTP status and the Russian message for the admin.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import Hook
from app.catalog import estimate as est
from app.catalog.gguf import GgufMeta
from app.catalog.hf import HfFile, group_gguf, parse_quant
from app.localmodels.store import (
    GiB,
    LOCAL_PREFIX,
    LocalNotFound,
    FileCheck,
    LocalStore,
    check_gguf,
    local_repo,
    shard_index,
    signature_error,
    valid_file,
    valid_name,
)
from app.models import UPLOAD_ACTIVE, LocalModelFile, ModelDeployment, ModelUpload

log = logging.getLogger(__name__)

MiB = 2**20
MIN_FILE_BYTES = 24  # signature, version and the two counts of a GGUF header
UPLOAD_TTL = timedelta(days=7)  # an upload that received nothing this long is cancelled
STRAY_PART_AGE_S = 3600.0  # a .part without an active upload is removed once this old
FAILED_SHOWN = timedelta(days=7)  # failed uploads stay in the list this long
HASH_BLOCK = 64 * MiB
WRITE_BLOCK = 4 * MiB
PROGRESS_EVERY_S = 2.0
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOCK_NOT_AVAILABLE = "55P03"


def _append(f, data: bytes) -> None:
    """One write of a part into the .part file (a function of its own so a test can fill the disk)."""
    f.write(data)


class LocalModelError(Exception):
    def __init__(self, status: int, detail: str, headers: dict[str, str] | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = headers


def _gb(n: int) -> str:
    return f"{n / GiB:.1f}".replace(".", ",") + " ГБ"


def _now() -> datetime:
    return datetime.now(UTC)


def meta_of(row: LocalModelFile) -> GgufMeta | None:
    return GgufMeta(**row.meta) if row.meta else None


# ---------- views ----------


def upload_view(u: ModelUpload, chunk_bytes: int) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "model": u.model,
        "file": u.path,
        "size_bytes": u.size_bytes,
        "received_bytes": u.received_bytes,
        "status": u.status,
        "sha256_expected": u.sha256_expected,
        "sha256": u.sha256,
        "verified_bytes": u.verified_bytes,
        "error": u.error or None,
        "chunk_bytes": chunk_bytes,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "updated_at": u.updated_at.isoformat() if u.updated_at else None,
    }


def chunk_bytes(settings) -> int:
    return settings.upload_chunk_mib * MiB


async def _local_deployments(db: AsyncSession) -> list[tuple[str, str, str | None]]:
    rows = await db.execute(
        select(ModelDeployment.name, ModelDeployment.repo, ModelDeployment.file)
        .where(ModelDeployment.repo.startswith(LOCAL_PREFIX, autoescape=True))
        .order_by(ModelDeployment.name)
    )
    return [tuple(r) for r in rows]


def _file_view(rows: dict[str, LocalModelFile], choice, used: list[str], settings) -> dict[str, Any]:
    first = rows[choice.file]
    shard = shard_index(choice.file)
    expected = shard[1] if shard else 1
    complete = len(choice.shards) == expected and (shard is None or shard[0] == 1)
    meta = meta_of(first) if complete else None
    estimate = None
    if meta is not None:
        ctx, parallel = settings.default_ctx_per_request, settings.default_parallel
        e = est.estimate(choice.size_bytes, meta, min(ctx, meta.max_ctx), parallel)
        estimate = {"ctx_per_request": min(ctx, meta.max_ctx), "parallel": parallel, "total_bytes": e.total_bytes}
    return {
        "file": choice.file,
        "size_bytes": choice.size_bytes,
        "quant": choice.quant,
        "shards": choice.shards,
        "status": "ready" if meta is not None else "incomplete",
        "error": None if complete else f"Не хватает частей: есть {len(choice.shards)} из {expected}",
        "sha256": first.sha256,
        "origin": first.origin,
        "meta": dataclasses.asdict(meta) if meta is not None else None,
        "estimate": estimate,
        "used_by": used,
        "updated_at": first.updated_at.isoformat() if first.updated_at else None,
    }


async def catalog(db: AsyncSession, store: LocalStore, settings) -> dict[str, Any]:
    """Own models with their files (shards grouped), the uploads in progress or failed, and the volume's space."""
    files = (await db.scalars(select(LocalModelFile).order_by(LocalModelFile.model, LocalModelFile.path))).all()
    deployments = await _local_deployments(db)
    by_model: dict[str, list[LocalModelFile]] = {}
    for f in files:
        by_model.setdefault(f.model, []).append(f)
    models = []
    for name, rows in by_model.items():
        repo = local_repo(name)
        ready = {r.path: r for r in rows if r.status == "ready"}
        views = []
        for choice in group_gguf([HfFile(p, r.size_bytes) for p, r in ready.items()]):
            used = [d for d, r, f in deployments if r == repo and f == choice.file]
            views.append(_file_view(ready, choice, used, settings))
        for r in rows:
            if r.status == "invalid":
                views.append({
                    "file": r.path, "size_bytes": r.size_bytes, "quant": parse_quant(r.path), "shards": [r.path],
                    "status": "invalid", "error": r.error or None, "sha256": r.sha256, "origin": r.origin,
                    "meta": None, "estimate": None, "used_by": [d for d, rp, f in deployments if rp == repo and f == r.path],
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                })
        models.append({
            "name": name,
            "repo": repo,
            "size_bytes": sum(r.size_bytes for r in rows),
            "used_by": sorted({d for d, r, _ in deployments if r == repo}),
            "files": views,
        })
    uploads = (
        await db.scalars(
            select(ModelUpload)
            .where(
                ModelUpload.status.in_(UPLOAD_ACTIVE)
                | ((ModelUpload.status == "failed") & (ModelUpload.updated_at > _now() - FAILED_SHOWN))
            )
            .order_by(ModelUpload.created_at)
        )
    ).all()
    used, free = await asyncio.to_thread(lambda: (store.used_bytes(), store.free_bytes()))
    limit = settings.local_models_max_gib * GiB or None
    return {
        "models": models,
        "uploads": [upload_view(u, chunk_bytes(settings)) for u in uploads],
        "storage": {
            "used_bytes": used, "free_bytes": free, "limit_bytes": limit, "chunk_bytes": chunk_bytes(settings),
            "folder": str(store.models_dir),
        },
    }


# ---------- uploads ----------


async def _upload(db: AsyncSession, upload_id: uuid.UUID, lock: bool = False, nowait: bool = False) -> ModelUpload:
    query = select(ModelUpload).where(ModelUpload.id == upload_id).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update(nowait=nowait)
    try:
        row = await db.scalar(query)
    except DBAPIError as e:
        if getattr(e.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
            await db.rollback()
            raise LocalModelError(409, "Эта загрузка уже принимает часть файла в другом запросе") from e
        raise
    if row is None:
        raise LocalModelError(404, "Загрузка не найдена")
    return row


_NOT_UPLOADING = {
    "verifying": "Файл уже загружен и проверяется",
    "done": "Загрузка уже завершена",
    "cancelled": "Загрузка отменена",
}


def _not_uploading(row: ModelUpload) -> LocalModelError:
    if row.status == "failed":
        return LocalModelError(409, f"Загрузка завершилась ошибкой: {row.error}")
    return LocalModelError(409, _NOT_UPLOADING.get(row.status, "Загрузка не принимает данные"))


async def create_upload(
    db: AsyncSession, store: LocalStore, settings, model: str, file: str, size: int, sha256: str | None, user: str
) -> tuple[ModelUpload, bool]:
    """A new upload, or the active one of the same file and size (the browser resumes it): (row, created)."""
    if not valid_name(model):
        raise LocalModelError(
            422, "Имя модели: латинские буквы, цифры, точка, дефис и подчёркивание, до 96 символов, начинается с буквы или цифры"
        )
    if not valid_file(file):
        raise LocalModelError(422, "Имя файла: латинские буквы, цифры, точка, дефис и подчёркивание, расширение .gguf")
    if size < MIN_FILE_BYTES:
        raise LocalModelError(422, f"Размер файла не меньше {MIN_FILE_BYTES} байт")
    if sha256 is not None:
        sha256 = sha256.strip().lower() or None
    if sha256 is not None and not _SHA256.fullmatch(sha256):
        raise LocalModelError(422, "sha256: 64 шестнадцатеричных символа")
    if await asyncio.to_thread(store.path_of(model, file).exists):
        raise LocalModelError(409, f"Файл {file} уже есть в модели {model}")
    active = (await db.scalars(select(ModelUpload).where(ModelUpload.status.in_(UPLOAD_ACTIVE)))).all()
    same = next((u for u in active if u.model == model and u.path == file), None)
    if same is not None:
        if same.status == "verifying":
            raise LocalModelError(409, "Этот файл уже загружен и проверяется")
        if same.size_bytes != size:
            raise LocalModelError(409, "Этот файл уже загружается с другим размером: отмените прежнюю загрузку")
        if sha256 and same.sha256_expected != sha256:
            same.sha256_expected = sha256
            await db.commit()
        return same, False
    limit = settings.local_models_max_gib * GiB
    if limit:
        used = await asyncio.to_thread(store.used_bytes) + sum(u.size_bytes for u in active)
        if used + size > limit:
            raise LocalModelError(
                413,
                f"Лимит своих моделей {_gb(limit)}: занято {_gb(used)} с учётом загрузок, файл {_gb(size)}. "
                "Удалите ненужные модели или увеличьте LOCAL_MODELS_MAX_GIB",
            )
    free, reserve = await asyncio.to_thread(lambda: (store.free_bytes(), store.reserve_bytes()))
    pending = sum(u.size_bytes - u.received_bytes for u in active if u.status == "uploading")
    need = size + pending + reserve
    if need > free:
        raise LocalModelError(
            507,
            f"На диске главного не хватает места: свободно {_gb(free)}, нужно {_gb(need)} "
            f"(файл, незавершённые загрузки и запас {_gb(reserve)})",
        )
    row = ModelUpload(model=model, path=file, size_bytes=size, sha256_expected=sha256, status="uploading", created_by=user)
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as e:  # a concurrent create of the same file
        await db.rollback()
        raise LocalModelError(409, "Этот файл уже загружается") from e

    def make_part() -> None:
        store.uploads_dir.mkdir(parents=True, exist_ok=True)
        store.part_path(str(row.id)).touch()

    await asyncio.to_thread(make_part)
    await db.commit()
    return row, True


def _size(path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return -1


async def _fail(db: AsyncSession, store: LocalStore, row: ModelUpload, error: str) -> None:
    row.status, row.error = "failed", error[:512]
    await asyncio.to_thread(store.part_path(str(row.id)).unlink, True)
    await db.commit()
    log.warning("upload %s of local/%s/%s failed: %s", row.id, row.model, row.path, error)


async def receive_chunk(
    db: AsyncSession,
    store: LocalStore,
    settings,
    upload_id: uuid.UUID,
    offset: int,
    length: int | None,
    chunks: AsyncIterator[bytes],
) -> ModelUpload:
    """Appends one part at `offset`, which must be what the server already has (409 with `Upload-Offset` otherwise).

    The row stays locked while the part arrives: a second request for the same upload gets 409 at once. A broken
    connection keeps what arrived (the caller ends `chunks` early), so the next part continues from there."""
    row = await _upload(db, upload_id, lock=True, nowait=True)
    if row.status != "uploading":
        raise _not_uploading(row)
    part = store.part_path(str(row.id))
    have = await asyncio.to_thread(_size, part)
    if have < 0:  # the volume was cleaned by hand: the upload starts over
        await asyncio.to_thread(lambda: (store.uploads_dir.mkdir(parents=True, exist_ok=True), part.touch()))
        have = 0
    if offset != have:
        await db.rollback()
        raise LocalModelError(
            409, f"Смещение {offset} не совпадает с уже принятым объёмом: {have} байт", {"Upload-Offset": str(have)}
        )
    limit = chunk_bytes(settings)
    remaining = row.size_bytes - have
    if length is not None and length > limit:
        await db.rollback()
        raise LocalModelError(413, f"Часть файла больше {settings.upload_chunk_mib} МБ")
    if length is not None and length > remaining:
        await db.rollback()
        raise LocalModelError(413, f"Часть выходит за конец файла: осталось {remaining} байт")
    allowed = min(limit, remaining)
    written = 0
    f = await asyncio.to_thread(part.open, "ab")
    try:
        buf = bytearray()
        async for data in chunks:
            if written + len(buf) + len(data) > allowed:
                await asyncio.to_thread(f.truncate, have)
                await db.rollback()
                raise LocalModelError(413, f"Часть выходит за конец файла или больше {settings.upload_chunk_mib} МБ")
            buf += data
            if len(buf) >= WRITE_BLOCK:
                await asyncio.to_thread(_append, f, bytes(buf))
                written += len(buf)
                buf.clear()
        if buf:
            await asyncio.to_thread(_append, f, bytes(buf))
            written += len(buf)
    except OSError as e:
        log.error("upload %s: write failed: %s", row.id, e)
        row.received_bytes = await asyncio.to_thread(_size, part)
        await db.commit()
        raise LocalModelError(507, "Не удалось записать часть файла: на диске главного закончилось место или он недоступен") from e
    finally:
        await asyncio.to_thread(f.close)
    total = have + written
    if have < 8 <= total:  # the first bytes arrived: a file that is not GGUF is refused at once, not after 400 GB

        def head() -> bytes:
            with part.open("rb") as fh:
                return fh.read(8)

        error = signature_error(await asyncio.to_thread(head))
        if error:
            await _fail(db, store, row, error)
            raise LocalModelError(422, error)
    row.received_bytes = total
    row.updated_at = _now()
    await db.commit()
    return row


async def get_upload(db: AsyncSession, upload_id: uuid.UUID) -> ModelUpload:
    return await _upload(db, upload_id)


async def finish_upload(
    db: AsyncSession, store: LocalStore, upload_id: uuid.UUID, audit: Hook | None = None
) -> ModelUpload:
    row = await _upload(db, upload_id, lock=True)
    if row.status == "verifying":
        return row
    if row.status != "uploading":
        raise _not_uploading(row)
    have = await asyncio.to_thread(_size, store.part_path(str(row.id)))
    if have != row.size_bytes:
        error = LocalModelError(409, f"Файл загружен не полностью: {max(have, 0)} из {row.size_bytes} байт")
        await db.rollback()  # the row expires here: the message is built before
        raise error
    row.received_bytes, row.status, row.verified_bytes = have, "verifying", 0
    if audit is not None:  # the first finish only: a repeat returned above; awaited in the transaction of the change
        await audit(row)
    await db.commit()
    return row


async def cancel_upload(db: AsyncSession, store: LocalStore, upload_id: uuid.UUID) -> None:
    row = await _upload(db, upload_id, lock=True)
    if row.status == "done":
        raise LocalModelError(409, "Загрузка уже завершена: удалите файл в каталоге «Мои модели»")
    if row.status != "cancelled":
        row.status = "cancelled"
    await asyncio.to_thread(store.part_path(str(row.id)).unlink, True)
    await db.commit()


async def _status(db: AsyncSession, upload_id) -> str | None:
    return await db.scalar(select(ModelUpload.status).where(ModelUpload.id == upload_id))


async def _verify(db: AsyncSession, store: LocalStore, row: ModelUpload) -> None:
    part = store.part_path(str(row.id))
    if not await asyncio.to_thread(part.is_file):
        return await _fail(db, store, row, "Файл загрузки пропал с диска")
    digest = hashlib.sha256()
    done = 0
    last = time.monotonic()
    f = await asyncio.to_thread(part.open, "rb")
    try:
        while block := await asyncio.to_thread(f.read, HASH_BLOCK):
            await asyncio.to_thread(digest.update, block)
            done += len(block)
            if time.monotonic() - last >= PROGRESS_EVERY_S:
                last = time.monotonic()
                if await _status(db, row.id) != "verifying":
                    return  # cancelled meanwhile
                row.verified_bytes = done
                await db.commit()
    finally:
        await asyncio.to_thread(f.close)
    sha = digest.hexdigest()
    row.verified_bytes, row.sha256 = done, sha
    if done != row.size_bytes:
        return await _fail(db, store, row, f"Размер файла {done} байт вместо {row.size_bytes}")
    if row.sha256_expected and sha != row.sha256_expected:
        return await _fail(db, store, row, f"sha256 не совпадает: ожидался {row.sha256_expected}, получен {sha}")
    check = await asyncio.to_thread(check_gguf, part, row.path)
    if check.error:
        return await _fail(db, store, row, check.error)
    target = store.path_of(row.model, row.path)
    if await asyncio.to_thread(target.exists):
        return await _fail(db, store, row, f"Файл {row.path} уже есть в модели {row.model}")
    await db.commit()  # sha256 and progress; the last step runs under the row lock
    row = await _upload(db, row.id, lock=True)  # a cancel either finished before this or waits until the commit
    if row.status != "verifying":
        await db.rollback()
        return

    def move() -> os.stat_result:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, target)
        return target.stat()

    st = await asyncio.to_thread(move)
    # upsert: a scan may have listed the renamed file already, as a folder file without a checksum
    values = {
        "size_bytes": st.st_size, "mtime": st.st_mtime, "sha256": sha, "origin": "upload", "status": "ready",
        "error": "", "meta": dataclasses.asdict(check.meta) if check.meta is not None else None, "updated_at": _now(),
    }
    await db.execute(
        pg_insert(LocalModelFile)
        .values(model=row.model, path=row.path, **values)
        .on_conflict_do_update(constraint="uq_local_model_files_model_path", set_=values)
    )
    row.status = "done"
    await db.commit()
    await sync_manifests(db, store)
    log.info("upload %s verified: local/%s/%s, sha256 %s", row.id, row.model, row.path, sha)


async def verify_pending(db: AsyncSession, store: LocalStore) -> None:
    """Checks every finished upload (controllers): sha256, GGUF header, then the rename into the model directory."""
    rows = (
        await db.scalars(select(ModelUpload).where(ModelUpload.status == "verifying").order_by(ModelUpload.updated_at))
    ).all()
    for row in rows:
        try:
            await _verify(db, store, row)
        except OSError as e:
            await db.rollback()
            await _fail(db, store, await _upload(db, row.id), f"Не удалось проверить файл: {e.strerror or e}")


async def expire_uploads(db: AsyncSession, store: LocalStore) -> None:
    """Cancels uploads idle for a week; removes .part files no active upload owns."""
    stale = (
        await db.scalars(
            select(ModelUpload).where(ModelUpload.status == "uploading", ModelUpload.updated_at < _now() - UPLOAD_TTL)
        )
    ).all()
    for row in stale:
        row.status, row.error = "cancelled", "Загрузка не продолжалась 7 дней и отменена"
        await asyncio.to_thread(store.part_path(str(row.id)).unlink, True)
    await db.commit()
    active = {str(i) for i in (await db.scalars(select(ModelUpload.id).where(ModelUpload.status.in_(UPLOAD_ACTIVE)))).all()}

    def sweep() -> None:
        if not store.uploads_dir.is_dir():
            return
        now = time.time()
        for p in store.uploads_dir.iterdir():
            try:
                if p.suffix == ".part" and p.stem not in active and now - p.stat().st_mtime >= STRAY_PART_AGE_S:
                    p.unlink(missing_ok=True)
            except OSError:
                continue

    await asyncio.to_thread(sweep)


# ---------- the drop folder ----------


async def sync_manifests(db: AsyncSession, store: LocalStore) -> None:
    """Writes each model's manifest of ready files (store.MANIFEST): the mirror serves those and nothing else."""
    rows = (await db.scalars(select(LocalModelFile).where(LocalModelFile.status == "ready"))).all()
    ready: dict[str, dict[str, dict]] = {}
    for r in rows:
        ready.setdefault(r.model, {})[r.path] = {"size": r.size_bytes, "mtime": r.mtime, "sha256": r.sha256}
    await asyncio.to_thread(store.write_manifests, ready)


async def scan(db: AsyncSession, store: LocalStore, settings, now: float | None = None) -> None:
    """Brings the catalog in line with models/local/: loose files are moved into model directories, new or changed
    files (unchanged for LOCAL_MODELS_SETTLE_S) are checked, rows of vanished files are dropped."""
    now = time.time() if now is None else now
    settle = settings.local_models_settle_s
    for src, dst in await asyncio.to_thread(store.adopt_loose, settle, now):
        log.info("local models: %s moved to %s", src, dst)
    found = await asyncio.to_thread(store.walk)
    rows = {(r.model, r.path): r for r in (await db.scalars(select(LocalModelFile))).all()}
    seen = set()
    for f in found:
        key = (f.model, f.path)
        seen.add(key)
        row = rows.get(key)
        if row is not None and row.size_bytes == f.size and row.mtime == f.mtime:
            continue
        if now - f.mtime < settle:
            continue  # still being copied
        if f.problem:
            check = FileCheck(None, f.problem)
        else:
            check = await asyncio.to_thread(check_gguf, store.local_dir / f.model / f.path)
        values = {
            "size_bytes": f.size, "mtime": f.mtime, "status": "invalid" if check.error else "ready",
            "error": check.error or "", "meta": dataclasses.asdict(check.meta) if check.meta is not None else None,
        }
        if row is None:
            # nothing on conflict: verification of an upload (or a scan in another process) wrote this file first
            await db.execute(
                pg_insert(LocalModelFile)
                .values(model=f.model, path=f.path, origin="folder", **values)
                .on_conflict_do_nothing(constraint="uq_local_model_files_model_path")
            )
            log.info("local models: found local/%s/%s (%s)", f.model, f.path, check.error or "ok")
        else:
            row.sha256 = None  # the file was replaced: its checksum is no longer known
            for k, v in values.items():
                setattr(row, k, v)
    for key, row in rows.items():
        if key not in seen:
            await db.delete(row)
    try:
        await db.commit()
    except IntegrityError:  # a concurrent writer of the same rows: the next scan settles it
        await db.rollback()
    await sync_manifests(db, store)


# ---------- deletion ----------


async def delete_model(
    db: AsyncSession, store: LocalStore, name: str, file: str | None = None, audit: Hook | None = None
) -> None:
    """Deletes a whole own model or one of its files (all shards), unless a deployment uses it."""
    try:
        model_dir = store.model_dir(name)
    except LocalNotFound as e:
        # a directory with a name the catalog flags as invalid can still be deleted as a whole
        bad = store.local_dir / name
        if file is not None or "/" in name or "\\" in name or name in (".", "..") or name.startswith(".") or bad.is_symlink():
            raise LocalModelError(404, "Своя модель не найдена") from e
        model_dir = bad
    rows = (await db.scalars(select(LocalModelFile).where(LocalModelFile.model == name))).all()
    if not rows and not await asyncio.to_thread(model_dir.is_dir):
        raise LocalModelError(404, "Своя модель не найдена")
    repo = local_repo(name)
    paths: list[str] | None = None
    if file is not None:
        choice = next((c for c in group_gguf([HfFile(r.path, r.size_bytes) for r in rows]) if c.file == file), None)
        if choice is None:
            raise LocalModelError(404, "Файл не найден в модели")
        paths = choice.shards
    users = [d for d, r, f in await _local_deployments(db) if r == repo and (file is None or f == file)]
    if users:
        raise LocalModelError(409, f"Модель используется развёртываниями: {', '.join(users)}")
    if paths is None:
        await asyncio.to_thread(shutil.rmtree, model_dir, True)
        doomed = rows
    else:
        await asyncio.to_thread(store.remove, name, paths)
        doomed = [r for r in rows if r.path in paths]
    for r in doomed:
        await db.delete(r)
    if audit is not None:
        await audit(name)
    await db.commit()
    await sync_manifests(db, store)
    log.info("local models: deleted %s%s", repo, f"/{file}" if file else "")
