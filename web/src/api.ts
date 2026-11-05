import type {
  AdminStatus,
  AdminUsage,
  AdminUserFull,
  AuditPage,
  ApiKey,
  BackupsData,
  CacheDeleted,
  CatalogModel,
  CommandPreview,
  CreatedKey,
  Deployment,
  DeploymentBody,
  DeploymentStatus,
  Estimate,
  HfFile,
  HfVllmInfo,
  JoinInfo,
  KeyLimitsJob,
  KvCacheType,
  LocalCatalog,
  Me,
  MetricsHistory,
  MetricsLive,
  MetricsNames,
  MmprojFile,
  ModelCache,
  ModelUpload,
  NodeDeleteResult,
  NodesResponse,
  PodLogs,
  PrefetchJob,
  PrefetchList,
  PublicStatus,
  PurgeResult,
  SystemSetting,
  Usage,
  UsageGroup,
  UserPatch,
  UsersPage,
  WakeResult,
} from "./types";
import { validationDetail } from "./lib/validation";

export class ApiError extends Error {
  status: number;
  detail: string;
  /** the rest of a JSON error body next to `detail` (a refused node removal lists the deployments it would hurt) */
  data: Record<string, unknown>;
  constructor(status: number, detail: string, data: Record<string, unknown> = {}) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.data = data;
  }
}

let csrfToken: string | null = null;
const unauthorizedListeners = new Set<() => void>();

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export function onUnauthorized(fn: () => void) {
  unauthorizedListeners.add(fn);
  return () => {
    unauthorizedListeners.delete(fn);
  };
}

const GATEWAY_DOWN = "Шлюз моделей недоступен";
const SESSION_STALE = "Сессия устарела. Обновите страницу и повторите действие";

function absolute(path: string): string {
  if (typeof window === "undefined") return path;
  return new URL(path, window.location.origin).toString();
}

function headersFor(method: string, hasBody: boolean): Record<string, string> {
  const h: Record<string, string> = { Accept: "application/json" };
  if (hasBody) h["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) h["X-CSRF-Token"] = csrfToken;
  return h;
}

async function errorFrom(res: Response): Promise<ApiError> {
  let detail = "";
  let extra: Record<string, unknown> = {};
  try {
    const data = await res.json();
    if (typeof data?.detail === "string") detail = data.detail;
    else if (Array.isArray(data?.detail)) detail = validationDetail(data.detail);
    if (data && typeof data === "object" && !Array.isArray(data)) extra = data;
  } catch {
    /* body is not JSON */
  }
  if (!detail) {
    detail = res.status === 503 ? GATEWAY_DOWN : `Ошибка ${res.status}`;
  }
  return new ApiError(res.status, detail, extra);
}

const isCsrfRejection = (res: Response, err: ApiError) => res.status === 403 && err.detail === "CSRF";

/** A rejected CSRF token usually means the session cookie was renewed: fetch a fresh token once. */
async function refreshCsrf(): Promise<void> {
  try {
    await api.me();
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) unauthorizedListeners.forEach((fn) => fn());
    throw new ApiError(403, SESSION_STALE);
  }
}

export interface Reply<T> {
  data: T;
  headers: Headers;
}

async function send<T>(method: string, path: string, body?: unknown, retried = false): Promise<Reply<T>> {
  let res: Response;
  try {
    res = await fetch(absolute(path), {
      method,
      credentials: "same-origin",
      headers: headersFor(method, body !== undefined),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Нет связи с сервером");
  }
  if (!res.ok) {
    if (res.status === 401 && path !== "/api/me") unauthorizedListeners.forEach((fn) => fn());
    const err = await errorFrom(res);
    if (isCsrfRejection(res, err)) {
      if (retried || path === "/api/me") throw new ApiError(403, SESSION_STALE);
      await refreshCsrf();
      return send<T>(method, path, body, true);
    }
    throw err;
  }
  if (res.status === 204) return { data: undefined as T, headers: res.headers };
  const text = await res.text();
  return { data: (text ? JSON.parse(text) : undefined) as T, headers: res.headers };
}

const get = async <T>(path: string) => (await send<T>("GET", path)).data;
const post = async <T>(path: string, body?: unknown) => (await send<T>("POST", path, body ?? {})).data;
/** POST with the session and CSRF handling of `api`, for calls that live outside this file (lib/adminActions.ts) */
export const postJson = post;
const patch = async <T>(path: string, body: unknown) => (await send<T>("PATCH", path, body)).data;
const put = async <T>(path: string, body: unknown) => (await send<T>("PUT", path, body)).data;
const del = async (path: string) => {
  await send<void>("DELETE", path);
};
/** GET and DELETE with the same handling, for the same reason (lib/nodeVersions.ts) */
export const getJson = get;
export const deleteJson = del;
const delJson = async <T>(path: string) => (await send<T>("DELETE", path)).data;

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Filters of the audit log (`since` and `until` are ISO times) */
export type AuditQuery = {
  action?: string;
  actor?: string;
  target?: string;
  since?: string;
  until?: string;
};

export interface EstimateParams {
  repo: string;
  file: string | null;
  engine: string;
  ctx_per_request: number;
  parallel: number;
  group_size: number;
  nodes: string[];
  mode?: string;
  use_node_ram?: boolean;
  ram_budget_pct?: number;
  /** llama.cpp settings that change the memory (portal task 2-22b) */
  kv_cache_type?: KvCacheType;
  draft_repo?: string | null;
  draft_file?: string | null;
  mmproj_file?: string | null;
}

export const api = {
  async me(): Promise<Me> {
    const me = await get<Me>("/api/me");
    setCsrfToken(me.csrf_token);
    return me;
  },
  publicStatus: () => get<PublicStatus>("/api/public/status"),
  models: () => get<CatalogModel[]>("/api/models"),

  async keys(): Promise<{ keys: ApiKey[]; usageUnavailable: boolean }> {
    const r = await send<ApiKey[]>("GET", "/api/keys");
    return { keys: r.data, usageUnavailable: r.headers.get("X-Usage-Unavailable") === "1" };
  },
  createKey: (alias: string, expires_days: number | null) => post<CreatedKey>("/api/keys", { alias, expires_days }),
  deleteKey: (id: string) => del(`/api/keys/${id}`),
  usage: (days: number) => get<Usage>(`/api/usage?days=${days}`),

  deployments: () => get<Deployment[]>("/api/admin/deployments"),
  deployment: (id: number) => get<Deployment>(`/api/admin/deployments/${id}`),
  createDeployment: (body: DeploymentBody) => post<Deployment>("/api/admin/deployments", body),
  updateDeployment: (id: number, body: Partial<DeploymentBody>) =>
    patch<Deployment>(`/api/admin/deployments/${id}`, body),
  deleteDeployment: (id: number) => del(`/api/admin/deployments/${id}`),
  refreshRevision: (id: number) =>
    post<{ changed: boolean; deployment: Deployment }>(`/api/admin/deployments/${id}/refresh-revision`, {}),
  deploymentStatus: (id: number) => get<DeploymentStatus>(`/api/admin/deployments/${id}/status`),
  hfFiles: (repo: string) => get<HfFile[]>(`/api/admin/hf/files${qs({ repo })}`),
  hfVllm: (repo: string) => get<HfVllmInfo>(`/api/admin/hf/files${qs({ repo, engine: "vllm" })}`),
  hfEstimate: (p: EstimateParams) =>
    get<Estimate>(
      `/api/admin/hf/estimate${qs({
        repo: p.repo,
        file: p.file,
        engine: p.engine,
        ctx_per_request: p.ctx_per_request,
        parallel: p.parallel,
        group_size: p.group_size,
        nodes: p.nodes.join(","),
        mode: p.mode,
        use_node_ram: p.use_node_ram,
        ram_budget_pct: p.ram_budget_pct,
        kv_cache_type: p.kv_cache_type,
        draft_repo: p.draft_repo,
        draft_file: p.draft_file,
        mmproj_file: p.mmproj_file,
      })}`,
    ),
  /** projector files (mmproj*.gguf) of a model repository, for «Изображения» */
  hfMmproj: (repo: string) => get<MmprojFile[]>(`/api/admin/hf/mmproj${qs({ repo })}`),
  /** the llama-server command of a saved deployment, secrets masked */
  deploymentCommand: (id: number) => get<CommandPreview>(`/api/admin/deployments/${id}/command`),
  /** the command a form that is not saved yet would start; the server validates it like a create (422) */
  commandPreview: (body: DeploymentBody) => post<CommandPreview>("/api/admin/deployments/command-preview", body),
  nodes: (enrolled = false) => get<NodesResponse>(enrolled ? "/api/admin/nodes?enrolled=1" : "/api/admin/nodes"),
  assignNode: (name: string, deployment_id: number | null) =>
    put<unknown>(`/api/admin/nodes/${encodeURIComponent(name)}/assignment`, { deployment_id }),
  setMaintenance: (name: string, maintenance: boolean) =>
    put<unknown>(`/api/admin/nodes/${encodeURIComponent(name)}/maintenance`, { maintenance }),
  wakeNode: (name: string) => post<WakeResult>(`/api/admin/nodes/${encodeURIComponent(name)}/wake`),
  /** removes the model files and the tensor cache the node's deployment does not need; waits for the node */
  purgeNodeModels: (name: string) => post<PurgeResult>(`/api/admin/nodes/${encodeURIComponent(name)}/purge-models`),
  /** removes the node from the cluster and everything the portal keeps for it; 409 for a node with a deployment unless `force` */
  deleteNode: (name: string, force = false) =>
    delJson<NodeDeleteResult>(`/api/admin/nodes/${encodeURIComponent(name)}${qs({ force: force ? 1 : null })}`),
  /** «Подтвердить»: a new node may serve (audit node.approve) */
  approveNode: (name: string, claimId?: number) =>
    post<{ approved: string[] }>(
      `/api/admin/nodes/${encodeURIComponent(name)}/approve`,
      claimId === undefined ? {} : { claim_id: claimId },
    ),
  /** «Отклонить»: one claim on the name, or all of them */
  rejectClaims: (name: string, claimId?: number) =>
    delJson<{ node: string; rejected: number }>(
      `/api/admin/nodes/${encodeURIComponent(name)}/claims${qs({ claim_id: claimId ?? null })}`,
    ),
  /** «Сбросить ключ ноды»: the key and the approval go, the node registers again and waits for approval */
  resetNodeKey: (name: string) =>
    post<{ node: string; approved: boolean }>(`/api/admin/nodes/${encodeURIComponent(name)}/reset-key`),
  join: () => get<JoinInfo>("/api/admin/join"),
  adminStatus: () => get<AdminStatus>("/api/admin/status"),
  backups: () => get<BackupsData>("/api/admin/backups"),
  runBackup: () => post<{ job: string }>("/api/admin/backups"),
  deleteBackup: (name: string) => del(`/api/admin/backups/${encodeURIComponent(name)}`),
  adminUsage: (p: { from: string; to: string; group: UsageGroup }) => get<AdminUsage>(`/api/admin/usage${qs(p)}`),
  metricsLive: () => get<MetricsLive>("/api/admin/metrics/live"),
  metricsNames: () => get<MetricsNames>("/api/admin/metrics/names"),
  metricsHistory: (kind: "node" | "model", name: string, from: Date, to: Date) =>
    get<MetricsHistory>(
      `/api/admin/metrics/history?kind=${kind}&name=${encodeURIComponent(name)}&from=${encodeURIComponent(from.toISOString())}&to=${encodeURIComponent(to.toISOString())}`,
    ),
  settings: () => get<SystemSetting[]>("/api/admin/settings"),
  putSetting: (key: string, value: number | boolean) =>
    put<SystemSetting>(`/api/admin/settings/${encodeURIComponent(key)}`, { value }),
  /** back to the value of the .env */
  resetSetting: (key: string) => delJson<SystemSetting>(`/api/admin/settings/${encodeURIComponent(key)}`),
  applyKeyLimits: () => post<KeyLimitsJob>("/api/admin/key-limits/apply"),
  keyLimitsJob: () => get<KeyLimitsJob>("/api/admin/key-limits/job"),
  restartMirror: () => post<SystemSetting>("/api/admin/hf-mirror/restart"),
  users: (q: { q?: string; role?: string; blocked?: boolean; limit: number; offset: number }) =>
    get<UsersPage>(`/api/admin/users${qs(q)}`),
  patchUser: (id: string, body: UserPatch) => patch<AdminUserFull>(`/api/admin/users/${id}`, body),
  /** the keys of one user as the user sees them (masked); `usageUnavailable`: the gateway could not count the tokens */
  async userKeys(id: string): Promise<{ keys: ApiKey[]; usageUnavailable: boolean }> {
    const r = await send<ApiKey[]>("GET", `/api/admin/users/${id}/keys`);
    return { keys: r.data, usageUnavailable: r.headers.get("X-Usage-Unavailable") === "1" };
  },
  revokeUserKey: (userId: string, keyId: string) => del(`/api/admin/users/${userId}/keys/${keyId}`),
  audit: (q: AuditQuery & { limit: number; offset: number }) => get<AuditPage>(`/api/admin/audit${qs(q)}`),
  /** the address of the CSV of the filtered log: a plain link, the browser sends the session cookie */
  auditExportUrl: (q: AuditQuery) => `/api/admin/audit/export${qs(q)}`,
  podLogs: (name: string, lines = 200) =>
    get<PodLogs>(`/api/admin/pods/${encodeURIComponent(name)}/logs?lines=${lines}`),

  localModels: () => get<LocalCatalog>("/api/admin/local-models"),
  scanLocalModels: () => post<LocalCatalog>("/api/admin/local-models/scan"),
  deleteLocalModel: (name: string, file?: string) =>
    del(`/api/admin/local-models/${encodeURIComponent(name)}${qs({ file })}`),
  createUpload: (body: { model: string; file: string; size_bytes: number; sha256?: string | null }) =>
    post<ModelUpload>("/api/admin/local-models/uploads", body),
  upload: (id: string) => get<ModelUpload>(`/api/admin/local-models/uploads/${id}`),
  finishUpload: (id: string) => post<ModelUpload>(`/api/admin/local-models/uploads/${id}/finish`),
  cancelUpload: (id: string) => del(`/api/admin/local-models/uploads/${id}`),
  putUploadPart: (id: string, offset: number, part: Blob, signal?: AbortSignal) => sendPart(id, offset, part, signal),

  // the cache of the mirror and the downloads into it (task 2-29)
  modelCache: () => get<ModelCache>("/api/admin/cache"),
  /** a file of the cache (`path`), or the whole repository */
  deleteCached: async (repo: string, path?: string) =>
    (await send<CacheDeleted>("DELETE", `/api/admin/cache/${encodePath(path ? `${repo}/${path}` : repo)}`)).data,
  prefetchJobs: () => get<PrefetchList>("/api/admin/prefetch"),
  prefetch: (body: { repo: string; revision?: string | null; files: string[] }) =>
    post<{ jobs: PrefetchJob[] }>("/api/admin/prefetch", body),
  /** stops a queued or running download; a finished one leaves the list */
  cancelPrefetch: (id: string) => del(`/api/admin/prefetch/${encodeURIComponent(id)}`),
  clearPrefetch: () => del("/api/admin/prefetch"),
  /** the weights of a deployment (model, projector, draft model) at its pinned commits */
  prefetchDeployment: (id: number) => post<{ jobs: PrefetchJob[] }>(`/api/admin/deployments/${id}/prefetch`),
};

const encodePath = (path: string) => path.split("/").map(encodeURIComponent).join("/");

/** One part of an upload as raw bytes; a 409 carries the offset the server has in `Upload-Offset`. */
export class OffsetMismatch extends ApiError {
  offset: number;
  constructor(detail: string, offset: number) {
    super(409, detail);
    this.offset = offset;
  }
}

function readPart(part: Blob): Promise<ArrayBuffer> {
  if (typeof part.arrayBuffer === "function") return part.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(part);
  });
}

async function sendPart(id: string, offset: number, part: Blob, signal?: AbortSignal, retried = false): Promise<ModelUpload> {
  let res: Response;
  try {
    // the part is read first (at most UPLOAD_CHUNK_MIB): a body of known length, no chunked transfer encoding
    const body = await readPart(part);
    res = await fetch(absolute(`/api/admin/local-models/uploads/${id}?offset=${offset}`), {
      method: "PUT",
      credentials: "same-origin",
      headers: { ...headersFor("PUT", false), "Content-Type": "application/octet-stream" },
      body,
      signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0, "Нет связи с сервером");
  }
  if (res.ok) return (await res.json()) as ModelUpload;
  if (res.status === 401) unauthorizedListeners.forEach((fn) => fn());
  const err = await errorFrom(res);
  if (isCsrfRejection(res, err) && !retried) {
    await refreshCsrf();
    return sendPart(id, offset, part, signal, true);
  }
  const have = res.headers.get("Upload-Offset");
  if (res.status === 409 && have !== null) throw new OffsetMismatch(err.detail, Number(have));
  throw err;
}

/** OpenAI content part: text, or an image as a data URL (the portal takes png, jpeg and webp) */
export type ContentPart = { type: "text"; text: string } | { type: "image_url"; image_url: { url: string } };

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string | ContentPart[];
}

/** Streams the playground answer; calls onDelta for every text chunk. */
export async function streamChat(
  body: { model: string; messages: ChatMessage[]; max_tokens?: number },
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  for (let attempt = 0; ; attempt++) {
    try {
      res = await fetch(absolute("/api/playground/chat"), {
        method: "POST",
        credentials: "same-origin",
        headers: { ...headersFor("POST", true), Accept: "text/event-stream" },
        body: JSON.stringify(body),
        signal,
      });
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      throw new ApiError(0, "Нет связи с сервером");
    }
    if (res.ok) break;
    if (res.status === 401) unauthorizedListeners.forEach((fn) => fn());
    const err = await errorFrom(res);
    if (!isCsrfRejection(res, err)) throw err;
    if (attempt > 0) throw new ApiError(403, SESSION_STALE);
    await refreshCsrf();
  }
  if (!res.body) throw new ApiError(res.status, "Пустой ответ сервера");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  // stop reading (and close the connection) the moment the user presses stop, even before the first chunk
  const onAbort = () => void reader.cancel().catch(() => undefined);
  signal?.addEventListener("abort", onAbort, { once: true });
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (payload === "[DONE]") return;
        let json: { error?: { message?: string }; choices?: { delta?: { content?: string } }[] } | null = null;
        try {
          json = JSON.parse(payload);
        } catch {
          continue;
        }
        if (json?.error) throw new ApiError(res.status, json.error.message ?? "Ошибка модели");
        const delta = json?.choices?.[0]?.delta?.content;
        if (typeof delta === "string" && delta) onDelta(delta);
      }
    }
  } catch (e) {
    if ((e as Error).name === "AbortError") return;
    throw e;
  } finally {
    signal?.removeEventListener("abort", onAbort);
  }
}
