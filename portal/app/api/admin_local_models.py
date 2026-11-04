"""Admin API «Мои модели»: own model files beside Hugging Face (app.localmodels).

  GET    /api/admin/local-models                     the catalog, the uploads in progress, the space on the volume
  POST   /api/admin/local-models/scan                look for files dropped into the folder now
  DELETE /api/admin/local-models/{name}[?file=]      delete a model or one file (all shards), unless deployed
  POST   /api/admin/local-models/uploads             start (or resume) an upload: model, file, size_bytes, sha256
  GET    /api/admin/local-models/uploads/{id}        its state; `received_bytes` is where the next part starts
  PUT    /api/admin/local-models/uploads/{id}?offset=N   the next part, raw bytes (application/octet-stream)
  POST   /api/admin/local-models/uploads/{id}/finish the whole file is there: sha256 and GGUF check follow
  DELETE /api/admin/local-models/uploads/{id}        cancel, the part is removed
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect

from app import audit
from app.auth.sessions import require_admin, require_csrf
from app.config import Settings
from app.db import get_session
from app.deps import get_local_models, get_settings_dep
from app.localmodels import service as lm
from app.localmodels.store import LocalStore
from app.models import ModelUpload, User

router = APIRouter(prefix="/api/admin/local-models", dependencies=[Depends(require_admin)])
csrf = [Depends(require_csrf)]


@contextmanager
def _errors():
    try:
        yield
    except lm.LocalModelError as e:
        raise HTTPException(e.status, e.detail, headers=e.headers) from e


class UploadIn(BaseModel):
    model: str
    file: str
    size_bytes: int
    sha256: str | None = None


@router.get("")
async def local_models(
    db: AsyncSession = Depends(get_session),
    store: LocalStore = Depends(get_local_models),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    return await lm.catalog(db, store, settings)


@router.post("/scan", dependencies=csrf)
async def scan(
    db: AsyncSession = Depends(get_session),
    store: LocalStore = Depends(get_local_models),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    await lm.scan(db, store, settings)
    return await lm.catalog(db, store, settings)


@router.post("/uploads", status_code=201, dependencies=csrf)
async def create_upload(
    body: UploadIn,
    response: Response,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    store: LocalStore = Depends(get_local_models),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    with _errors():
        row, created = await lm.create_upload(
            db, store, settings, body.model, body.file, body.size_bytes, body.sha256, user.email
        )
    if not created:
        response.status_code = 200  # the active upload of this file: the browser continues it
    return lm.upload_view(row, lm.chunk_bytes(settings))


@router.get("/uploads/{upload_id}")
async def get_upload(
    upload_id: uuid.UUID, db: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings_dep)
) -> dict:
    with _errors():
        return lm.upload_view(await lm.get_upload(db, upload_id), lm.chunk_bytes(settings))


@router.put("/uploads/{upload_id}", dependencies=csrf)
async def put_part(
    upload_id: uuid.UUID,
    request: Request,
    offset: int = Query(ge=0),
    db: AsyncSession = Depends(get_session),
    store: LocalStore = Depends(get_local_models),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    raw = request.headers.get("content-length", "")
    length = int(raw) if raw.isdigit() else None

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in request.stream():
                if chunk:
                    yield chunk
        except ClientDisconnect:  # what arrived is kept: the browser continues from received_bytes
            return

    with _errors():
        row = await lm.receive_chunk(db, store, settings, upload_id, offset, length, body())
    return lm.upload_view(row, lm.chunk_bytes(settings))


@router.post("/uploads/{upload_id}/finish", status_code=202, dependencies=csrf)
async def finish_upload(
    upload_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    store: LocalStore = Depends(get_local_models),
    settings: Settings = Depends(get_settings_dep),
) -> dict:

    async def log(row: ModelUpload) -> None:
        await audit.record(
            db, user, "local_model.upload", "local_model", f"{row.model}/{row.path}",
            {"size_bytes": row.size_bytes, "sha256_expected": row.sha256_expected}, audit.client_ip(request),
        )

    with _errors():
        row = await lm.finish_upload(db, store, upload_id, audit=log)
    return lm.upload_view(row, lm.chunk_bytes(settings))


@router.delete("/uploads/{upload_id}", status_code=204, dependencies=csrf)
async def cancel_upload(
    upload_id: uuid.UUID, db: AsyncSession = Depends(get_session), store: LocalStore = Depends(get_local_models)
) -> Response:
    with _errors():
        await lm.cancel_upload(db, store, upload_id)
    return Response(status_code=204)


@router.delete("/{name}", status_code=204, dependencies=csrf)
async def delete_model(
    name: str,
    request: Request,
    file: str | None = None,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    store: LocalStore = Depends(get_local_models),
) -> Response:

    async def log(model: str) -> None:
        await audit.record(
            db, user, "local_model.delete", "local_model", model, {"file": file} if file else None,
            audit.client_ip(request),
        )

    with _errors():
        await lm.delete_model(db, store, name, file, audit=log)
    return Response(status_code=204)
