import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(**kw: Any):
    return mapped_column(DateTime(timezone=True), **kw)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role in ('user', 'admin')", name="ck_users_role"),
        CheckConstraint("role_source in ('auto', 'manual')", name="ck_users_role_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sub: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    username: Mapped[str] = mapped_column(String(255), default="")
    full_name: Mapped[str] = mapped_column(String(512), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")
    created_at: Mapped[datetime] = _ts(default=_now, server_default=func.now())
    last_login_at: Mapped[datetime] = _ts(default=_now, server_default=func.now())
    # auto: the role comes from ADMIN_EMAILS at every login; manual: set in the panel and kept (task 2-31a)
    role_source: Mapped[str] = mapped_column(String(8), default="auto", server_default="auto")
    # a blocked user cannot log in, their sessions stop working at once (their keys are revoked by task 2-32)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    blocked_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # per-user overrides of the global limits (app.settings_store); None: the global setting
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_keys: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WebSession(Base):
    __tablename__ = "web_sessions"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    id_token: Mapped[str] = mapped_column(Text, default="")
    csrf_token: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _ts(default=_now, server_default=func.now())
    expires_at: Mapped[datetime] = _ts()


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(255), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(255))
    code_verifier: Mapped[str] = mapped_column(String(255))
    return_to: Mapped[str] = mapped_column(String(2048), default="/")
    created_at: Mapped[datetime] = _ts(default=_now, server_default=func.now())


class ApiKeyRef(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    litellm_token: Mapped[str] = mapped_column(String(255), unique=True)
    alias: Mapped[str] = mapped_column(String(255))
    masked: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts(default=_now, server_default=func.now())
    expires_at: Mapped[datetime | None] = _ts(nullable=True)
    is_playground: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class ModelDeployment(Base):
    __tablename__ = "model_deployments"
    __table_args__ = (
        CheckConstraint("engine in ('llamacpp', 'vllm')", name="ck_model_deployments_engine"),
        CheckConstraint("mode in ('replicas', 'split')", name="ck_model_deployments_mode"),
        CheckConstraint("accel in ('cpu', 'nvidia', 'any')", name="ck_model_deployments_accel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    engine: Mapped[str] = mapped_column(String(16), default="llamacpp")
    repo: Mapped[str] = mapped_column(String(512))
    file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Hugging Face commit of `repo` the nodes download (resolve/<sha>), pinned when the deployment is created or its
    # model changes; None: `main` (deployments made before task 2-15, own models local/*)
    revision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="replicas")
    group_size: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    accel: Mapped[str] = mapped_column(String(16), default="any")
    ctx_per_request: Mapped[int] = mapped_column(Integer)
    parallel: Mapped[int] = mapped_column(Integer)
    auto_assign: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # split: nodes kept out of the groups, to rebuild a group that lost a member
    spare_nodes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # seconds a lost node keeps its place (label, split group); None: the default of the mode
    regroup_grace_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # working window (app.deployments.schedule); None: around the clock
    schedule: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # split on nvidia: the rpc servers also offer the node's RAM (`-d CUDA0,CPU`); cpu nodes always use it
    use_node_ram: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # share of the node's RAM (label llm.nsu/ram-mib) the split model may take
    ram_budget_pct: Mapped[int] = mapped_column(Integer, default=70, server_default="70")
    # llama.cpp settings (task 2-22). All of them except `system_prompt` and `experimental` change the pods (spec hash).
    reasoning: Mapped[str] = mapped_column(String(8), default="off", server_default="off")  # off | on | auto
    # KV cache type: rows made before the setting keep f16 (server default), a new deployment gets q8_0
    kv_cache_type: Mapped[str] = mapped_column(String(8), default="q8_0", server_default="f16")  # f16 | q8_0 | q4_0
    # sampling defaults (temperature, top_p, top_k, min_p, repeat_penalty); a client may override them
    sampling: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # draft model of speculative decoding: a repository (local/<model> for own files), its file and pinned commit
    draft_repo: Mapped[str | None] = mapped_column(String(512), nullable=True)
    draft_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    draft_revision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mmproj_file: Mapped[str | None] = mapped_column(String(512), nullable=True)  # images; a file of the repo or local/...
    extra_args: Mapped[str | None] = mapped_column(String(2000), nullable=True)  # own llama-server flags
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)  # gateway side: added when the client sent none
    experimental: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # visible to admins only
    created_at: Mapped[datetime] = _ts(default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = _ts(default=_now, onupdate=_now, server_default=func.now())


class NodeAssignment(Base):
    __tablename__ = "node_assignments"
    __table_args__ = (CheckConstraint("source in ('k8s', 'standalone')", name="ck_node_assignments_source"),)

    node_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    deployment_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_deployments.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(16))
    manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # on maintenance: the node runs nothing and is not auto-assigned; the assignment is kept for its return
    maintenance: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    updated_at: Mapped[datetime] = _ts(default=_now, onupdate=_now, server_default=func.now())


class PortalFlag(Base):
    """One-time facts of the installation (e.g. the seed deployment was created) that must outlive deletions."""

    __tablename__ = "portal_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = _ts(default=_now, server_default=func.now())


class ControllerTimer(Base):
    """A pause the workload controller is waiting out (a NotReady node keeping its label, a split group waiting for a
    lost member), kept here so a restart of the controllers does not start the pause over.

    `key` is "notready:<node>" or "lost:<deployment>:<group index>"; `since` is wall-clock seconds (time.time()).
    """

    __tablename__ = "controller_timers"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    since: Mapped[float] = mapped_column(Float)
    members: Mapped[str] = mapped_column(Text, default="", server_default="")  # lost: the group's members
    leader_lost: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class LocalModelFile(Base):
    """A checked GGUF file of an own model (models/local/<model>/<path> on the mirror's volume), as the last scan
    of the folder or the verification of an upload saw it. The files themselves are the truth: a row whose file is
    gone is dropped by the next scan."""

    __tablename__ = "local_model_files"
    __table_args__ = (
        UniqueConstraint("model", "path", name="uq_local_model_files_model_path"),
        CheckConstraint("origin in ('upload', 'folder')", name="ck_local_model_files_origin"),
        CheckConstraint("status in ('ready', 'invalid')", name="ck_local_model_files_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model: Mapped[str] = mapped_column(String(96))
    path: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mtime: Mapped[float] = mapped_column(Float)  # of the file when it was checked: a change means a new check
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)  # known for uploads only
    origin: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str] = mapped_column(String(512), default="", server_default="")
    # GGUF metadata of a first or only file (the stored deployment meta without sizes); None for later shards
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = _ts(default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = _ts(default=_now, onupdate=_now, server_default=func.now())


UPLOAD_ACTIVE = ("uploading", "verifying")


class ModelUpload(Base):
    """A resumable upload of one file into an own model: parts arrive in order into .uploads/<id>.part, `finish`
    hands it to the controllers, which check sha256 and the GGUF header and rename it into models/local/<model>/."""

    __tablename__ = "model_uploads"
    __table_args__ = (
        CheckConstraint(
            "status in ('uploading', 'verifying', 'done', 'failed', 'cancelled')", name="ck_model_uploads_status"
        ),
        # one active upload per file: a second create of the same file resumes the first
        Index(
            "uq_model_uploads_active", "model", "path", unique=True,
            postgresql_where=text("status in ('uploading', 'verifying')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model: Mapped[str] = mapped_column(String(96))
    path: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    received_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    sha256_expected: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(16), default="uploading")
    error: Mapped[str] = mapped_column(String(512), default="", server_default="")
    created_by: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = _ts(default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = _ts(default=_now, onupdate=_now, server_default=func.now())


class StandaloneNode(Base):
    __tablename__ = "standalone_nodes"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    url: Mapped[str] = mapped_column(String(2048))
    accel: Mapped[str] = mapped_column(String(16))
    vram_mib: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ram_mib: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    version: Mapped[str] = mapped_column(String(64), default="")
    applied_spec_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    registered_at: Mapped[datetime] = _ts(default=_now, server_default=func.now())
    last_seen_at: Mapped[datetime] = _ts(default=_now, server_default=func.now())
    # set by a clean stop of the node (POST /api/nodes/<name>/stop): out of routing at once, the record and its NIC for
    # Wake-on-LAN stay (only DELETE, i.e. -Uninstall, forgets the PC); cleared by its next registration or heartbeat
    stopped_at: Mapped[datetime | None] = _ts(nullable=True)


class NodeWake(Base):
    """The physical NIC a node reported for Wake-on-LAN (app.nodes.wake): POST /api/nodes/wake in k3s mode, the
    heartbeat in standalone mode. Removed with the node (DELETE /api/nodes/<name>, -Uninstall): an uninstalled PC is
    never woken."""

    __tablename__ = "node_wake"

    node_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    mac: Mapped[str] = mapped_column(String(17))  # aa:bb:cc:dd:ee:ff
    ip: Mapped[str] = mapped_column(String(15))  # the NIC's IPv4 address
    prefix: Mapped[int] = mapped_column(Integer)  # of its subnet: the directed broadcast is computed from both
    updated_at: Mapped[datetime] = _ts(default=_now, onupdate=_now, server_default=func.now())


class AuditLog(Base):
    """Who did what in the admin panel (app.audit.record). The actor's email is copied: the row outlives the user."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = _ts(default=_now, server_default=func.now(), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)  # dotted: deployment.create, user.block, ...
    target_type: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # never secrets (audit.scrub)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SystemSetting(Base):
    """A runtime setting changed in the panel (app.settings_store); a missing row means the env default."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = _ts(default=_now, onupdate=_now, server_default=func.now())
    updated_by: Mapped[str | None] = mapped_column(String(320), nullable=True)


class MetricSample(Base):
    """One monitoring sample of a node or a model deployment (task 2-33, which also enforces the retention)."""

    __tablename__ = "metric_samples"
    __table_args__ = (
        CheckConstraint("kind in ('node', 'model')", name="ck_metric_samples_kind"),
        Index("ix_metric_samples_kind_name_at", "kind", "name", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = _ts(default=_now)
    kind: Mapped[str] = mapped_column(String(16))  # node | model
    name: Mapped[str] = mapped_column(String(255))  # the node or the deployment
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
