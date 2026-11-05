"""«Применить к существующим ключам»: the key limits of the panel (`key_rpm_limit`, `key_tpm_limit`) are put onto the
keys that already exist, in the background.

A new key gets the current limits when it is created; this job brings the old ones in line. A user who has an own
limit of a kind (`users.rpm_limit` / `users.tpm_limit`, task 2-32) keeps it: only the kinds without an override are
sent to LiteLLM. The state lives in Redis (`JOB_KEY`): any API worker can start the job and any can show its progress;
`LOCK_KEY` keeps two jobs from running at once.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app import settings_store
from app.litellm_client import LiteLLMError, LiteLLMUnavailable
from app.models import ApiKeyRef, User

log = logging.getLogger(__name__)

JOB_KEY = "key-limits-job"
LOCK_KEY = "key-limits-job:lock"
LOCK_TTL_S = 3600
JOB_TTL_S = 24 * 3600


class AlreadyRunning(Exception):
    """A job of this installation is already running."""


async def status(redis) -> dict[str, Any]:
    """The last job: {state: none|running|done|failed, total, done, failed, rpm_limit, tpm_limit, started_at,
    finished_at, error}."""
    raw = await redis.hgetall(JOB_KEY)
    if not raw:
        return {"state": "none"}
    out: dict[str, Any] = {"state": raw.get("state", "none")}
    for field in ("total", "done", "failed", "rpm_limit", "tpm_limit"):
        out[field] = int(raw[field]) if raw.get(field, "") != "" else None
    for field in ("started_at", "finished_at", "error"):
        out[field] = raw.get(field) or None
    return out


async def _todo(state, rpm: int, tpm: int) -> list[tuple[str, int | None, int | None]]:
    """(token, rpm to send, tpm to send) of every key that needs a change; None: that limit is the user's own."""
    async with state.sessionmaker() as db:
        rows = (
            await db.execute(
                select(ApiKeyRef.litellm_token, User.rpm_limit, User.tpm_limit).join(User, User.id == ApiKeyRef.user_id)
            )
        ).all()
    return [
        (token, None if own_rpm is not None else rpm, None if own_tpm is not None else tpm)
        for token, own_rpm, own_tpm in rows
        if own_rpm is None or own_tpm is None
    ]


async def start(state, before_spawn: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> asyncio.Task:
    """Starts the job with the effective limits (AlreadyRunning when one runs). The task is kept in
    `state.key_limits_task`; its progress is `status(state.redis)`. `before_spawn(job)` runs once the lock is held and
    the job is recorded, but before any key is touched: the caller writes the audit row there, so that the keys never
    change without it (when it raises nothing is started and the lock is released)."""
    redis = state.redis
    if not await redis.set(LOCK_KEY, "1", nx=True, ex=LOCK_TTL_S):
        raise AlreadyRunning()
    try:
        async with state.sessionmaker() as db:  # from the table, not from the cache: the admin has just saved them
            limits = await settings_store.get_many(db, ("key_rpm_limit", "key_tpm_limit"), state.settings)
        rpm, tpm = limits["key_rpm_limit"], limits["key_tpm_limit"]
        await redis.delete(JOB_KEY)
        await redis.hset(
            JOB_KEY,
            mapping={
                "state": "running", "total": "", "done": 0, "failed": 0, "rpm_limit": rpm, "tpm_limit": tpm,
                "started_at": _now_iso(), "finished_at": "", "error": "",
            },
        )
        await redis.expire(JOB_KEY, JOB_TTL_S)
        if before_spawn is not None:
            await before_spawn(await status(redis))
        task = asyncio.create_task(_run(state, rpm, tpm), name="key-limits-job")
    except BaseException:
        await redis.delete(JOB_KEY, LOCK_KEY)
        raise
    state.key_limits_task = task
    return task


async def _run(state, rpm: int, tpm: int) -> None:
    redis = state.redis
    try:
        todo = await _todo(state, rpm, tpm)
        await redis.hset(JOB_KEY, "total", len(todo))
        done = failed = 0
        for token, send_rpm, send_tpm in todo:
            try:
                await state.litellm.update_key_limits(token, send_rpm, send_tpm)
                done += 1
            except LiteLLMError as exc:
                failed += 1
                log.warning("key limits: LiteLLM refused the update of a key: %s", exc)
            await redis.hset(JOB_KEY, mapping={"done": done, "failed": failed})
            await asyncio.sleep(0)
        await redis.hset(JOB_KEY, mapping={"state": "done", "finished_at": _now_iso()})
    except LiteLLMUnavailable as exc:
        log.warning("key limits: the gateway is unavailable, the job stops: %s", exc)
        await _finish_failed(redis, "Шлюз моделей недоступен")
    except asyncio.CancelledError:
        await _finish_failed(redis, "Остановлено при перезапуске портала")
        raise
    except Exception:
        log.exception("key limits: the job failed")
        await _finish_failed(redis, "Ошибка при обновлении ключей, подробности в журнале портала")
    finally:
        await redis.delete(LOCK_KEY)


async def _finish_failed(redis, message: str) -> None:
    try:
        await redis.hset(JOB_KEY, mapping={"state": "failed", "error": message, "finished_at": _now_iso()})
    except Exception:
        log.warning("key limits: could not record the failure", exc_info=True)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
