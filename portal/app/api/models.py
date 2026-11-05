"""User model catalog and the public status page data.

Experimental deployments are for admins: the catalog lists them (marked `experimental`) only for an admin, and the
public status of the login page never does.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import current_user
from app.db import get_session
from app.deployments import service
from app.config import Settings
from app.deps import get_backend_counts, get_settings_dep
from app.models import User

router = APIRouter()


@router.get("/api/models")
async def list_models(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_session), counts=Depends(get_backend_counts)
) -> list[dict]:
    return [
        {
            "name": dep.name,
            "engine": dep.engine,
            "mode": dep.mode,
            "group_size": dep.group_size,
            "ctx_per_request": dep.ctx_per_request,
            "parallel": dep.parallel,
            "ready_backends": ready,
            "status": status,
            "experimental": bool(dep.experimental),
            "vision": bool(dep.mmproj_file),  # accepts images (Playground)
        }
        for dep, ready, status in await service.catalog(db, counts)
        if user.role == "admin" or not dep.experimental
    ]


@router.get("/api/public/status")
async def public_status(
    db: AsyncSession = Depends(get_session),
    counts=Depends(get_backend_counts),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    return {
        "login_provider": settings.login_provider_name.strip() or "SSO",
        "models": [
            {"name": dep.name, "status": status, "ready_backends": ready}
            for dep, ready, status in await service.catalog(db, counts)
            if not dep.experimental
        ]
    }
