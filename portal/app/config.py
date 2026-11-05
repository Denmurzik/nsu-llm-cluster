import hashlib
import ipaddress
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.nodes import wake


def key_fingerprint(key: str) -> str:
    """Stable, non-reversible tag of the backend key (sha256, 12 hex chars); empty for no key."""
    return hashlib.sha256(key.encode()).hexdigest()[:12] if key else ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "postgresql+asyncpg://portal:portal@127.0.0.1:5432/portal"
    db_pool_size: int = Field(default=10, ge=1)  # per process; see main/render_manifests.py for the budget
    db_max_overflow: int = Field(default=20, ge=0)
    # shared by API workers and the controllers process: playground keys, backend status
    redis_url: str = "redis://127.0.0.1:6379/0"
    public_hostname: str = "double.fresh.nsuts.ru"
    main_advertise_ip: str = ""
    # main serves an own certificate (run-main --certs, entrypoint.sh sets it from TLS_CERT): it need not cover the IP
    main_own_cert: bool = False

    # no defaults: the OIDC provider is the operator's choice. Empty issuer or client id: «Войти» answers with a clear
    # error (auth/routes.py) and the portal logs it at start; nothing falls back to a provider of any institution
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    # how the UI names the provider on the login button and in the users page («Войти через <name>»)
    login_provider_name: str = "SSO"
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_scopes: str = "openid profile email"
    # Bridge through a front of another domain (the NSU example): the client's redirect_uri is registered for that domain,
    # whose front answers 302 to https://<public_hostname>/auth/callback with the same query. Empty: own callback.
    oidc_redirect_uri: str = ""
    # where the provider sends the browser after logout (must also be allowed for the client); empty: https://<public_hostname>/
    oidc_post_logout_redirect_uri: str = ""
    admin_emails: str = ""
    # true: every decision made by the e-mail address (the admin role from ADMIN_EMAILS, OIDC_ALLOWED_DOMAINS) needs the
    # provider's email_verified to be true; an absent claim counts as not verified. false: the address alone decides (NSU ID
    # sends email_verified=false, so it needs false, and then ADMIN_SUBS is the safer way to name the admins)
    admin_require_email_verified: bool = True
    # admins by the OIDC `sub` (or `<issuer>|<sub>`), comma separated: a provider cannot hand anyone else that subject,
    # whatever e-mail address it lets a user set
    admin_subs: str = ""
    # who may log in at all; empty: everyone the provider authenticates. Domains of the e-mail address, comma separated
    # (`nsu.ru` also admits `g.nsu.ru`), and/or one claim that must carry a value: `name=value` (a list claim such as
    # `groups` must contain the value). Both set: both must hold
    oidc_allowed_domains: str = ""
    oidc_required_claim: str = ""
    # front proxies before main's Caddy (the same TRUSTED_PROXIES Caddy gets): addresses or networks, comma separated. The
    # audit log walks X-Forwarded-For from the right past them to the client (app.audit.client_ip); empty: no front proxy
    trusted_proxies: str = ""
    session_ttl_hours: int = 12
    # absolute lifetime of a web session whatever its use (the runtime setting `session_max_days` overrides it)
    session_max_days: int = Field(default=30, ge=1, le=365)
    cookie_name: str = "nsullm_session"
    cookie_secure: bool = True

    litellm_url: str = "http://127.0.0.1:4000"
    litellm_master_key: SecretStr = SecretStr("")
    max_keys_per_user: int = 5
    key_rpm_limit: int = 60
    key_tpm_limit: int = 100000
    # new API keys one user may create per hour (POST /api/keys; deleting and re-creating keys is not a way around
    # the limits anyway, those hold for the user as a whole, app.user_limits)
    key_create_per_hour: int = Field(default=20, ge=1)
    # Playground (app.api.playground): requests of one user in flight at once, and the request bodies one API worker
    # holds in memory at once (MiB); over them the portal answers 429 / 503 before reading the body
    playground_max_inflight: int = Field(default=2, ge=1)
    playground_body_budget_mib: int = Field(default=128, ge=21)
    # every backend takes at most parallel x queue_factor requests (its slots plus a bounded queue); LiteLLM
    # counts the limit per worker process, so each worker gets 1/litellm_workers of it, but never less than
    # parallel (one worker can fill every slot; LiteLLM answers 429 at once). 2 (one queued request per
    # slot): in the 1000-user load test 8 let a slow CPU backend queue requests past the clients' 120 s limit
    queue_factor: int = Field(default=2, ge=1)
    litellm_workers: int = Field(default=4, ge=1)
    # concurrent chat streams one portal process keeps open to LiteLLM
    litellm_stream_connections: int = Field(default=1000, ge=1)
    # the backend controller does not repeat its own writes for this long (stale LiteLLM worker listings)
    backend_recent_window_s: float = Field(default=30.0, ge=0)
    # how long /api/models and /api/public/status reuse the ready-backend counts (0 turns the cache off)
    status_cache_ttl_s: float = Field(default=5.0, ge=0)
    # how long the code reuses the runtime settings of the panel (settings_store.RuntimeSettings); 0: read every time
    settings_cache_ttl_s: float = Field(default=5.0, ge=0)

    # nodes join with the agent token (k3s --agent-token); the k3s server token never leaves the main server
    agent_token: SecretStr = SecretStr("")
    # written by k3s in the secure form K10<ca hash>::node:<agent token>, shown on the Nodes page
    agent_token_file: str = "/var/lib/rancher/k3s/server/agent-token"
    # the k3s server token; the node API accepts it for one release from nodes installed before the agent token
    cluster_token: SecretStr = SecretStr("")
    # off by default (infra finding L3): main's entrypoint sets it true only for an install upgraded from before the agent token
    node_legacy_token: bool = False
    # a node registered under a new name waits for the admin's approval on the «Ноды» page before it gets a deployment,
    # keys or a LiteLLM backend (app.nodes.identity); the runtime setting of the same name overrides it
    node_approval_required: bool = True
    # nodes known before per-node keys (migration 0011) may call without a key, from their known address, this many days
    # after the upgrade; then (or at once with 0) every one of them needs «Подтвердить» (app.nodes.identity)
    node_key_migration_days: int = Field(default=7, ge=0, le=365)
    # the shared key of inference backends before per-node keys (task SEC-1): only nodes that have no key of their own
    # yet (old standalone images, app.nodes.identity) still get it; every other backend has its own key
    backend_api_key: SecretStr = SecretStr("")
    kubeconfig: str = "/etc/rancher/k3s/k3s.yaml"
    k8s_namespace: str = "llm"
    node_image_repo: str = "nsu-llm-node"
    node_image_tag: str = "0.2.0"
    # Versions and updates of the nodes (task 2-30, app.nodes.versions). The pods run the node image pinned by the digest
    # that main's registry reports for the tag, so a new image under the same tag reaches the nodes by itself; when the
    # registry has no such tag they keep the tag with imagePullPolicy Always. False: the tag and IfNotPresent as before.
    node_image_pin: bool = True
    # image registry on main (REGISTRY_PORT of entrypoint.sh, 127.0.0.1 of the main container); REGISTRY_URL replaces the address
    registry_port: int = Field(default=5000, ge=1, le=65535)
    registry_url: str = ""
    # false: a node container is replaced only when the admin asks for it on the «Ноды» page; true: also by itself, but only
    # while the node serves nothing, whenever main's registry offers another image than the node runs
    node_auto_update: bool = False
    # after a failed update the node is not told to try again for this long (an admin's new request resets it)
    node_update_retry_s: int = Field(default=900, ge=0)
    # this main's own version: baked into the image by main/Dockerfile (ARG VERSION, GIT_COMMIT); the digest of the image
    # is known to the one who started the container (run-main.sh) and comes as MAIN_IMAGE_DIGEST
    nsu_llm_version: str = ""
    nsu_llm_revision: str = ""
    main_image_digest: str = ""
    infer_port: int = 18080
    rpc_port: int = 18050

    hf_endpoint: str = "https://huggingface.co"  # upstream of the mirror; the portal itself only with the mirror off
    hf_token: SecretStr = SecretStr("")
    # caching Hugging Face mirror on main (pod hf-mirror, main/hf_mirror.py): the inference pods download through it
    # with HF_MIRROR_TOKEN, the real token stays on main. False: the pods go to Hugging Face directly with HF_TOKEN.
    hf_mirror_enabled: bool = True
    # address of the mirror for the nodes; empty: https://<main_advertise_ip>/hf (https://<public_hostname>/hf without the IP
    # or with an own certificate)
    hf_mirror_url: str = ""
    # size limit of the mirror's cache in GiB (HF_MIRROR_MAX_GIB, which the mirror pod reads itself); the portal knows it
    # as the default of the runtime setting `hf_mirror_max_gib`
    hf_mirror_max_gib: int = Field(default=500, ge=1)
    hf_mirror_port: int = 8090  # the mirror listens on 127.0.0.1:<port> of main (progress report for the portal)
    # the nodes' side of the mirror: never handed out as it is any more, only the key that signs the nodes' scoped tokens
    # (app.mirror_tokens, task SEC-1)
    hf_mirror_token: SecretStr = SecretStr("")
    # the portal's own key to the mirror (admin API, transfers, the catalog's reads of any repository; SEC-2 of main);
    # empty (an older main): HF_MIRROR_TOKEN
    hf_mirror_admin_token: SecretStr = SecretStr("")
    # start-up budget of an inference pod (startupProbe): 600 s + 2 x size / rate (download, then load) x 1.5,
    # at least an hour; the rate is MB/s from the mirror and over the network to the rpc servers
    model_load_mbps: float = Field(default=40.0, gt=0)
    # size limit of the ggml-rpc-server tensor cache on each node (rpc-cache-gc.sh; 0 = no limit)
    rpc_cache_max_gib: int = Field(default=64, ge=0)
    # data directory of the Hugging Face mirror as the portal sees it in the main container (volume llm-main-hf):
    # own model files live in models/local/<name>/ there and reach the nodes as the repository local/<name>
    hf_mirror_data_dir: str = "/var/lib/llm-hf"
    # total size of own model files (0: only the free space of the volume limits them)
    local_models_max_gib: int = Field(default=0, ge=0)
    # largest part of an upload in one request (Caddy lets up to 80 MB through on the upload path)
    upload_chunk_mib: int = Field(default=32, ge=1, le=64)
    # the controllers look for files dropped into models/ this often; a file is taken once unchanged for settle_s
    local_models_scan_s: float = Field(default=60.0, ge=1)
    local_models_settle_s: float = Field(default=30.0, ge=0)

    # Cleanup of model weights on class nodes (app.nodes.cleanup, task 2-26): a node without a deployment (unassigned, the
    # deployment deleted or disabled; the working window does not count) gets its model files and tensor cache removed
    # after this many seconds. A node whose deployment changed its model is cleaned at once.
    model_cache_idle_s: float = Field(default=600.0, ge=0)

    default_model_name: str = "gemma-4-e4b"
    default_model_repo: str = "unsloth/gemma-4-E4B-it-GGUF"
    default_model_file: str = "gemma-4-E4B-it-Q4_K_M.gguf"
    default_ctx_per_request: int = 8192
    default_parallel: int = 2

    controllers_enabled: bool = True
    controller_interval_s: float = 10.0
    # a NotReady node keeps its deployment label this long, then loses it so DaemonSet rollouts do not wait for it
    node_notready_grace_s: float = Field(default=120.0, ge=0)
    # the same pause for the nodes of a split deployment, which also keeps a group waiting for a lost member (a
    # restarted PC must not rebuild a 15-node model); both are defaults a deployment's regroup_grace_s overrides
    split_regroup_grace_s: float = Field(default=600.0, ge=0)
    # when a deployment's working window ends its backends leave LiteLLM at once and its pods stay this long, so
    # the streams in progress finish
    schedule_drain_s: float = Field(default=60.0, ge=0)
    # split: CUDA compute buffer kept free on every GPU besides the 1.5 GiB reserve (app.catalog.placement)
    gpu_compute_reserve_mib: int = Field(default=1024, ge=0)
    standalone_ttl_s: int = 45
    # a standalone record without a heartbeat for this long is removed (the node registers again when it is back);
    # one that shadows a Ready cluster node of the same name goes at once
    standalone_purge_s: int = Field(default=86400, ge=60)
    # Wake-on-LAN (app.nodes.wake): a node that went down while its deployment needs it (not on maintenance, inside
    # the working window) gets a magic packet WOL_DELAY_S after it was seen down, then again after WOL_RETRY_S, doubling
    # (at most 30 min), WOL_MAX_TRIES attempts in all. False turns the automatic waking off, the «Разбудить» button stays.
    wol_enabled: bool = True
    wol_delay_s: float = Field(default=60.0, ge=0)
    wol_retry_s: float = Field(default=60.0, gt=0)
    wol_max_tries: int = Field(default=6, ge=0)
    wol_unicast: bool = True  # also to the node's last address, besides the subnet's broadcast
    # fallback: from attempt WOL_VIA_NODE_AFTER + 1 on, a live node of the same subnet also sends the packet on its own
    # network (k8s: a short Job pinned to it, standalone: a command in its heartbeat); always: from the first attempt
    wol_via_node: Literal["fallback", "always", "off"] = "fallback"
    wol_via_node_after: int = Field(default=2, ge=0)
    # subnet=address pairs, e.g. 10.1.2.0/24=10.1.2.255: where the packets for the nodes of a subnet go instead of its
    # directed broadcast (main's own subnet from a bridge container, a relay, a router that wants another address)
    wol_broadcast_override: str = ""
    # daily pg_dump files of the postgres-backup CronJob (main/manifests/common-20-postgres.yaml.tmpl) as the portal sees them
    backup_dir: str = "/var/lib/llm/backups"
    # NVIDIA GPU of the main server's host as `nvidia-smi` reports it ("name, memory"); run-main.sh passes it, empty: none
    main_gpu: str = ""
    web_dist: str = "/opt/llm/web"
    migrations_dir: str = ""

    @field_validator("wol_broadcast_override")
    @classmethod
    def _valid_wol_override(cls, value: str) -> str:
        wake.parse_overrides(value)  # its ValueError names the variable and the expected form
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def _valid_trusted_proxies(cls, value: str) -> str:
        for part in value.split(","):
            if part.strip():
                try:
                    ipaddress.ip_network(part.strip(), strict=False)
                except ValueError:
                    raise ValueError(f"TRUSTED_PROXIES: не адрес и не подсеть: {part.strip()}") from None
        return value

    @property
    def trusted_proxy_networks(self) -> tuple:
        return tuple(ipaddress.ip_network(p.strip(), strict=False) for p in self.trusted_proxies.split(",") if p.strip())

    @field_validator("oidc_required_claim")
    @classmethod
    def _valid_required_claim(cls, value: str) -> str:
        value = value.strip()
        if value and ("=" not in value or not value.partition("=")[0].strip()):
            raise ValueError("OIDC_REQUIRED_CLAIM: нужно имя=значение, например groups=llm-users")
        return value

    @property
    def wol_overrides(self) -> wake.Overrides:
        return wake.parse_overrides(self.wol_broadcast_override)

    @property
    def registry_base(self) -> str:
        """Address of the image registry on main, without a trailing slash."""
        return self.registry_url.rstrip("/") or f"http://127.0.0.1:{self.registry_port}"

    @property
    def registry_repo(self) -> str:
        """Name of the node image repository in main's registry: main keeps it as `nsu-llm-node` whatever registry the pods
        ask (docker.io/library/nsu-llm-node, ghcr.io/<owner>/nsu-llm-node, see node/rootfs/opt/llm/bin/entrypoint.sh)."""
        return self.node_image_repo.rsplit("/", 1)[-1]

    @property
    def backend_key_fingerprint(self) -> str:
        return key_fingerprint(self.backend_api_key.get_secret_value())

    @property
    def mirror_admin_key(self) -> str:
        """The portal's own key to the mirror: HF_MIRROR_ADMIN_TOKEN, else (an older main) HF_MIRROR_TOKEN."""
        return self.hf_mirror_admin_token.get_secret_value() or self.hf_mirror_token.get_secret_value()

    @property
    def node_hf_endpoint(self) -> str:
        """Mirror address for the nodes, without a trailing slash. By IP: class PCs often cannot resolve PUBLIC_HOSTNAME,
        and the IP site of Caddy serves /hf with a certificate of the internal CA for the IP. An own certificate is
        for the name (a public one cannot cover a private IP): then the name, unless HF_MIRROR_URL says otherwise."""
        if self.hf_mirror_url:
            return self.hf_mirror_url.rstrip("/")
        host = self.main_advertise_ip if self.main_advertise_ip and not self.main_own_cert else self.public_hostname
        return f"https://{host}/hf"

    @property
    def public_base_url(self) -> str:
        return f"https://{self.public_hostname}"

    @property
    def admin_email_set(self) -> frozenset[str]:
        return frozenset(e.strip().lower() for e in self.admin_emails.split(",") if e.strip())

    @property
    def admin_sub_set(self) -> frozenset[str]:
        """ADMIN_SUBS as given (a subject is case sensitive): `<sub>` or `<issuer>|<sub>`."""
        return frozenset(e.strip() for e in self.admin_subs.split(",") if e.strip())

    @property
    def allowed_domain_set(self) -> frozenset[str]:
        return frozenset(
            d.strip().lower().lstrip("@").removeprefix("*.").rstrip(".")
            for d in self.oidc_allowed_domains.split(",")
            if d.strip().lstrip("@").removeprefix("*.")
        )

    @property
    def required_claim(self) -> tuple[str, str] | None:
        """OIDC_REQUIRED_CLAIM as (name, value), None when empty."""
        if not self.oidc_required_claim:
            return None
        name, _, value = self.oidc_required_claim.partition("=")
        return name.strip(), value.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
