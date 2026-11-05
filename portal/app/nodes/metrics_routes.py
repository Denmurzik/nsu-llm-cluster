"""POST /api/nodes/{name}/metrics: the node's own sample (node-metrics.sh, every NODE_PUSH_INTERVAL_S seconds).

Bearer agent token and the node's own key (X-Node-Key), like the rest of the node API. The latest sample is kept in Redis for the live views; the
controllers process turns it into the per-minute row (app/controllers/metrics.py). A node pushing more often than once
per MIN_PUSH_GAP_MS gets 429 with Retry-After; a Redis that cannot be written answers 503 (the node tries again at its
next interval, the sample of a missed one is not worth a queue).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import REDIS_ERRORS
from app.metrics import live
from app.metrics.schemas import NodeMetricsIn
from app.nodes import identity
from app.nodes.routes import _NAME, require_node_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/nodes", dependencies=[Depends(require_node_token)])


@router.post("/{name}/metrics", status_code=204)
async def push_metrics(
    body: NodeMetricsIn, request: Request, name: str = Path(pattern=_NAME), db: AsyncSession = Depends(get_session)
):
    name = name.lower()
    await identity.check(request, db, name)
    await db.rollback()  # no transaction stays open over the Redis calls
    redis = request.app.state.redis
    try:
        if not await live.allow_push(redis, name):
            raise HTTPException(
                429, "Слишком частые замеры: не чаще одного в 5 с", headers={"Retry-After": str(live.MIN_PUSH_GAP_MS // 1000)}
            )
        await live.put(redis, live.NODE_KEY, name, body.sample(), live.NODE_LIVE_TTL_S)
    except REDIS_ERRORS as exc:
        log.warning("node %s: metrics sample dropped, Redis is unavailable: %s", name, exc)
        raise HTTPException(503, "Хранилище замеров недоступно") from exc
    return Response(status_code=204)
