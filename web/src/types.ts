export type Role = "user" | "admin";

export interface Me {
  id: string;
  email: string;
  username: string;
  full_name: string;
  role: Role;
  csrf_token: string;
}

export type ModelStatus = "ready" | "starting" | "down" | "unknown";
export type Engine = "llamacpp" | "vllm";
export type DeployMode = "replicas" | "split";
export type Accel = "any" | "cpu" | "nvidia";

export interface PublicStatus {
  models: { name: string; status: ModelStatus; ready_backends: number | null }[];
}

export interface CatalogModel {
  name: string;
  engine: Engine;
  mode: DeployMode;
  group_size: number;
  ctx_per_request: number;
  parallel: number;
  ready_backends: number | null;
  status: ModelStatus;
  /** only an admin ever gets `true`: ordinary users do not receive experimental models at all */
  experimental: boolean;
  /** the model accepts images (it has an mmproj file) */
  vision: boolean;
}

export interface ApiKey {
  id: string;
  alias: string;
  masked: string;
  created_at: string;
  expires_at: string | null;
  tokens_30d: number;
  requests_30d: number;
}

export interface CreatedKey {
  id: string;
  alias: string;
  key: string;
  masked: string;
  created_at: string;
  expires_at: string | null;
}

export interface Usage {
  days: number;
  prompt_tokens: number;
  completion_tokens: number;
  requests: number;
  by_day: { day: string; tokens: number; requests: number }[];
}

export interface ModelMeta {
  weights_bytes?: number;
  arch: string;
  n_layers: number;
  n_heads: number;
  n_kv_heads: number;
  head_dim: number;
  max_ctx: number;
}

export interface Deployment {
  id: number;
  name: string;
  engine: Engine;
  repo: string;
  file: string | null;
  /** Hugging Face commit the nodes download; null: main (made before the pin, own models) */
  revision: string | null;
  mode: DeployMode;
  group_size: number;
  accel: Accel;
  ctx_per_request: number;
  parallel: number;
  auto_assign: boolean;
  enabled: boolean;
  spare_nodes?: number;
  /** null: the default of the mode, see regroup_grace_effective_s */
  regroup_grace_s?: number | null;
  regroup_grace_effective_s?: number;
  schedule?: Schedule | null;
  schedule_active?: boolean;
  /** split on nvidia: the rpc servers also offer the node's RAM */
  use_node_ram?: boolean;
  /** share of the node's RAM a split model may take, % */
  ram_budget_pct?: number;
  /** llama.cpp settings (portal task 2-22): reasoning of the model */
  reasoning: Reasoning;
  /** KV cache type: deployments made before the setting keep f16, new ones get q8_0 */
  kv_cache_type: KvCacheType;
  /** sampling defaults: a client may override them; null: the server's own */
  sampling: Sampling | null;
  /** draft model of speculative decoding (own files: local/<model>), its file and pinned commit */
  draft_repo: string | null;
  draft_file: string | null;
  draft_revision: string | null;
  /** images: a file of the model's repository or local/... */
  mmproj_file: string | null;
  /** own llama-server flags */
  extra_args: string | null;
  /** added by the gateway when the client sent no system message */
  system_prompt: string | null;
  /** experimental: visible to admins only */
  experimental: boolean;
  /** SCHEDULE_DRAIN_S: how long the pods keep finishing answers after the window ends */
  schedule_drain_s?: number;
  meta_missing?: boolean;
  ctx_exceeds_model?: boolean;
  assigned_nodes?: string[];
  meta?: ModelMeta | null;
  spec_hash?: string;
}

export type Reasoning = "off" | "on" | "auto";
export type KvCacheType = "f16" | "q8_0" | "q4_0";

export interface Sampling {
  temperature?: number;
  top_p?: number;
  top_k?: number;
  min_p?: number;
  repeat_penalty?: number;
}

/** Working window in Novosibirsk time; days are ISO weekdays (1 is Monday). */
export interface Schedule {
  days: number[];
  start: string;
  end: string;
}

export interface DeploymentBody {
  name: string;
  engine: Engine;
  repo: string;
  file: string | null;
  mode: DeployMode;
  group_size: number;
  accel: Accel;
  ctx_per_request: number;
  parallel: number;
  auto_assign: boolean;
  enabled: boolean;
  spare_nodes?: number;
  regroup_grace_s?: number | null;
  schedule?: Schedule | null;
  use_node_ram?: boolean;
  ram_budget_pct?: number;
  reasoning?: Reasoning;
  kv_cache_type?: KvCacheType;
  sampling?: Sampling | null;
  draft_repo?: string | null;
  draft_file?: string | null;
  mmproj_file?: string | null;
  extra_args?: string | null;
  system_prompt?: string | null;
  experimental?: boolean;
  nodes: string[];
}

/** Load stage of an inference pod (portal: app/deployments/progress.py). */
export type LoadStage = "starting" | "downloading" | "loading" | "ready";

export interface LoadProgress {
  stage: LoadStage;
  percent: number | null;
  done_bytes: number | null;
  total_bytes: number | null;
}

export interface PodStatus {
  name: string;
  role: string;
  phase: string;
  ready: boolean;
  restarts: number;
  message: string;
  last_log: string | null;
  /** null for rpc pods and pods that are not running */
  progress: LoadProgress | null;
}

export interface DeploymentStatus {
  spec_hash: string;
  nodes: { name: string; source: string; pods: PodStatus[]; applied: boolean }[];
  groups: string[][];
  /** split groups the controller does not start because the model does not fit their memory, in Russian */
  placement_problems?: string[];
  ready_backends: number | null;
}

export interface HfFile {
  file: string;
  size_bytes: number;
  quant: string | null;
  shards: string[];
}

/** An image projector file of a repository (`mmproj*.gguf`) */
export interface MmprojFile {
  file: string;
  size_bytes: number;
  quant: string | null;
}

/** The llama-server command of a deployment: the first pod, the rpc server of a split group, notes (Russian) */
export interface CommandPreview {
  leader: string;
  rpc: string | null;
  notes: string[];
}

export interface HfVllmInfo {
  size_bytes: number;
  meta: ModelMeta;
}

export interface Estimate {
  meta: ModelMeta;
  estimate: {
    weights_bytes: number;
    kv_bytes: number;
    total_bytes: number;
    per_node_bytes: number;
    /** on a GPU node: what its VRAM holds (the input embeddings stay in RAM) and what stays in RAM; null: unknown */
    device_bytes?: number | null;
    host_bytes?: number | null;
    /** parts besides the model, included in total_bytes (zero without a draft model / projector) */
    draft_weights_bytes?: number;
    draft_kv_bytes?: number;
    mmproj_bytes?: number;
  };
  /** need_bytes: what one replica needs on this node (VRAM share on a GPU node) */
  nodes: { name: string; capacity_bytes: number | null; need_bytes?: number; fits: boolean }[];
  recommended_group_size: number | null;
  ctx_ok: boolean;
  /** split: layers of the first group over the GPU and RAM of its nodes; null otherwise or when unknown */
  placement?: Placement | null;
}

export interface PlacementNode {
  name: string;
  leader: boolean;
  vram_bytes: number;
  /** null: the node offers no GPU */
  vram_budget_bytes: number | null;
  gpu_layers: number;
  ram_bytes: number;
  /** null: the node's RAM is not used */
  ram_budget_bytes: number | null;
  cpu_layers: number;
}

export interface Placement {
  fits: boolean;
  tensor_split: number[];
  ram_bytes: number;
  speed_warning: boolean;
  nodes: PlacementNode[];
}

export interface NodeInfo {
  name: string;
  source: "k8s" | "standalone";
  accel: "cpu" | "nvidia";
  ip: string | null;
  ready: boolean;
  vram_mib: number | null;
  mem_mib: number | null;
  capacity_bytes: number | null;
  deployment: string | null;
  backends: number;
  version: string | null;
  is_main: boolean;
  maintenance: boolean;
  /** the node reported its network card: the «Разбудить» button works */
  wakeable: boolean;
  wake: WakeState | null;
}

/** Wake-on-LAN attempts of the controller while the node is down */
export interface WakeState {
  tries: number;
  max_tries: number;
  since: string;
}

export interface WakeResult {
  node: string;
  targets: string[];
  /** a live node of the same subnet that also sends the packet (WOL_VIA_NODE), or null */
  relay: string | null;
}

export interface NodesResponse {
  k8s_available: boolean;
  nodes: NodeInfo[];
  backends: unknown[];
}

export interface StatusNode {
  name: string;
  source: "k8s" | "standalone";
  accel: "cpu" | "nvidia";
  ready: boolean;
  is_main: boolean;
  vram_mib: number | null;
  mem_mib: number | null;
  deployment: string | null;
  backends: number;
  /** seconds since the last sign of life; null when the cluster has none */
  heartbeat_age_s: number | null;
  maintenance: boolean;
  wakeable: boolean;
  wake: WakeState | null;
}

export interface GatewayCounts {
  rate_limited: number;
  server_errors: number;
}

export interface AdminStatus {
  at: string;
  k8s_available: boolean;
  nodes: StatusNode[];
  deployments: {
    name: string;
    mode: DeployMode;
    enabled: boolean;
    status: ModelStatus | "off";
    ready_backends: number | null;
    assigned_nodes: number;
  }[];
  gateway: { available: boolean; last_5m: GatewayCounts | null; last_1h: GatewayCounts | null };
  main: { gpu: string | null };
  backups: {
    available: boolean;
    count: number;
    last_at: string | null;
    last_age_s: number | null;
    last_size_bytes: number | null;
  };
}

export interface JoinInfo {
  agent_token: string;
  main_host: string;
  linux: string;
  linux_standalone: string;
  windows: string;
}

export interface AdminUser {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  created_at: string;
  last_login_at: string | null;
  keys: number;
  tokens_30d: number | null;
}

export interface PodLogs {
  name: string;
  lines: string;
}

/** «Мои модели»: a file (or a sharded set) of an own model in models/local/<name>/ on main */
export interface LocalModelFile {
  file: string;
  size_bytes: number;
  quant: string | null;
  shards: string[];
  /** incomplete: shards are missing; invalid: not a usable GGUF file (see error) */
  status: "ready" | "incomplete" | "invalid";
  error: string | null;
  sha256: string | null;
  origin: "upload" | "folder";
  meta: ModelMeta | null;
  /** memory at the default context and parallelism; null until the file is ready */
  estimate: { ctx_per_request: number; parallel: number; total_bytes: number } | null;
  used_by: string[];
  updated_at: string | null;
}

export interface LocalModel {
  name: string;
  /** the repository a deployment uses: local/<name> */
  repo: string;
  size_bytes: number;
  used_by: string[];
  files: LocalModelFile[];
}

export type UploadStatus = "uploading" | "verifying" | "done" | "failed" | "cancelled";

export interface ModelUpload {
  id: string;
  model: string;
  file: string;
  size_bytes: number;
  received_bytes: number;
  status: UploadStatus;
  sha256_expected: string | null;
  sha256: string | null;
  verified_bytes: number;
  error: string | null;
  chunk_bytes: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface LocalCatalog {
  models: LocalModel[];
  uploads: ModelUpload[];
  storage: {
    used_bytes: number;
    free_bytes: number;
    limit_bytes: number | null;
    chunk_bytes: number;
    /** models/ on the mirror's volume, as the main container sees it */
    folder: string;
  };
}

/** An entry of the audit log (portal: app/audit.py, table audit_log); never holds secrets. */
export interface AuditEntry {
  id: number;
  /** ISO time */
  at: string;
  actor_id: string | null;
  actor_email: string | null;
  /** dotted: deployment.create, node.wake, user.block, setting.update, ... */
  action: string;
  /** deployment, node, user, key, setting, cache, backup, local_model */
  target_type: string;
  target: string;
  details: Record<string, unknown> | null;
  ip: string | null;
}

export interface AuditPage {
  items: AuditEntry[];
  total: number;
}

/** A runtime setting (portal: app/settings_store.py): the effective value, the env default and the rules for a new one */
export interface SystemSetting {
  key: string;
  type: "int" | "float" | "bool";
  /** effective: the stored override or the default */
  value: number | boolean;
  default: number | boolean;
  /** a value changed in the panel is stored */
  overridden: boolean;
  min: number | null;
  max: number | null;
  /** Russian */
  description: string;
  /** takes effect only after the component restarts */
  restart_required: boolean;
  updated_at: string | null;
  updated_by: string | null;
}

/**
 * Fields the users table gained in migration 0009. role_source `auto`: the role follows ADMIN_EMAILS at login;
 * `manual`: set in the panel and kept. A null limit means the global setting.
 */
export interface AdminUserOps {
  role_source: "auto" | "manual";
  blocked: boolean;
  blocked_reason: string | null;
  rpm_limit: number | null;
  tpm_limit: number | null;
  max_keys: number | null;
}

/** The user row of the admin panel once the users API returns the migration 0009 fields */
export type AdminUserFull = AdminUser & AdminUserOps;
