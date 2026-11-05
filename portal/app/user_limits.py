"""Limits of a user's API keys: the user's own value when the admin set one, else the global setting (2-34). The same
limits also hold for the user as a whole, over all its keys and the Playground (task SEC-1, `sweep` below)."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store
from app.config import Settings
from app.litellm_client import LiteLLMError
from app.models import ApiKeyRef, User

log = logging.getLogger(__name__)


async def key_limits(db: AsyncSession, user: User, settings: Settings | None = None) -> tuple[int, int]:
    """(requests per minute, tokens per minute) a key of this user gets."""
    rpm = user.rpm_limit if user.rpm_limit is not None else await settings_store.get(db, "key_rpm_limit", settings)
    tpm = user.tpm_limit if user.tpm_limit is not None else await settings_store.get(db, "key_tpm_limit", settings)
    return int(rpm), int(tpm)


async def max_keys(db: AsyncSession, user: User, settings: Settings | None = None) -> int:
    """How many regular keys the user may have."""
    if user.max_keys is not None:
        return user.max_keys
    return int(await settings_store.get(db, "max_keys_per_user", settings))


# ---- the user-level limits in LiteLLM (task SEC-1, rule 7) ------------------------------------------------------
#
# A key's own rpm/tpm has its own window, so a user with five keys and the Playground had six times the limit, and a
# deleted and re-created key started with an empty window. LiteLLM v1.106.6 also enforces limits of the user itself over
# all its keys together (parallel_request_limiter_v3, descriptor `user`; tests/integration/test_litellm_real_user_limits.py),
# so every user who has a key gets the same limits as its own: `ensure_user` when a key is issued, the panel when the
# admin changes them, and `sweep` (controllers process) for everything else — the global setting changed, users from
# before this release, a LiteLLM database that was reset.

SYNCED_KEY = "user-limits:synced"  # Redis hash: user id -> "rpm:tpm" last sent to LiteLLM
SYNCED_TTL_S = 3600  # the hash goes once an hour, and every user is sent again (LiteLLM's database may have been reset)


async def forget(redis, user_id: str) -> None:
    """The user's limits are sent again by the next sweep (the panel changed them, or a change may have failed)."""
    try:
        await redis.hdel(SYNCED_KEY, user_id)
    except Exception:  # noqa: BLE001 - the hourly full resync covers it
        log.warning("user limits: could not mark user %s for a resync", user_id, exc_info=True)


async def sweep(state) -> int:
    """Puts the effective limits of every user who has a key onto the LiteLLM user, for the ones that changed since the
    last sweep. Returns how many users were updated. LiteLLMUnavailable stops the sweep (the next one retries)."""
    async with state.sessionmaker() as db:
        defaults = await settings_store.get_many(db, ("key_rpm_limit", "key_tpm_limit"), state.settings)
        users = (
            await db.execute(
                select(User.id, User.rpm_limit, User.tpm_limit).where(User.id.in_(select(ApiKeyRef.user_id)))
            )
        ).all()
    redis = state.redis
    synced = await redis.hgetall(SYNCED_KEY)
    updated = 0
    for uid, own_rpm, own_tpm in users:
        rpm = int(own_rpm if own_rpm is not None else defaults["key_rpm_limit"])
        tpm = int(own_tpm if own_tpm is not None else defaults["key_tpm_limit"])
        value = f"{rpm}:{tpm}"
        if synced.get(str(uid)) == value:
            continue
        try:
            await state.litellm.update_user_limits(str(uid), rpm, tpm)
        except LiteLLMError as exc:
            log.warning("user limits: LiteLLM refused the limits of user %s: %s", uid, exc)
            continue
        await redis.hset(SYNCED_KEY, str(uid), value)
        if not synced and not updated:  # a new hash: it lives an hour, then everyone is sent again
            await redis.expire(SYNCED_KEY, SYNCED_TTL_S)
        updated += 1
    return updated
