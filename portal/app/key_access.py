"""Which LiteLLM models a user's keys reach: `public` for everyone, `experimental` for admins as well.

LiteLLM keys list access group names in `models` (`models_for_role`); every portal-managed model is in the group
`public` or `experimental` (`model_info.access_groups`, set by the backend controller). A key made before this
feature has `models: []`, which means every model, so the existing keys are brought in line the same way as a key
whose owner changed role: the controllers process calls `sweep` every round.

`portal_flags` keeps one row per user, `keyaccess:<user id>` = the role the user's keys were last set for. A user whose
current role differs (or has no row: the one-time migration of old keys) gets all their keys updated, the row follows,
so a second round changes nothing. The role is the one `current_user` computes from ADMIN_EMAILS, so a user taken off the
list loses the experimental models within one controller interval even if they never open the portal again.
Admins that lost the role go first: cutting access is more urgent than giving it.
"""

import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.litellm_client import LiteLLMError, LiteLLMUnavailable, models_for_role
from app.models import ApiKeyRef, PortalFlag, User

log = logging.getLogger(__name__)

FLAG_PREFIX = "keyaccess:"
BATCH = 200  # users per round; the rest wait for the next one
_LOG_EVERY_S = 60.0
_last_warned = -_LOG_EVERY_S


def effective_role(user: User, settings: Settings) -> str:
    """The role `current_user` gives the user right now (the ADMIN_EMAILS list can have changed since the login)."""
    if user.role_source == "manual":
        return user.role  # set in the panel: ADMIN_EMAILS does not touch it
    listed = user.email.lower() in settings.admin_email_set
    if user.role == "admin" and not listed:
        return "user"
    if user.role == "user" and listed and not settings.admin_require_email_verified:
        return "admin"
    return user.role


async def sync_user_keys(db: AsyncSession, litellm, user: User, role: str) -> bool:
    """Sets every key of the user (the hidden Playground one too) for `role`; False when the user has no keys."""
    tokens = list((await db.scalars(select(ApiKeyRef.litellm_token).where(ApiKeyRef.user_id == user.id))).all())
    await db.commit()  # no transaction stays open across the LiteLLM calls
    if not tokens:
        return False
    await litellm.update_key_models(tokens, models_for_role(role))
    return True


async def sweep(state) -> int:
    """One round; returns the number of users whose keys were updated. LiteLLM errors end the round quietly."""
    global _last_warned
    async with state.sessionmaker() as db:
        users = (await db.scalars(select(User).where(User.id.in_(select(ApiKeyRef.user_id))))).all()
        flags = {
            f.key: f.value
            for f in (await db.scalars(select(PortalFlag).where(PortalFlag.key.like(FLAG_PREFIX + "%")))).all()
        }
        await db.commit()
        todo = []
        for user in users:
            role = effective_role(user, state.settings)
            known = flags.get(FLAG_PREFIX + str(user.id))
            if known != role:
                todo.append((known != "admin", str(user.id), user, role))  # demotions first
        done = 0
        for _, _, user, role in sorted(todo, key=lambda t: t[:2])[:BATCH]:
            try:
                await sync_user_keys(db, state.litellm, user, role)
            except (LiteLLMUnavailable, LiteLLMError) as exc:
                now = time.monotonic()
                if now - _last_warned >= _LOG_EVERY_S:
                    _last_warned = now
                    log.warning("key access: cannot update the keys of %s, retrying next round: %s", user.id, exc)
                if isinstance(exc, LiteLLMUnavailable):
                    break
                continue
            flag = await db.get(PortalFlag, FLAG_PREFIX + str(user.id))
            if flag is None:
                db.add(PortalFlag(key=FLAG_PREFIX + str(user.id), value=role))
            else:
                flag.value = role
            await db.commit()
            done += 1
        return done
