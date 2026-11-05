"""«Подтвердить» and «Сбросить ключ ноды» of the «Ноды» page (task SEC-1, app.nodes.identity).

A node registered under a new name waits for the admin's approval (setting `node_approval_required`): until then it gets no
deployment, no LiteLLM backend, no backend key and no mirror token. Approval is single or bulk, each node with its own audit
row `node.approve`. Resetting a node's key (`node.reset_key`) takes its key and its approval away: the node registers again
with the agent token and waits for approval; its old key is refused (403 node_key_invalid).
"""

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.sessions import require_admin, require_csrf
from app.db import get_session
from app.k8s import MAIN_NODE
from app.models import User
from app.nodes import identity

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

_NAME = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"


class ApproveIn(BaseModel):
    names: list[str] = Field(min_length=1, max_length=500)


class ApproveOneIn(BaseModel):
    # the claim the admin picked (a registration of a known name without its key); none: a waiting new node
    claim_id: int | None = None


def _normal(name: str) -> str:
    name = name.strip().lower()
    if name == MAIN_NODE:
        raise HTTPException(409, "Главный сервер не подтверждают: он не нода класса")
    if not re.fullmatch(_NAME, name):
        raise HTTPException(422, f"Не имя ноды: {name[:80]}")
    return name


async def _approve(
    db: AsyncSession, request: Request, user: User, names: list[str], claim_id: int | None = None
) -> list[str]:
    done = []
    for name in dict.fromkeys(_normal(n) for n in names):
        claims = await identity.live_claims(db, name)
        picked = next((c for c in claims if c.id == claim_id), None)
        try:
            row = await identity.approve(db, name, user.email, getattr(request.app.state, "k8s", None), claim_id)
        except identity.NotRegistered:
            await db.rollback()
            raise HTTPException(
                404, f"Нода «{name}» не регистрировалась (или ещё не зарегистрировалась после сброса ключа): подтверждать нечего"
            ) from None
        except identity.ClaimsPending:
            await db.rollback()
            raise HTTPException(
                409, f"На имя «{name}» есть заявки: выберите одну в строке ноды, сверив её адрес со знакомыми адресами"
            ) from None
        except identity.NoSuchClaim:
            await db.rollback()
            raise HTTPException(404, "Этой заявки уже нет: обновите страницу") from None
        details: dict[str, Any] = {"has_key": row.key_hash is not None}
        if picked is not None:
            details |= {"claim_ip": picked.ip, "rejected": [c.ip for c in claims if c.id != picked.id]}
        await audit.record(db, user, "node.approve", "node", name, details, audit.client_ip(request))
        done.append(name)
    await db.commit()
    for name in done:
        log.info("node %s approved by %s", name, user.email)
    return done


@router.post("/nodes/{name}/approve", dependencies=[Depends(require_csrf)])
async def approve_one(
    name: str,
    request: Request,
    body: ApproveOneIn | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """«Подтвердить»: the node may serve (the controllers assign and label it in their next round). With `claim_id` the
    claim the admin picked becomes the node (the other claims are rejected)."""
    return {"approved": await _approve(db, request, user, [name], body.claim_id if body else None)}


@router.delete("/nodes/{name}/claims", dependencies=[Depends(require_csrf)])
async def reject_claims(
    name: str,
    request: Request,
    claim_id: int | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """«Отклонить»: one claim (`claim_id`) or every claim on the name; an audit row each (`node.claim_reject`)."""
    name = _normal(name)
    gone = await identity.reject_claims(db, name, claim_id)
    for c in gone:
        await audit.record(db, user, "node.claim_reject", "node", name, {"claim_ip": c.ip}, audit.client_ip(request))
    await db.commit()
    return {"node": name, "rejected": len(gone)}


@router.post("/nodes/approve", dependencies=[Depends(require_csrf)])
async def approve_many(
    body: ApproveIn, request: Request, db: AsyncSession = Depends(get_session), user: User = Depends(require_admin)
) -> dict[str, Any]:
    """«Подтвердить выбранные»: several waiting new nodes at once, an audit row each. A name with claims is 409: those are
    picked one by one in the row."""
    return {"approved": await _approve(db, request, user, body.names)}


@router.post("/nodes/{name}/reset-key", dependencies=[Depends(require_csrf)])
async def reset_key(
    name: str, request: Request, db: AsyncSession = Depends(get_session), user: User = Depends(require_admin)
) -> dict[str, Any]:
    """«Сбросить ключ ноды»: the node's key and approval go. The node (or whoever registers the name next) gets a new key
    with the agent token and waits for approval; LiteLLM loses the node's backends in the controller's next round."""
    name = _normal(name)
    if not await identity.reset(db, name):
        raise HTTPException(404, "У ноды нет ключа: портал её не регистрировал")
    await audit.record(db, user, "node.reset_key", "node", name, None, audit.client_ip(request))
    await db.commit()
    log.warning("node %s: key reset by %s, it waits for approval after its next registration", name, user.email)
    return {"node": name, "approved": False}
