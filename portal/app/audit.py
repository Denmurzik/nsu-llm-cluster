"""Audit log of the admin panel: who did what to what (table `audit_log`).

`record` only adds the row to the session: the caller commits it together with its own change, so the log never
shows an action that was rolled back, and a change never goes unlogged when the commit succeeds. A service function
that commits by itself takes an `audit` hook (`Hook`) and awaits it just before its own commit, so the row and the
change are one transaction; the admin handlers pass a closure that calls `record`.

Actions are dotted `<object>.<verb>`: deployment.create, deployment.update, deployment.delete, deployment.refresh_revision,
node.assign, node.maintenance, node.wake, node.update, node.delete, node.purge_models, user.role, user.block,
key.revoke, setting.update, cache.delete, cache.prefetch, backup.run, local_model.upload, local_model.delete.
`target_type` names the kind of `target` (deployment, node, user, key, setting, cache, backup, local_model).
"""

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, User

# One audit row is added by `Hook(obj)`: the service passes the object it changed (deployment, assignment, ...).
Hook = Callable[[Any], Awaitable[None]]

# A key of `details` that names a secret is dropped (case-insensitive; - and _ alike): secret, password, token (but
# not tokens), authorization, api_key, key_value, a bare `key` and anything ending in `_key`. Names such as max_keys,
# rpm_limit, tpm_limit or key_id are not secrets and stay.
SENSITIVE = re.compile(r"secret|password|passwd|authorization|api_?key|key_value|token(?!s)|(?:^|_)key$")
MAX_STR = 500  # longer text (a system prompt, a command line) is cut: the log says what changed, not the whole value
_LOOPBACK = frozenset({"127.0.0.1", "::1"})


def _clean(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else value[: MAX_STR - 1] + "…"
    if isinstance(value, Mapping):
        return scrub(value)
    if isinstance(value, list | tuple | set | frozenset):
        items = sorted(value, key=str) if isinstance(value, set | frozenset) else value
        return [_clean(v) for v in items]
    return _clean(str(value))


def scrub(details: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """`details` without the keys that may hold a secret (secret, token, key, password in the name; nested too),
    with every value made JSON-safe and long text cut. None stays None."""
    if details is None:
        return None
    return {
        str(k): _clean(v)
        for k, v in details.items()
        if not SENSITIVE.search(str(k).lower().replace("-", "_"))
    }


async def record(
    db: AsyncSession,
    actor: User | None,
    action: str,
    target_type: str,
    target: str,
    details: dict | None = None,
    ip: str | None = None,
) -> None:
    """Adds an audit row to the session, without committing. `actor` None: the system itself (a controller)."""
    db.add(
        AuditLog(
            actor_id=actor.id if actor is not None else None,
            actor_email=actor.email[:320] if actor is not None else None,
            action=action[:64],
            target_type=target_type[:32],
            target=str(target)[:255],
            details=scrub(details) or None,
            ip=ip[:64] if ip else None,
        )
    )


def client_ip(request: Request) -> str | None:
    """The address the request came from. Behind the Caddy of the main server the peer is the loopback and the
    client is the first entry of X-Forwarded-For; a peer that is not the loopback is taken as it is (the header
    could be forged by it)."""
    peer = request.client.host if request.client else None
    if peer in _LOOPBACK:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return peer
