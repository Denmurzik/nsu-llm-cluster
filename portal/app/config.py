import hashlib
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

    oidc_issuer: str = "https://sso-ng.nsu.ru/auth/realms/NSU"
    oidc_client_id: str = "double.fresh.nsuts.ru"
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_scopes: str = "openid profile email"
    # NSU ID bridge: the client's redirect_uri is registered for another domain (double.fresh.nsuts.ru/*), whose front
    # (tsga nginx) answers 302 to https://<public_hostname>/auth/callback with the same query. Empty: own callback.
    oidc_redirect_uri: str = ""
    # where NSU ID sends the browser after logout (must also be allowed for the client); empty: https://<public_hostname>/
    oidc_post_logout_redirect_uri: str = ""
    admin_emails: str = ""
    # true: the admin role also needs email_verified (absent claim allowed, "false" blocks it);
    # false: ADMIN_EMAILS alone decides (NSU ID never sends email_verified=true)
    admin_require_email_verified: bool = True
    session_ttl_hours: int = 12
    cookie_name: str = "nsullm_session"
    cookie_secure: bool = True

    litellm_url: str = "http://127.0.0.1:4000"
    litellm_master_key: SecretStr = SecretStr("")
    max_keys_per_user: int = 5
    key_rpm_limit: int = 60
    key_tpm_limit: int = 100000
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

    # nodes join with the agent token (k3s --agent-token); the k3s server token never leaves the main server
    agent_token: SecretStr = SecretStr("")
    # written by k3s in the secure form K10<ca hash>::node:<agent token>, shown on the Nodes page
    agent_token_file: str = "/var/lib/rancher/k3s/server/agent-token"
    # the k3s server token; the node API accepts it for one release from nodes installed before the agent token
    cluster_token: SecretStr = SecretStr("")
    node_legacy_token: bool = True
    backend_api_key: SecretStr = SecretStr("")  # shared secret of inference backends (llama-server/vLLM --api-key)
    kubeconfig: str = "/etc/rancher/k3s/k3s.yaml"
    k8s_namespace: str = "llm"
    node_image_repo: str = "nsu-llm-node"
    node_image_tag: str = "0.1.0"
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
    hf_mirror_token: SecretStr = SecretStr("")
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

    @property
    def wol_overrides(self) -> wake.Overrides:
        return wake.parse_overrides(self.wol_broadcast_override)

    @property
    def backend_key_fingerprint(self) -> str:
        return key_fingerprint(self.backend_api_key.get_secret_value())

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
