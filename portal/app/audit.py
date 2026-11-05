"""Audit log of the admin panel: who did what to what (table `audit_log`).

`record` only adds the row to the session: the caller commits it together with its own change, so the log never
shows an action that was rolled back, and a change never goes unlogged when the commit succeeds. A service function
that commits by itself takes an `audit` hook (`Hook`) and awaits it just before its own commit, so the row and the
change are one transaction; the admin handlers pass a closure that calls `record`.

Actions are dotted `<object>.<verb>`: deployment.create, deployment.update, deployment.delete, deployment.refresh_revision,
node.assign, node.maintenance, node.wake, node.update, node.delete, node.purge_models, user.role, user.block,
user.unblock, user.limits, key.revoke, setting.update, cache.delete, cache.prefetch, backup.run, backup.download,
backup.delete, local_model.upload, local_model.delete.
`target_type` names the kind of `target` (deployment, node, user, key, setting, cache, backup, local_model).
"""

import ipaddress
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, User

# One audit row is added by `Hook(obj)`: the service passes the object it changed (deployment, assignment, ...).
Hook = Callable[[Any], Awaitable[None]]

# A key of `details` that names a secret is dropped (case-insensitive; - and _ alike): secret, password, token,
# authorization, api_key, key_value, a bare `key`, anything ending in `_key`, and the plurals that hold lists of LiteLLM
# key secrets: `tokens`, `keys`, `*_tokens`, `*_keys`. Names such as max_keys, max_tokens, tokens_30d, rpm_limit,
# tpm_limit or key_id are not secrets and stay.
SENSITIVE = re.compile(
    r"secret|password|passwd|authorization|api_?key|key_value|token(?!s)|^(?:tokens|keys)$"
    r"|(?<!max)_(?:tokens|keys)$|(?:^|_)key$"
)
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


def _ip(value: str) -> str | None:
    try:
        return ipaddress.ip_address(value.strip()).compressed
    except ValueError:
        return None


def client_ip(request: Request) -> str | None:
    """The address the request came from (finding L3), never one a client could write itself.

    The portal listens on 127.0.0.1 behind main's Caddy, which appends the address of its own peer to X-Forwarded-For
    (and keeps what a TRUSTED_PROXIES front proxy sent before it). uvicorn already replaces the loopback peer with that
    last entry. From there the entries are walked from the right only while the address in hand is a trusted front proxy
    (TRUSTED_PROXIES, `settings.trusted_proxies`): the first other address is the client. The first entry of the header
    is never taken as such: in front of a trusted proxy it is whatever the client wrote."""
    peer = request.client.host if request.client else None
    entries = [e for e in (_ip(x) for x in request.headers.get("x-forwarded-for", "").split(",")) if e]
    if peer is None:
        return None
    if entries and (peer in _LOOPBACK or entries[-1] == _ip(peer)):
        peer = entries.pop()  # the address Caddy saw (uvicorn has put it in place of the loopback already)
    elif peer in _LOOPBACK:
        return peer
    trusted = _trusted(request)
    while entries and _in(peer, trusted):
        peer = entries.pop()
    return peer


def _trusted(request: Request) -> tuple:
    settings = getattr(getattr(request.app, "state", None), "settings", None) if "app" in request.scope else None
    return getattr(settings, "trusted_proxy_networks", ()) if settings is not None else ()


def _in(address: str, networks: tuple) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip.version == n.version and ip in n for n in networks)
