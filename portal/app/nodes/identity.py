"""Per-node identity and approval (task SEC-1, finding H1 of the portal review, M7 of the infra review).

The agent token (one per cluster, on every PC of the class) only *joins*. The first registration of a name with it
(`POST /api/nodes/register` in standalone mode, `POST /api/nodes/enroll` in k3s mode) issues the node its own key: 32
random bytes, returned once, kept here only as a SHA-256 hash. The node keeps it root-only in its state directory
(`$STATE_DIR/node.key` = /var/lib/llm-node/node.key in the container, next to ca.crt) and sends it as `X-Node-Key` with
every later call for that name, besides the agent token. The agent token alone never updates or deletes a name that has
a key; it can only register a new name.

A new name waits for the admin's approval when the setting `node_approval_required` is on (the default): it may report
itself (heartbeat, metrics, NIC, image), but it gets no deployment, no LiteLLM backend, no backend key and no mirror
token until «Подтвердить» on the «Ноды» page.

Claims (fix rounds 1-2). A registration of a name the portal knows but that has no key — a node of before (legacy) from
an address the portal does not know for it or after the migration window, a name whose key the admin reset — does not
touch the name: it is kept as a *claim* (table node_claims) with a key of its own. The claim's key only heartbeats
(`approved: false`, no deployment, the registry record is not touched). A name keeps several claims (one per address, at
most MAX_CLAIMS, each for CLAIM_TTL_S), never first-wins: the admin compares each claim's address with the addresses the
portal knows for the name and picks one («Сделать нодой», never in the bulk approval) or rejects them. A claim refused at
the cap is written to the audit log (`node.claim_refused`). So a holder of the agent token can neither take a known name,
nor make it wait, nor push the real node's registration out of the admin's sight.

Stopping and deleting a node (`strict`) always need the node's own key of an approved name: never the agent token alone,
never a legacy call without a key, never a claim or a waiting node.

Nodes known before identities (migration 0011, `legacy`): for `node_key_migration_days` after the migration (the row's
creation) a call without a key is accepted from the address the portal knows for the node (its last address, the host of
its url, its reported NIC, the addresses of its Kubernetes node); the registration of the upgraded node from there gets a
key and keeps the approval. Past the window, or from anywhere else, the registration becomes a claim. «The same address»
is a weak proof: behind NAT, with main in Docker Desktop, or on a LAN where a laptop can take a switched-off PC's address
every node may share it. The safe path is a window of 0 days and «Подтвердить» for every upgraded node
(docs/deploy-class.md).

A lost reply (fix round 1): the node sends a random nonce of its own with a registration without a key
(`X-Node-Enroll-Nonce`). The same nonce again, within ENROLL_RETRY_S and while the key it was given has never been used,
gets a new key instead of 409 node_name_taken.

The node's backend key (llama-server --api-key) is derived from its key: sha256("nsu-llm-backend:" + key), hex. The
node computes it itself (registrar.sh writes $STATE_DIR/backend/key, which the k3s pods mount), the portal stores it
when it issues the key (LiteLLM needs it in clear), so one stolen node opens its own backends and nobody else's.
"""

import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, settings_store
from app.models import NodeClaim, NodeIdentity, NodeWake, StandaloneNode

log = logging.getLogger(__name__)

HEADER = "X-Node-Key"
NONCE_HEADER = "X-Node-Enroll-Nonce"
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
BACKEND_PREFIX = "nsu-llm-backend:"
APPROVAL_SETTING = "node_approval_required"
MIGRATION_SETTING = "node_key_migration_days"
ENROLL_RETRY_S = 900  # a registration whose reply got lost may be repeated (same nonce, key never used) this long
MAX_CLAIMS = 10  # claims one name keeps at once (one per address); one more is refused and written to the audit log
CLAIM_TTL_S = 7 * 86400  # a claim the admin did not pick in this time is dropped

# the codes of a 403 (detail.code): the node acts on them (registrar.sh drops a key the portal answers invalid)
REQUIRED = "node_key_required"  # the name has a key (or its migration window is over); the call came without one
INVALID = "node_key_invalid"  # the key does not match the name (reset by the admin, the node deleted, or a foreign key)
TAKEN = "node_name_taken"  # a registration without a key of a name that has one (or a claim waiting for the admin)
UNKNOWN = "node_unknown"  # no identity of that name: the node has to register first (register / enroll)
PENDING = "node_pending"  # the call needs an approved node (stop, delete; a claim may only heartbeat)

_MESSAGES = {
    REQUIRED: "У ноды «{name}» есть свой ключ (или закончился срок перехода на ключи), а запрос пришёл без него "
    "(X-Node-Key). Обновите скрипты ноды (run-node) вместе с образом или переустановите ноду.",
    INVALID: "Ключ ноды «{name}» не подходит: администратор сбросил его или удалил ноду. Нода зарегистрируется "
    "заново и будет ждать подтверждения на странице «Ноды».",
    TAKEN: "Имя «{name}» уже занято другой нодой со своим ключом или ждёт подтверждения. Если это та же машина, "
    "сбросьте ключ ноды на странице «Ноды» («Сбросить ключ ноды») и подтвердите её после повторной регистрации.",
    UNKNOWN: "Портал не знает ноду «{name}»: она регистрируется при старте своего контейнера (агентским токеном). "
    "Перезапустите ноду или обновите её образ.",
    PENDING: "Нода «{name}» ждёт подтверждения администратора (страница «Ноды», «Подтвердить»): до него ей доступен "
    "только heartbeat, а остановить или удалить ноду может только подтверждённая нода своим ключом или администратор.",
}


def refuse(code: str, name: str, status: int = 403) -> HTTPException:
    return HTTPException(status, {"code": code, "message": _MESSAGES[code].format(name=name)})


def new_key() -> str:
    return secrets.token_urlsafe(32)  # 43 characters of [A-Za-z0-9_-]


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def backend_key_of(key: str) -> str:
    """The node's llama-server key, derived from its own key (the node computes the same, registrar.sh)."""
    return hashlib.sha256((BACKEND_PREFIX + key).encode()).hexdigest()


def presented_key(request: Request) -> str | None:
    value = request.headers.get(HEADER, "").strip()
    return value or None


def presented_nonce(request: Request) -> str | None:
    value = request.headers.get(NONCE_HEADER, "").strip()
    return value if NONCE_RE.fullmatch(value) else None


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


@dataclass
class Checked:
    """The outcome of a node call's identity check: the row (None for an unknown name without a key, when the caller
    allows it), whether the node is approved, whether the caller holds a claim (its key only heartbeats), and the key
    issued by this call (registration only, returned to the node once)."""

    row: NodeIdentity | None
    approved: bool
    issued: str | None = None
    claim: bool = False


async def approval_required(state: Any) -> bool:
    return bool(await settings_store.live(state, APPROVAL_SETTING))


async def migration_days(state: Any) -> float:
    return float(await settings_store.live(state, MIGRATION_SETTING))


def is_approved(row: NodeIdentity | None, required: bool) -> bool:
    """A name without a row (a Kubernetes node that never registered, e.g. an old image) counts as approved only while
    approval is off; a row says it itself."""
    return row.approved if row is not None else not required


def migration_open(row: NodeIdentity, days: float, now: datetime | None = None) -> bool:
    """A legacy row without a key is still in its migration window (0 days: closed at once)."""
    if not row.legacy or row.key_hash is not None or days <= 0:
        return False
    created = row.created_at or datetime.now(UTC)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) < created + timedelta(days=days)


async def known_addresses(db: AsyncSession, row: NodeIdentity, k8s=None) -> set[str]:
    """The addresses the portal knows for the node: the last one it called from, the host of its url (standalone), its
    reported NIC, the addresses of its Kubernetes node."""
    found = {row.last_ip} if row.last_ip else set()
    node = await db.get(StandaloneNode, row.name)
    if node is not None:
        host = node.url.removeprefix("http://").rpartition(":")[0]
        if host:
            found.add(host)
    nic = await db.get(NodeWake, row.name)
    if nic is not None:
        found.add(nic.ip)
    if k8s is not None:
        try:
            for n in await k8s.list_nodes():
                if n.name.lower() == row.name:
                    found |= {a for a in (n.internal_ip, n.external_ip) if a}
        except Exception as exc:  # noqa: BLE001 - the cluster is only one of the sources
            log.info("node %s: its Kubernetes addresses are unknown: %s", row.name, exc)
    return {_norm(a) for a in found}


def _norm(address: str | None) -> str:
    try:
        return ipaddress.ip_address(address or "").compressed
    except ValueError:
        return (address or "").lower()


async def _from_known_address(request: Request, db: AsyncSession, row: NodeIdentity) -> bool:
    ip = audit.client_ip(request)
    return bool(ip) and _norm(ip) in await known_addresses(db, row, getattr(request.app.state, "k8s", None))


async def check(
    request: Request, db: AsyncSession, name: str, allow_unknown: bool = False, strict: bool = False, claim_ok: bool = False
) -> Checked:
    """The identity check of every node call but the registration.

    A presented key must be the name's key (403 node_key_invalid otherwise, also for a name the portal does not know), or
    the key of its claim when `claim_ok` (the heartbeat: a claim gets nothing but `approved: false`). Without a key: a
    name with a key is 403 node_key_required; a legacy name without a key passes from its known address while its
    migration window is open; an unknown name passes only when `allow_unknown` (the heartbeat answers it 404 so the node
    registers), else 403 node_unknown. `strict` (stop, delete): only the key of an approved name, else 403 node_pending
    (or node_key_required without a key)."""
    name = name.lower()
    state = request.app.state
    ip = audit.client_ip(request)
    row = await db.get(NodeIdentity, name)
    required = await approval_required(state)
    key = presented_key(request)
    if key is not None:
        digest = key_hash(key)
        if row is not None and _same(digest, row.key_hash):
            changed = False
            if row.key_used_at is None:  # the node has its key: a lost reply can no longer be repeated
                row.key_used_at, changed = datetime.now(UTC), True
            if ip and row.last_ip != ip:
                row.last_ip, changed = ip[:64], True
            if changed:
                await db.commit()
            if strict and not row.approved:
                raise refuse(PENDING, name)
            return Checked(row, row.approved)
        if row is not None and any(_same(digest, c.key_hash) for c in await live_claims(db, name)):
            if not claim_ok:
                raise refuse(PENDING, name)
            return Checked(row, False, claim=True)
        raise refuse(INVALID, name)
    if row is None:
        if allow_unknown and not strict:
            return Checked(None, False)
        raise refuse(UNKNOWN, name)
    if (
        not strict
        and migration_open(row, await migration_days(state))
        and await _from_known_address(request, db, row)
    ):
        if ip and row.last_ip != ip:
            row.last_ip = ip[:64]
            await db.commit()
        return Checked(row, is_approved(row, required))
    raise refuse(REQUIRED, name)


def _within_retry(at: datetime | None, now: datetime) -> bool:
    if at is None:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return now - at < timedelta(seconds=ENROLL_RETRY_S)


async def enroll(request: Request, db: AsyncSession, name: str) -> Checked:
    """The identity part of a registration (standalone register, k3s enroll).

    With a key: the same node again (or its claim). Without a key:
      - a new name gets a key (approved unless `node_approval_required`);
      - a name with a key: 409 node_name_taken, unless it is the same registration again (the client's nonce of the one
        that got the key, within ENROLL_RETRY_S, the key never used: its reply got lost), which gets a new key;
      - a name without a key: an approved legacy name in its migration window, or one the admin approved before it ever
        registered, gets the key from its known address and keeps the approval; anything else becomes a claim, kept apart
        from the name until the admin approves it (one claim at a time: another is 409 unless it is the same nonce).
    The new key is returned once (Checked.issued) and written to the audit log as `node.enroll` (without the key)."""
    name = name.lower()
    state = request.app.state
    ip = audit.client_ip(request)
    if presented_key(request) is not None:
        return await check(request, db, name, claim_ok=True)
    nonce = presented_nonce(request)
    nonce_digest = key_hash(nonce) if nonce else None
    required = await approval_required(state)
    row = await db.get(NodeIdentity, name, with_for_update=True)
    now = datetime.now(UTC)
    key = new_key()
    if row is None:
        row = NodeIdentity(name=name, approved=not required, legacy=False, created_at=now)
        if not required:
            row.approved_at, row.approved_by = now, "auto"
        db.add(row)
        how = "new"
    elif row.key_hash is not None:
        if _same(nonce_digest, row.key_nonce_hash) and row.key_used_at is None and _within_retry(row.key_issued_at, now):
            how = "repeated"  # the reply of the first registration got lost
        else:
            await db.rollback()
            raise refuse(TAKEN, name, 409)
    else:
        days = await migration_days(state)
        if row.legacy:
            in_window = row.approved and migration_open(row, days)
        else:  # approved by the admin before it ever registered (a Kubernetes node of an old image): the same window
            in_window = row.approved and days > 0 and _within(row.approved_at, now, days * 86400)
        if in_window and await _from_known_address(request, db, row):
            how = "migrated" if row.legacy else "approved before"
            row.legacy = False
            await db.execute(delete(NodeClaim).where(NodeClaim.name == name))
        else:
            return await _claim(request, db, row, key, nonce_digest, now)
    row.key_hash, row.backend_key, row.key_issued_at = key_hash(key), backend_key_of(key), now
    row.key_nonce_hash, row.key_used_at = nonce_digest, None
    row.last_ip = ip[:64] if ip else row.last_ip
    await audit.record(db, None, "node.enroll", "node", name, {"how": how, "approved": row.approved}, ip)
    try:
        await db.commit()
    except IntegrityError:  # a concurrent registration of the same new name won
        await db.rollback()
        raise refuse(TAKEN, name, 409) from None
    if not row.approved:
        log.warning("node %s registered from %s (%s): it waits for the admin's approval on the Nodes page", name, ip, how)
    return Checked(row, row.approved, key)


def _within(at: datetime | None, now: datetime, seconds: float) -> bool:
    if at is None:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return now - at < timedelta(seconds=seconds)


async def live_claims(db: AsyncSession, name: str) -> list[NodeClaim]:
    """The claims of a name the admin can still pick (younger than CLAIM_TTL_S), oldest first."""
    cutoff = datetime.now(UTC) - timedelta(seconds=CLAIM_TTL_S)
    rows = await db.scalars(
        select(NodeClaim).where(NodeClaim.name == name.lower(), NodeClaim.at >= cutoff).order_by(NodeClaim.at, NodeClaim.id)
    )
    return list(rows.all())


async def _claim(request: Request, db: AsyncSession, row: NodeIdentity, key: str, nonce_digest: str | None, now: datetime) -> Checked:
    """Keeps the registration as a claim of its own: the same registration again (its nonce) or another one from the same
    address replaces that address's claim; up to MAX_CLAIMS addresses, one more is refused (409) and audited."""
    name, ip = row.name, audit.client_ip(request)
    await db.execute(delete(NodeClaim).where(NodeClaim.name == name, NodeClaim.at < now - timedelta(seconds=CLAIM_TTL_S)))
    claims = await live_claims(db, name)
    mine = next((c for c in claims if _same(nonce_digest, c.nonce_hash)), None) or next(
        (c for c in claims if ip and c.ip == ip[:64]), None
    )
    if mine is None and len(claims) >= MAX_CLAIMS:
        await audit.record(db, None, "node.claim_refused", "node", name, {"claims": len(claims)}, ip)
        await db.commit()
        log.warning("node %s: a registration from %s refused, %d claims wait for the admin already", name, ip, len(claims))
        raise refuse(TAKEN, name, 409)
    if mine is None:
        mine = NodeClaim(name=name)
        db.add(mine)
        how = "claim"
    else:
        how = "claim repeated"
    mine.key_hash, mine.backend_key, mine.at = key_hash(key), backend_key_of(key), now
    mine.ip, mine.nonce_hash = (ip[:64] if ip else None), nonce_digest
    await audit.record(db, None, "node.enroll", "node", name, {"how": how, "approved": False, "claims": len(claims) + (how == "claim")}, ip)
    await db.commit()
    log.warning("node %s: a registration from %s is kept as a claim for the admin (%s)", name, ip, how)
    return Checked(row, False, key, claim=True)


async def approved_names(db: AsyncSession, names, required: bool) -> set[str]:
    """Which of `names` are approved (a name without a row: only while approval is off)."""
    names = {n.lower() for n in names}
    if not names:
        return set()
    rows = {r.name: r for r in (await db.scalars(select(NodeIdentity).where(NodeIdentity.name.in_(names)))).all()}
    return {n for n in names if is_approved(rows.get(n), required)}


async def backend_keys(db: AsyncSession) -> dict[str, str]:
    """name -> the node's own backend key, for every node that has one."""
    rows = (await db.scalars(select(NodeIdentity).where(NodeIdentity.backend_key.is_not(None)))).all()
    return {r.name: r.backend_key for r in rows}


class NotRegistered(LookupError):
    """«Подтвердить» for a name that never registered (no identity, no Kubernetes node), or that did not register again
    after its key was reset (its registration comes as a claim to pick)."""


class ClaimsPending(ValueError):
    """«Подтвердить» without a claim for a name that has claims: the admin has to pick one."""


class NoSuchClaim(LookupError):
    """The claim the admin picked is not (or no longer) there."""


async def approve(db: AsyncSession, name: str, by: str, k8s=None, claim_id: int | None = None) -> NodeIdentity:
    """Admin: the node may serve.

    - `claim_id`: that claim becomes the name's key (its holder is the node from now on); the other claims are rejected, and
      the registry record of the name is dropped, so the node registers again with its own url.
    - Without `claim_id`: a waiting new node (it has its key), or a Kubernetes node that never registered (an old image: a
      row without a key, its registration from its known address then keeps the approval for node_key_migration_days).
      A name with claims: ClaimsPending (pick one). A name without a key that is not such a Kubernetes node (after a reset:
      its registration comes as a claim): NotRegistered.
    The caller writes the audit row and commits."""
    name = name.lower()
    row = await db.get(NodeIdentity, name)
    now = datetime.now(UTC)
    claims = await live_claims(db, name)
    if claim_id is not None:
        claim = next((c for c in claims if c.id == claim_id), None)
        if claim is None:
            raise NoSuchClaim(name)
        if row is None:
            row = NodeIdentity(name=name, created_at=now)
            db.add(row)
        row.key_hash, row.backend_key, row.key_issued_at = claim.key_hash, claim.backend_key, claim.at
        row.key_nonce_hash, row.key_used_at = claim.nonce_hash, None
        row.last_ip = claim.ip or row.last_ip
        row.legacy = False
        await db.execute(delete(NodeClaim).where(NodeClaim.name == name))
        await db.execute(delete(StandaloneNode).where(StandaloneNode.name == name))
    elif claims:
        raise ClaimsPending(name)
    elif row is None:
        in_cluster = False
        if k8s is not None:
            try:
                in_cluster = any(n.name.lower() == name for n in await k8s.list_nodes())
            except Exception as exc:  # noqa: BLE001 - nothing to approve that the portal cannot see
                log.info("approve %s: the cluster does not answer: %s", name, exc)
        if not in_cluster:
            raise NotRegistered(name)
        row = NodeIdentity(name=name, created_at=now)
        db.add(row)
    elif row.key_hash is None and not row.approved:
        raise NotRegistered(name)  # reset and not registered again yet: its registration comes as a claim
    row.approved, row.approved_at, row.approved_by = True, now, by[:320]
    return row


async def reject_claims(db: AsyncSession, name: str, claim_id: int | None = None) -> list[NodeClaim]:
    """Admin «Отклонить»: one claim of the name, or all of them. The claimants' keys answer 403 node_key_invalid from now
    on. Returns the claims removed; the caller audits and commits."""
    claims = [c for c in await live_claims(db, name) if claim_id is None or c.id == claim_id]
    for c in claims:
        await db.delete(c)
    return claims


async def claims_by_name(db: AsyncSession) -> dict[str, list[NodeClaim]]:
    cutoff = datetime.now(UTC) - timedelta(seconds=CLAIM_TTL_S)
    out: dict[str, list[NodeClaim]] = {}
    for c in (await db.scalars(select(NodeClaim).where(NodeClaim.at >= cutoff).order_by(NodeClaim.at, NodeClaim.id))).all():
        out.setdefault(c.name, []).append(c)
    return out


async def reset(db: AsyncSession, name: str) -> bool:
    """Admin «Сбросить ключ ноды»: the key, the backend key, the claims and the approval go, also of a legacy node without
    a key; the node registers again (a claim, among any others: the admin picks, nobody wins a race) and waits. False when the portal knows no identity of that name. The
    caller writes the audit row and commits."""
    row = await db.get(NodeIdentity, name.lower())
    if row is None:
        return False
    row.key_hash = row.backend_key = row.key_issued_at = row.key_nonce_hash = row.key_used_at = None
    row.approved, row.approved_at, row.approved_by, row.legacy = False, None, None, False
    await db.execute(delete(NodeClaim).where(NodeClaim.name == row.name))
    return True


async def forget(db: AsyncSession, name: str) -> None:
    """The node is removed (app.nodes.routes.remove_node): its identity goes with it. Does not commit."""
    await db.execute(delete(NodeClaim).where(NodeClaim.name == name.lower()))
    await db.execute(delete(NodeIdentity).where(NodeIdentity.name == name.lower()))
