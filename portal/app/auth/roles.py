"""Who is an admin and who may log in at all, from the OIDC claims and the settings (task SEC-1, rules 5 and 6).

The admin role (for a user whose role is not pinned in the panel) comes from either list:
  - ADMIN_SUBS: the OIDC subject, `<sub>` or `<issuer>|<sub>`. A provider issues a subject to one account only, so this
    is the robust way, also for a provider that lets users set any e-mail address.
  - ADMIN_EMAILS: the e-mail address, and with ADMIN_REQUIRE_EMAIL_VERIFIED=true (the default) only when the provider
    said `email_verified: true` at the last login. An absent claim is not a confirmation: a provider that does not send
    it (Microsoft Entra multi-tenant, small IdPs) may let anyone type the admin's address.
NSU ID sends `email_verified: false` for everyone, so there ADMIN_REQUIRE_EMAIL_VERIFIED=false is needed for
ADMIN_EMAILS to work at all; ADMIN_SUBS does not depend on it.

Login itself: OIDC_ALLOWED_DOMAINS (domains of the address, a subdomain counts) and OIDC_REQUIRED_CLAIM (`name=value`),
both empty by default (everyone the provider authenticates). The domain check is an e-mail decision too, so it follows
ADMIN_REQUIRE_EMAIL_VERIFIED the same way.
"""

import logging
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)


def verified(claims: dict[str, Any]) -> bool:
    """The provider confirmed the address: the claim is the JSON `true` (some send the string "true")."""
    value = claims.get("email_verified")
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _issuer(settings: Settings) -> str:
    return settings.oidc_issuer.strip().rstrip("/")


def listed_by_sub(sub: str, settings: Settings) -> bool:
    if not sub:
        return False
    subs = settings.admin_sub_set
    if sub in subs:
        return True
    issuer = _issuer(settings)
    return any(e.rpartition("|")[0].rstrip("/") == issuer and e.rpartition("|")[2] == sub for e in subs if "|" in e)


WARNED_MAX = 1024
_warned: set[str] = set()  # addresses warned about in this process (the role is recomputed on every request)


def listed_by_email(email: str, email_verified: bool | None, settings: Settings) -> bool:
    if not email or email.lower() not in settings.admin_email_set:
        return False
    if email_verified is True or not settings.admin_require_email_verified:
        return True
    _warn_unverified(email.lower(), email_verified)
    return False


def _warn_unverified(email: str, email_verified: bool | None) -> None:
    """An address of ADMIN_EMAILS gets no admin role because the provider did not confirm it (after the upgrade to 0012 every
    user is `unknown` until the next login). Said loudly once per address and process, with the two ways out: the admin of
    an installation must never just lose the role without a word in the log."""
    if email in _warned:
        return
    if len(_warned) >= WARNED_MAX:
        _warned.clear()
    _warned.add(email)
    state = "unknown (logged in before this version: log in again)" if email_verified is None else "not confirmed by the provider"
    log.warning(
        "%s is in ADMIN_EMAILS but gets no admin role: email_verified is %s and ADMIN_REQUIRE_EMAIL_VERIFIED=true. "
        "Log in again, or name the admin by its OIDC subject in ADMIN_SUBS, or set ADMIN_REQUIRE_EMAIL_VERIFIED=false "
        "(a provider that never sends email_verified=true, e.g. NSU ID)",
        email,
        state,
    )


def listed_admin(user, settings: Settings) -> bool:
    """`user` (a User row) is an admin by ADMIN_SUBS or ADMIN_EMAILS right now (the role pinned in the panel aside)."""
    return listed_by_sub(user.sub, settings) or listed_by_email(user.email, user.email_verified, settings)


def auto_role(user, settings: Settings) -> str:
    return "admin" if listed_admin(user, settings) else "user"


def _domain_ok(email: str, domains: frozenset[str]) -> bool:
    domain = email.rpartition("@")[2].strip().lower().rstrip(".")
    if not email.count("@") or not domain:
        return False
    return any(domain == d or domain.endswith("." + d) for d in domains)


def _claim_ok(claims: dict[str, Any], name: str, value: str) -> bool:
    got = claims.get(name)
    if isinstance(got, list | tuple | set):
        return any(_scalar(v) == value for v in got)
    return got is not None and _scalar(got) == value


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def login_refusal(claims: dict[str, Any], settings: Settings) -> str | None:
    """Why this account may not log in (for the log, not the user), or None when it may."""
    domains = settings.allowed_domain_set
    if domains:
        email = str(claims.get("email") or "")
        if not _domain_ok(email, domains):
            return "the e-mail domain is not in OIDC_ALLOWED_DOMAINS"
        if settings.admin_require_email_verified and not verified(claims):
            return "the e-mail address is not verified (OIDC_ALLOWED_DOMAINS with ADMIN_REQUIRE_EMAIL_VERIFIED=true)"
    required = settings.required_claim
    if required is not None and not _claim_ok(claims, *required):
        return f"the claim {required[0]!r} does not carry the value OIDC_REQUIRED_CLAIM asks for"
    return None
