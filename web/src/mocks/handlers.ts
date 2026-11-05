import { delay, http, HttpResponse } from "msw";
import meUser from "./fixtures/get__api_me.json";
import meAdmin from "./fixtures/get__api_me--admin.json";
import publicStatus from "./fixtures/get__api_public_status.json";
import models from "./fixtures/get__api_models.json";
import keys from "./fixtures/get__api_keys.json";
import createdKey from "./fixtures/post__api_keys.json";
import usage from "./fixtures/get__api_usage.json";
import deployments from "./fixtures/get__api_admin_deployments.json";
import deploymentDetail from "./fixtures/get__api_admin_deployments_id.json";
import deploymentStatus from "./fixtures/get__api_admin_deployments_id_status.json";
import hfFiles from "./fixtures/get__api_admin_hf_files.json";
import hfEstimate from "./fixtures/get__api_admin_hf_estimate.json";
import hfEstimateSplit from "./fixtures/get__api_admin_hf_estimate--split.json";
import nodes from "./fixtures/get__api_admin_nodes.json";
import join from "./fixtures/get__api_admin_join.json";
import status from "./fixtures/get__api_admin_status.json";
import versions from "./fixtures/get__api_admin_versions.json";
import users from "./fixtures/get__api_admin_users.json";
import settings from "./fixtures/get__api_admin_settings.json";
import userKeys from "./fixtures/get__api_admin_users_id_keys.json";
import audit from "./fixtures/get__api_admin_audit.json";
import podLogs from "./fixtures/get__api_admin_pods_name_logs.json";
import localModels from "./fixtures/get__api_admin_local_models.json";
import deploymentCommand from "./fixtures/get__api_admin_deployments_id_command.json";
import modelCache from "./fixtures/get__api_admin_cache.json";
import prefetchJobs from "./fixtures/get__api_admin_prefetch.json";
import metricsLive from "./fixtures/get__api_admin_metrics_live.json";
import metricsNames from "./fixtures/get__api_admin_metrics_names.json";
import metricsHistoryNode from "./fixtures/get__api_admin_metrics_history.json";
import metricsHistoryModel from "./fixtures/get__api_admin_metrics_history--model.json";
import type {
  AdminUserFull,
  ApiKey,
  AuditEntry,
  KeyLimitsJob,
  LocalCatalog,
  MetricsHistory,
  ModelCache,
  ModelUpload,
  PrefetchJob,
  SystemSetting,
} from "../types";

export const fixtures = {
  meUser,
  meAdmin,
  publicStatus,
  models,
  keys,
  createdKey,
  usage,
  deployments,
  deploymentDetail,
  deploymentStatus,
  hfFiles,
  hfEstimate,
  hfEstimateSplit,
  nodes,
  join,
  status,
  versions,
  users,
  settings,
  userKeys,
  audit,
  podLogs,
  localModels,
  deploymentCommand,
  modelCache,
  prefetchJobs,
  metricsLive,
  metricsNames,
  metricsHistoryNode,
  metricsHistoryModel,
};

type Json = Record<string, unknown>;

export interface MockState {
  /** null means no session: /api/me answers 401 */
  role: "user" | "admin" | null;
  keys: Json[];
  deployments: Json[];
  assignments: Record<string, string | null>;
  maintenance: Record<string, boolean>;
  /** nodes removed from the cluster with «Удалить из кластера…» */
  removed: string[];
  nextKeyId: number;
  nextDeploymentId: number;
  /** «Мои модели»: the catalog the mock serves; uploads change in place */
  local: LocalCatalog;
  nextUploadId: number;
  /** «Кэш моделей»: the cache of the mirror and its background downloads */
  cache: ModelCache;
  prefetch: PrefetchJob[];
  nextJobId: number;
  /** «Настройки»: the settings the mock serves; saves and resets change them in place */
  settings: SystemSetting[];
  /** the mirror limit the running mirror uses (a restart applies the saved one) */
  mirrorApplied: number;
  /** the background job «Применить к существующим ключам»; each poll moves it on */
  keyJob: KeyLimitsJob;
  /** «Пользователи»: the rows the mock serves; a PATCH changes them in place */
  users: AdminUserFull[];
  /** keys per user id; Анна Петрова has the fixture keys, the others none */
  userKeys: Record<string, ApiKey[]>;
  /** the audit log, newest first */
  audit: AuditEntry[];
}

const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v));

const PETROVA = "7c1f6a3e-5b0d-4a52-9d44-0e2b8c1f3a10";

function fresh(role: MockState["role"] = "user"): MockState {
  return {
    role,
    keys: clone(keys) as Json[],
    deployments: clone(deployments) as Json[],
    assignments: Object.fromEntries(nodes.nodes.map((n) => [n.name, n.deployment])),
    maintenance: Object.fromEntries(nodes.nodes.map((n) => [n.name, n.maintenance])),
    removed: [],
    nextKeyId: 100,
    nextDeploymentId: 100,
    local: clone(localModels) as unknown as LocalCatalog,
    nextUploadId: 1,
    cache: clone(modelCache) as unknown as ModelCache,
    prefetch: clone(prefetchJobs.jobs) as unknown as PrefetchJob[],
    nextJobId: 1,
    settings: clone(settings) as unknown as SystemSetting[],
    mirrorApplied: 500,
    keyJob: { state: "none" },
    users: clone(users.items) as unknown as AdminUserFull[],
    userKeys: { [PETROVA]: clone(userKeys) as unknown as ApiKey[] },
    audit: clone(audit.items) as unknown as AuditEntry[],
  };
}

export const state: MockState = fresh();

export function resetState(role: MockState["role"] = "user") {
  Object.assign(state, fresh(role));
}

const MIB = 2 ** 20;
const OVERHEAD = 512 * MIB;

/** The projector files the mock finds in a repository (the portal lists `mmproj*.gguf`) */
const MMPROJ_FILES = [
  { file: "mmproj-BF16.gguf", size_bytes: 990 * MIB, quant: "BF16" },
  { file: "mmproj-F16.gguf", size_bytes: 990 * MIB, quant: "F16" },
  { file: "mmproj-F32.gguf", size_bytes: 1980 * MIB, quant: "F32" },
];

/** A shortened command of the form; the portal builds the real one (builders.command_preview) */
export function previewFor(body: Json) {
  const sampling = (body.sampling ?? {}) as Record<string, number>;
  const flags: Record<string, string> = { temperature: "--temp", top_p: "--top-p", top_k: "--top-k", min_p: "--min-p", repeat_penalty: "--repeat-penalty" };
  const parallel = Number(body.parallel ?? 1);
  const cache = "/var/lib/llm-models";
  const dir = (repo: string) => `${cache}/${repo.replace("/", "__")}`;
  const argv = [
    "llama-server --host 0.0.0.0 --port 18080 --jinja --metrics --cache-ram 512 -fa auto",
    `--ctx-size ${Number(body.ctx_per_request ?? 8192) * parallel} --parallel ${parallel} --no-kv-unified`,
    "-ngl 99 -t $THREADS",
    `--alias ${String(body.name)}`,
    `--reasoning ${String(body.reasoning ?? "off")}`,
    ...(body.kv_cache_type && body.kv_cache_type !== "f16" ? [`--cache-type-k ${body.kv_cache_type} --cache-type-v ${body.kv_cache_type}`] : []),
    ...Object.entries(sampling).map(([k, v]) => `${flags[k]} ${v}`),
    `-m ${dir(String(body.repo))}/${String(body.file)}`,
    body.mmproj_file ? `--mmproj ${dir(String(body.repo))}/${String(body.mmproj_file)}` : "--no-mmproj",
    ...(body.draft_repo ? [`-md ${dir(String(body.draft_repo))}/${String(body.draft_file)} --spec-type draft-simple --spec-draft-n-max 4 --spec-draft-n-min 1 -ngld 99`] : []),
    ...(body.extra_args ? [String(body.extra_args)] : []),
  ];
  return {
    leader: `LLAMA_API_KEY=*** ${argv.join(" ")}`,
    rpc: null,
    notes: ["-t: число ядер ноды без одного, KEEP_MODEL_REPOS и HF_ENDPOINT нода получает от портала."],
  };
}

/** Same formulas as portal/app/catalog/estimate.py */
export function estimateFor(params: URLSearchParams) {
  const meta = hfEstimate.meta;
  const ctx = Number(params.get("ctx_per_request") ?? 8192);
  const parallel = Number(params.get("parallel") ?? 1);
  const group = Math.max(1, Number(params.get("group_size") ?? 1));
  const names = (params.get("nodes") ?? "").split(",").filter(Boolean);
  const weights = meta.weights_bytes;
  // the KV bytes follow the cache type: f16 2 bytes per element, q8_0 34/32, q4_0 18/32 (estimate.py KV_BYTES_PER_ELEM)
  const elem = { f16: 2, q8_0: 34 / 32, q4_0: 18 / 32 }[params.get("kv_cache_type") ?? "f16"] ?? 2;
  const kv = Math.ceil(2 * meta.n_layers * meta.n_kv_heads * meta.head_dim * ctx * parallel * elem);
  // a draft model (700 MiB and its small f16 cache) and a projector (900 MiB), as the portal adds them
  const draftWeights = params.get("draft_repo") ? 700 * MIB : 0;
  const draftKv = params.get("draft_repo") ? Math.ceil(2 * 12 * 2 * 64 * ctx * parallel * 2) : 0;
  const mmproj = params.get("mmproj_file") ? 900 * MIB : 0;
  const total = weights + kv + draftWeights + draftKv + mmproj;
  const perNode = Math.ceil((weights + kv) / group) + draftWeights + draftKv + mmproj + OVERHEAD;
  const known = hfEstimate.nodes;
  const list = names.length ? known.filter((n) => names.includes(n.name)) : known;
  const capacities = list.map((n) => n.capacity_bytes).sort((a, b) => b - a);
  let recommended: number | null = null;
  for (let g = 1; g <= capacities.length; g++) {
    if (Math.ceil(total / g) + OVERHEAD <= capacities[g - 1]) {
      recommended = g;
      break;
    }
  }
  return {
    meta,
    estimate: {
      weights_bytes: weights,
      kv_bytes: kv,
      total_bytes: total,
      per_node_bytes: perNode,
      draft_weights_bytes: draftWeights,
      draft_kv_bytes: draftKv,
      mmproj_bytes: mmproj,
    },
    nodes: list.map((n) => ({ name: n.name, capacity_bytes: n.capacity_bytes, fits: perNode <= n.capacity_bytes })),
    recommended_group_size: recommended,
    ctx_ok: ctx >= 256 && ctx <= meta.max_ctx,
    // the placement is the portal's (app/catalog/placement.py); the mock shows a fixed one for split
    placement: params.get("mode") === "split" ? hfEstimateSplit.placement : null,
  };
}

/** PUT of an upload part, exported so a test can wrap it (a delay, a failure first) */
export async function putUploadPart({
  request,
  params,
}: {
  request: Request;
  params: Record<string, string | readonly string[] | undefined>;
}) {
  if (!csrfOk(request)) return forbidden();
  const up = state.local.uploads.find((u) => u.id === params.id);
  if (!up) return HttpResponse.json({ detail: "Загрузка не найдена" }, { status: 404 });
  if (up.status !== "uploading") return HttpResponse.json({ detail: "Загрузка отменена" }, { status: 409 });
  const offset = Number(new URL(request.url).searchParams.get("offset"));
  if (offset !== up.received_bytes)
    return HttpResponse.json(
      { detail: `Смещение ${offset} не совпадает с уже принятым объёмом: ${up.received_bytes} байт` },
      { status: 409, headers: { "Upload-Offset": String(up.received_bytes) } },
    );
  const bytes = (await request.arrayBuffer()).byteLength;
  up.received_bytes = Math.min(up.size_bytes, offset + bytes);
  return HttpResponse.json(up);
}

function csrfOk(request: Request): boolean {
  const me = state.role === "admin" ? meAdmin : meUser;
  return request.headers.get("X-CSRF-Token") === me.csrf_token;
}

const forbidden = () => HttpResponse.json({ detail: "CSRF" }, { status: 403 });
const needLogin = () => HttpResponse.json({ detail: "Требуется вход" }, { status: 401 });
const needAdmin = () => HttpResponse.json({ detail: "Недостаточно прав" }, { status: 403 });

function adminOnly(handler: (ctx: { request: Request; params: Record<string, string | readonly string[] | undefined> }) => Response | Promise<Response>) {
  return (ctx: { request: Request; params: Record<string, string | readonly string[] | undefined> }) => {
    if (!state.role) return needLogin();
    if (state.role !== "admin") return needAdmin();
    return handler(ctx);
  };
}

function sse(chunks: string[], gapMs = 0): Response {
  const enc = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      for (const text of chunks) {
        if (gapMs) await delay(gapMs);
        const payload = JSON.stringify({ choices: [{ delta: { content: text } }] });
        controller.enqueue(enc.encode(`data: ${payload}\n\n`));
      }
      controller.enqueue(enc.encode("data: [DONE]\n\n"));
      controller.close();
    },
  });
  return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
}

// -- task 2-32: users and the audit log ---------------------------------------------------------------------------
function usersList(q: URLSearchParams) {
  const text = (q.get("q") ?? "").trim().toLowerCase();
  const role = q.get("role");
  const blocked = q.get("blocked");
  const rows = state.users.filter(
    (u) =>
      (!text || u.email.toLowerCase().includes(text) || u.full_name.toLowerCase().includes(text)) &&
      (!role || u.role === role) &&
      (blocked === null || String(u.blocked) === blocked),
  );
  const offset = Number(q.get("offset") ?? 0);
  const limit = Number(q.get("limit") ?? 50);
  return HttpResponse.json({ items: rows.slice(offset, offset + limit), total: rows.length });
}

const conflict = (detail: string) => HttpResponse.json({ detail }, { status: 409 });

/** The rules of the portal (api/admin_users.py): not oneself, a manual role, a blocked user and the limits */
async function userPatch({ request, params }: { request: Request; params: Record<string, string | readonly string[] | undefined> }) {
  if (!csrfOk(request)) return forbidden();
  const user = state.users.find((u) => u.id === params.id);
  if (!user) return HttpResponse.json({ detail: "Пользователь не найден" }, { status: 404 });
  const body = (await request.json()) as Partial<AdminUserFull> & { role_source?: "auto" };
  const myself = user.id === meAdmin.id;
  if (myself && body.blocked) return conflict("Нельзя заблокировать самого себя");
  if (myself && body.role === "user") return conflict("Нельзя снять роль администратора с самого себя");
  if (body.role) Object.assign(user, { role: body.role, role_source: "manual" });
  if (body.role_source === "auto") Object.assign(user, { role_source: "auto", role: user.email === meAdmin.email ? "admin" : "user" });
  if (body.blocked === true) Object.assign(user, { blocked: true, blocked_reason: body.blocked_reason || null });
  if (body.blocked === false) Object.assign(user, { blocked: false, blocked_reason: null });
  for (const f of ["rpm_limit", "tpm_limit", "max_keys"] as const) if (f in body) user[f] = body[f] ?? null;
  return HttpResponse.json(user);
}

function auditList(q: URLSearchParams) {
  const has = (value: string | null | undefined, part: string | null) =>
    !part || (value ?? "").toLowerCase().includes(part.toLowerCase());
  const since = q.get("since");
  const until = q.get("until");
  const rows = state.audit.filter(
    (e) =>
      (!q.get("action") || e.action.startsWith(q.get("action") as string)) &&
      has(e.actor_email, q.get("actor")) &&
      has(e.target, q.get("target")) &&
      (!since || e.at >= since) &&
      (!until || e.at <= until),
  );
  const offset = Number(q.get("offset") ?? 0);
  const limit = Number(q.get("limit") ?? 50);
  return HttpResponse.json({ items: rows.slice(offset, offset + limit), total: rows.length });
}

// -- task 2-24: the probe, waking a node and the models that take images -----------------------------------------
/** the catalog fixture has `vision: false` everywhere (the contract test pins its shape); the mock turns it on for one
    model so the image flow can be tried and tested */
const VISION_MOCK = new Set(["gemma-4-e4b"]);

/** «Проверить»: the third deployment answers with an error, a switched-off one is refused like the portal does */
async function probeHandler({ request, params }: { request: Request; params: Record<string, string | readonly string[] | undefined> }) {
  if (!csrfOk(request)) return forbidden();
  const dep = state.deployments.find((d) => d.id === Number(params.id));
  if (!dep) return HttpResponse.json({ detail: "Развёртывание не найдено" }, { status: 404 });
  if (dep.enabled === false) return HttpResponse.json({ detail: "Развёртывание выключено" }, { status: 409 });
  await delay(600);
  if (dep.name === "llama-3-1-8b")
    return HttpResponse.json({ ok: false, status: 503, answer: "", ttft_ms: null, tokens_per_s: null, error: "Модель не отвечает: нет готовых экземпляров" });
  return HttpResponse.json({ ok: true, status: 200, answer: "Москва", ttft_ms: 820, tokens_per_s: 62.4, error: null });
}

/** Queues downloads like the mirror: a file that is queued or running already keeps its job */
function queueJobs(repo: string, revision: string, files: string[]): PrefetchJob[] {
  const out: PrefetchJob[] = [];
  for (const path of new Set(files)) {
    let job = state.prefetch.find((j) => j.repo === repo && j.path === path && (j.state === "queued" || j.state === "running"));
    if (!job) {
      job = {
        id: String(state.nextJobId++).padStart(12, "0"),
        repo,
        revision,
        path,
        state: "queued",
        size: null,
        written: 0,
        speed_bps: 0,
        error: null,
        cached: false,
        created: Date.now() / 1000,
        started: null,
        finished: null,
      };
      state.prefetch.push(job);
    }
    out.push(job);
  }
  return out;
}

// -- task 2-33: monitoring ---------------------------------------------------------------------------------------
/** The history of the fixture repeated over the asked period, one point per step (the real one is made of the controllers' minute rows) */
export function historyFor(url: URL): MetricsHistory {
  const kind = url.searchParams.get("kind") === "model" ? "model" : "node";
  const base = kind === "model" ? metricsHistoryModel : metricsHistoryNode;
  const to = new Date(url.searchParams.get("to") ?? Date.now()).getTime();
  const from = new Date(url.searchParams.get("from") ?? to - 3600_000).getTime();
  const step = Math.max(60, Math.ceil((to - from) / 1000 / 300 / 60) * 60);
  const count = Math.min(300, Math.floor((to - from) / 1000 / step));
  const points = Array.from({ length: count }, (_, i) => ({
    ...base.points[i % base.points.length],
    t: new Date(to - (count - 1 - i) * step * 1000).toISOString(),
  }));
  return { ...base, kind, name: url.searchParams.get("name") ?? base.name, from: new Date(from).toISOString(), to: new Date(to).toISOString(), step, points } as MetricsHistory;
}

/** The rules of the portal (app/settings_store.py) for a value sent to a setting */
function settingError(s: SystemSetting, value: unknown): string | null {
  if (s.type === "bool") return typeof value === "boolean" ? null : `«${s.key}»: нужно значение да/нет`;
  if (typeof value !== "number" || typeof value === "boolean") return `«${s.key}»: нужно число`;
  if (s.type === "int" && !Number.isInteger(value)) return `«${s.key}»: нужно целое число`;
  if (s.min !== null && value < s.min) return `«${s.key}»: не меньше ${s.min}`;
  if (s.max !== null && value > s.max) return `«${s.key}»: не больше ${s.max}`;
  return null;
}

/** a setting as the portal sends it: the restart marker of the mirror limit follows what the running mirror has */
function settingOut(s: SystemSetting): SystemSetting {
  const overridden = s.overridden;
  return {
    ...s,
    overridden,
    source: overridden ? "db" : "env",
    pending_restart: s.restart_required && s.value !== state.mirrorApplied,
  };
}

function settingByKey(key: unknown): SystemSetting | undefined {
  return state.settings.find((x) => x.key === key);
}

const UNKNOWN_SETTING = () => HttpResponse.json({ detail: "Неизвестная настройка" }, { status: 404 });

/** one poll of the job: the first shows it running, the next ones finish it */
function pollKeyJob(): KeyLimitsJob {
  const job = state.keyJob;
  if (job.state === "running") {
    const total = job.total ?? 0;
    const done = Math.min(total, (job.done ?? 0) + Math.ceil(total / 2));
    state.keyJob = done >= total ? { ...job, state: "done", done, finished_at: new Date().toISOString() } : { ...job, done };
  }
  return state.keyJob;
}

export const handlers = [
  http.get("/api/admin/metrics/live", adminOnly(() => HttpResponse.json(metricsLive))),
  http.get("/api/admin/metrics/names", adminOnly(() => HttpResponse.json(metricsNames))),
  http.get("/api/admin/metrics/history", adminOnly(({ request }) => HttpResponse.json(historyFor(new URL(request.url))))),
  http.post("/api/admin/deployments/:id/probe", adminOnly(probeHandler)),

  http.get("/api/me", () => {
    if (!state.role) return needLogin();
    return HttpResponse.json(state.role === "admin" ? meAdmin : meUser);
  }),
  http.get("/api/public/status", () => HttpResponse.json(publicStatus)),
  http.get("/api/models", () =>
    state.role ? HttpResponse.json(models.map((m) => ({ ...m, vision: VISION_MOCK.has(m.name) }))) : needLogin(),
  ),

  http.get("/api/keys", () => (state.role ? HttpResponse.json(state.keys) : needLogin())),
  http.post("/api/keys", async ({ request }) => {
    if (!state.role) return needLogin();
    if (!csrfOk(request)) return forbidden();
    const body = (await request.json()) as { alias: string; expires_days: number | null };
    if (state.keys.some((k) => k.alias === body.alias))
      return HttpResponse.json({ detail: "Ключ с таким названием уже есть" }, { status: 409 });
    if (state.keys.length >= 5) return HttpResponse.json({ detail: "Достигнут лимит ключей: 5" }, { status: 409 });
    const id = `00000000-0000-4000-8000-${String(state.nextKeyId++).padStart(12, "0")}`;
    const created = { ...createdKey, id, alias: body.alias };
    state.keys.unshift({
      id,
      alias: body.alias,
      masked: createdKey.masked,
      created_at: createdKey.created_at,
      expires_at: body.expires_days ? createdKey.expires_at : null,
      tokens_30d: 0,
      requests_30d: 0,
    });
    return HttpResponse.json(created, { status: 201 });
  }),
  http.delete("/api/keys/:id", ({ request, params }) => {
    if (!state.role) return needLogin();
    if (!csrfOk(request)) return forbidden();
    const id = String(params.id);
    if (!state.keys.some((k) => k.id === id)) return HttpResponse.json({ detail: "Ключ не найден" }, { status: 404 });
    state.keys = state.keys.filter((k) => k.id !== id);
    return new HttpResponse(null, { status: 204 });
  }),
  http.get("/api/usage", ({ request }) => {
    if (!state.role) return needLogin();
    const days = Number(new URL(request.url).searchParams.get("days") ?? 7);
    return HttpResponse.json({ ...usage, days, by_day: usage.by_day.slice(-days) });
  }),
  http.post("/api/playground/chat", ({ request }) => {
    if (!state.role) return needLogin();
    if (!csrfOk(request)) return forbidden();
    return sse(["Привет", ", это ", "ответ модели"], 5);
  }),

  http.get("/api/admin/deployments", adminOnly(() => HttpResponse.json(state.deployments))),
  http.post(
    "/api/admin/deployments",
    adminOnly(async ({ request }) => {
      if (!csrfOk(request)) return forbidden();
      const body = (await request.json()) as Json;
      if (state.deployments.some((d) => d.name === body.name))
        return HttpResponse.json({ detail: "Развёртывание с таким именем уже есть" }, { status: 409 });
      const { nodes: assigned, ...fields } = body;
      const dep = { ...fields, id: state.nextDeploymentId++ };
      state.deployments.push(dep);
      return HttpResponse.json({ ...dep, assigned_nodes: assigned ?? [] }, { status: 201 });
    }),
  ),
  http.post(
    "/api/admin/deployments/command-preview",
    adminOnly(async ({ request }) => {
      if (!csrfOk(request)) return forbidden();
      const body = (await request.json()) as Json;
      const args = String(body.extra_args ?? "");
      const forbiddenFlag = args.split(/\s+/).find((t) => ["--port", "--host", "--api-key"].includes(t.split("=")[0]));
      if (forbiddenFlag) return HttpResponse.json({ detail: `Флаг ${forbiddenFlag.split("=")[0]} задаётся порталом` }, { status: 422 });
      return HttpResponse.json(previewFor(body));
    }),
  ),
  http.get(
    "/api/admin/deployments/:id/command",
    adminOnly(({ params }) =>
      state.deployments.some((d) => d.id === Number(params.id))
        ? HttpResponse.json(deploymentCommand)
        : HttpResponse.json({ detail: "Развёртывание не найдено" }, { status: 404 }),
    ),
  ),
  http.get(
    "/api/admin/hf/mmproj",
    adminOnly(({ request }) => {
      const repo = new URL(request.url).searchParams.get("repo") ?? "";
      return HttpResponse.json(repo.includes("gemma") ? MMPROJ_FILES : []);
    }),
  ),
  http.get(
    "/api/admin/deployments/:id/status",
    adminOnly(() => HttpResponse.json(deploymentStatus)),
  ),
  http.get(
    "/api/admin/deployments/:id",
    adminOnly(({ params }) => {
      const dep = state.deployments.find((d) => d.id === Number(params.id));
      if (!dep) return HttpResponse.json({ detail: "Развёртывание не найдено" }, { status: 404 });
      return HttpResponse.json({ ...deploymentDetail, ...dep });
    }),
  ),
  http.patch(
    "/api/admin/deployments/:id",
    adminOnly(async ({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const body = (await request.json()) as Json;
      const dep = state.deployments.find((d) => d.id === Number(params.id));
      if (!dep) return HttpResponse.json({ detail: "Развёртывание не найдено" }, { status: 404 });
      const { nodes: assigned, ...fields } = body;
      Object.assign(dep, fields);
      return HttpResponse.json({ ...deploymentDetail, ...dep, assigned_nodes: assigned ?? deploymentDetail.assigned_nodes });
    }),
  ),
  http.post(
    "/api/admin/deployments/:id/refresh-revision",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const dep = state.deployments.find((d) => d.id === Number(params.id));
      if (!dep) return HttpResponse.json({ detail: "Развёртывание не найдено" }, { status: 404 });
      return HttpResponse.json({ changed: false, deployment: { ...deploymentDetail, ...dep } });
    }),
  ),
  http.delete(
    "/api/admin/deployments/:id",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      state.deployments = state.deployments.filter((d) => d.id !== Number(params.id));
      return new HttpResponse(null, { status: 204 });
    }),
  ),
  http.put(
    "/api/admin/nodes/:name/assignment",
    adminOnly(async ({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const body = (await request.json()) as { deployment_id: number | null };
      const dep = state.deployments.find((d) => d.id === body.deployment_id);
      state.assignments[String(params.name)] = dep ? String(dep.name) : null;
      return HttpResponse.json({ node: params.name, deployment_id: body.deployment_id });
    }),
  ),
  http.put(
    "/api/admin/nodes/:name/maintenance",
    adminOnly(async ({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const body = (await request.json()) as { maintenance: boolean };
      state.maintenance[String(params.name)] = body.maintenance;
      return HttpResponse.json({ node: params.name, maintenance: body.maintenance, deployment_id: null });
    }),
  ),
  http.post(
    "/api/admin/nodes/:name/wake",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      return HttpResponse.json({ node: params.name, targets: ["192.168.3.255", "192.168.3.77"], relay: null });
    }),
  ),
  http.delete(
    "/api/admin/nodes/:name",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const name = String(params.name);
      const node = nodes.nodes.find((n) => n.name === name);
      if (!node || state.removed.includes(name)) return HttpResponse.json({ detail: "Нода не найдена" }, { status: 404 });
      if (node.is_main) return HttpResponse.json({ detail: "Главный сервер нельзя удалить" }, { status: 409 });
      const held = state.assignments[name];
      const force = new URL(request.url).searchParams.get("force") === "1";
      if (held && !force) {
        const mode = (state.deployments.find((d) => d.name === held)?.mode as string | undefined) ?? "replicas";
        return HttpResponse.json(
          {
            detail: `Нода работает на развёртываниях: ${held}${mode === "split" ? " (split)" : ""}. Они потеряют её. Удалить всё равно можно с подтверждением`,
            deployments: [{ name: held, mode }],
          },
          { status: 409 },
        );
      }
      state.removed.push(name);
      return HttpResponse.json({
        node: name,
        online: node.ready,
        k8s: "deleted",
        unassigned: held ? [held] : [],
        uninstall: {
          windows: `.\\install-node.ps1 -Uninstall -Name ${name}`,
          linux: `sudo ./run-node.sh --uninstall --main 192.168.3.21 --name ${name}`,
        },
      });
    }),
  ),
  http.post(
    "/api/admin/nodes/:name/purge-models",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      return HttpResponse.json({
        node: params.name,
        status: "done",
        freed_bytes: 12884901888,
        models_bytes: 6442450944,
        removed: ["unsloth__old-model-GGUF", "rpc"],
        skipped: [],
      });
    }),
  ),
  http.get(
    "/api/admin/hf/files",
    adminOnly(({ request }) => {
      const url = new URL(request.url);
      const repo = url.searchParams.get("repo") ?? "";
      if (repo.includes("missing")) return HttpResponse.json({ detail: "Репозиторий или файл не найден" }, { status: 404 });
      if (repo.includes("hf-down")) return HttpResponse.json({ detail: "Hugging Face недоступен" }, { status: 502 });
      if (url.searchParams.get("engine") === "vllm")
        return HttpResponse.json({ size_bytes: 16060522496, meta: hfEstimate.meta });
      return HttpResponse.json(hfFiles);
    }),
  ),
  http.get(
    "/api/admin/hf/estimate",
    adminOnly(({ request }) => HttpResponse.json(estimateFor(new URL(request.url).searchParams))),
  ),
  http.get(
    "/api/admin/nodes",
    adminOnly(() =>
      HttpResponse.json({
        ...nodes,
        nodes: nodes.nodes
          .filter((n) => !state.removed.includes(n.name))
          .map((n) => ({
            ...n,
            deployment: state.assignments[n.name] ?? null,
            maintenance: state.maintenance[n.name] ?? false,
          })),
      }),
    ),
  ),
  http.get("/api/admin/local-models", adminOnly(() => HttpResponse.json(state.local))),
  http.post(
    "/api/admin/local-models/scan",
    adminOnly(({ request }) => (csrfOk(request) ? HttpResponse.json(state.local) : forbidden())),
  ),
  http.post(
    "/api/admin/local-models/uploads",
    adminOnly(async ({ request }) => {
      if (!csrfOk(request)) return forbidden();
      const body = (await request.json()) as { model: string; file: string; size_bytes: number; sha256?: string | null };
      const same = state.local.uploads.find((u) => u.model === body.model && u.file === body.file && u.status === "uploading");
      if (same) {
        if (same.size_bytes !== body.size_bytes)
          return HttpResponse.json(
            { detail: "Этот файл уже загружается с другим размером: отмените прежнюю загрузку" },
            { status: 409 },
          );
        return HttpResponse.json(same);
      }
      const up: ModelUpload = {
        ...(localModels.uploads[0] as ModelUpload),
        id: `00000000-0000-4000-8000-${String(state.nextUploadId++).padStart(12, "0")}`,
        model: body.model,
        file: body.file,
        size_bytes: body.size_bytes,
        received_bytes: 0,
        sha256_expected: body.sha256 ?? null,
        chunk_bytes: 4,
        status: "uploading",
      };
      state.local.uploads.push(up);
      return HttpResponse.json(up, { status: 201 });
    }),
  ),
  http.get(
    "/api/admin/local-models/uploads/:id",
    adminOnly(({ params }) => {
      const up = state.local.uploads.find((u) => u.id === params.id);
      return up ? HttpResponse.json(up) : HttpResponse.json({ detail: "Загрузка не найдена" }, { status: 404 });
    }),
  ),
  http.put("/api/admin/local-models/uploads/:id", adminOnly(putUploadPart)),
  http.post(
    "/api/admin/local-models/uploads/:id/finish",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const up = state.local.uploads.find((u) => u.id === params.id);
      if (!up) return HttpResponse.json({ detail: "Загрузка не найдена" }, { status: 404 });
      up.status = "verifying";
      return HttpResponse.json(up, { status: 202 });
    }),
  ),
  http.delete(
    "/api/admin/local-models/uploads/:id",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      state.local.uploads = state.local.uploads.filter((u) => u.id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
  ),
  http.delete(
    "/api/admin/local-models/:name",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const model = state.local.models.find((m) => m.name === params.name);
      if (!model) return HttpResponse.json({ detail: "Своя модель не найдена" }, { status: 404 });
      const file = new URL(request.url).searchParams.get("file");
      const users = file ? (model.files.find((f) => f.file === file)?.used_by ?? []) : model.used_by;
      if (users.length)
        return HttpResponse.json({ detail: `Модель используется развёртываниями: ${users.join(", ")}` }, { status: 409 });
      if (file) model.files = model.files.filter((f) => f.file !== file);
      else state.local.models = state.local.models.filter((m) => m.name !== params.name);
      return new HttpResponse(null, { status: 204 });
    }),
  ),
  // -- task 2-29: the cache of the mirror and the downloads into it ---------------------------------------------------
  http.get("/api/admin/cache", adminOnly(() => HttpResponse.json(state.cache))),
  http.delete(
    "/api/admin/cache/*",
    adminOnly(({ request }) => {
      if (!csrfOk(request)) return forbidden();
      const [owner, name, ...rest] = decodeURIComponent(new URL(request.url).pathname.slice("/api/admin/cache/".length)).split("/");
      const repo = `${owner}/${name}`;
      const path = rest.join("/");
      const hit = state.cache.files.filter((f) => f.repo === repo && (!path || f.path === path));
      if (hit.length === 0) return HttpResponse.json({ detail: "В кэше нет такого файла" }, { status: 404 });
      const users = [...new Set(hit.flatMap((f) => f.used_by.map((u) => u.name)))];
      if (users.length)
        return HttpResponse.json(
          { detail: `Файл используется развёртыванием ${users.join(", ")}: удалите развёртывание или выберите другую модель` },
          { status: 409 },
        );
      if (hit.some((f) => f.busy || f.downloading))
        return HttpResponse.json({ detail: "Файл сейчас отдаётся нодам или скачивается: попробуйте позже" }, { status: 409 });
      const freed = hit.reduce((s, f) => s + f.size, 0);
      state.cache.files = state.cache.files.filter((f) => !hit.includes(f));
      state.cache.used_bytes -= freed;
      return HttpResponse.json({ deleted: hit.map((f) => ({ repo: f.repo, path: f.path, size: f.size })), freed_bytes: freed });
    }),
  ),
  http.get("/api/admin/prefetch", adminOnly(() => HttpResponse.json({ jobs: state.prefetch, concurrency: 2 }))),
  http.post(
    "/api/admin/prefetch",
    adminOnly(async ({ request }) => {
      if (!csrfOk(request)) return forbidden();
      const body = (await request.json()) as { repo: string; revision?: string | null; files: string[] };
      if (!/^[\w.-]+\/[\w.-]+$/.test(body.repo))
        return HttpResponse.json({ detail: "Репозиторий указывается как владелец/название" }, { status: 422 });
      return HttpResponse.json({ jobs: queueJobs(body.repo, body.revision || "main", body.files) }, { status: 202 });
    }),
  ),
  http.delete(
    "/api/admin/prefetch/:id",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const job = state.prefetch.find((j) => j.id === params.id);
      if (!job) return HttpResponse.json({ detail: "Нет такого задания" }, { status: 404 });
      if (job.state === "queued" || job.state === "running")
        Object.assign(job, { state: "cancelled", error: "Скачивание отменено", finished: Date.now() / 1000 });
      else state.prefetch = state.prefetch.filter((j) => j !== job);
      return HttpResponse.json(job);
    }),
  ),
  http.delete(
    "/api/admin/prefetch",
    adminOnly(({ request }) => {
      if (!csrfOk(request)) return forbidden();
      const before = state.prefetch.length;
      state.prefetch = state.prefetch.filter((j) => j.state === "queued" || j.state === "running");
      return HttpResponse.json({ removed: before - state.prefetch.length });
    }),
  ),
  http.post(
    "/api/admin/deployments/:id/prefetch",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const dep = state.deployments.find((d) => d.id === Number(params.id));
      if (!dep) return HttpResponse.json({ detail: "Развёртывание не найдено" }, { status: 404 });
      const repo = String(dep.repo);
      if (repo.startsWith("local/"))
        return HttpResponse.json(
          { detail: "У развёртывания только свои файлы (local/…): они лежат на главном, скачивать нечего" },
          { status: 422 },
        );
      const files = [String(dep.file), ...(dep.mmproj_file ? [String(dep.mmproj_file)] : [])];
      return HttpResponse.json({ jobs: queueJobs(repo, String(dep.revision || "main"), files) }, { status: 202 });
    }),
  ),
  http.get("/api/admin/join", adminOnly(() => HttpResponse.json(join))),
  http.get("/api/admin/status", adminOnly(() => HttpResponse.json(status))),
  // versions of the node image and «Обновить» (portal task 2-30)
  http.get("/api/admin/versions", adminOnly(() => HttpResponse.json(versions))),
  http.post(
    "/api/admin/nodes/update",
    adminOnly(async ({ request }) => {
      if (!csrfOk(request)) return forbidden();
      const body = (await request.json()) as { names: string[]; when: string };
      return HttpResponse.json({ results: body.names.map((name) => ({ name, ok: true, when: body.when, waiting: false })) });
    }),
  ),
  http.post(
    "/api/admin/nodes/:name/update",
    adminOnly(async ({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const body = (await request.json()) as { when: string };
      return HttpResponse.json({ name: params.name, ok: true, when: body.when, waiting: false });
    }),
  ),
  http.delete(
    "/api/admin/nodes/:name/update",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      return HttpResponse.json({ name: params.name, cancelled: true });
    }),
  ),
  http.get("/api/admin/settings", adminOnly(() => HttpResponse.json(state.settings.map(settingOut)))),
  http.put(
    "/api/admin/settings/:key",
    adminOnly(async ({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const s = settingByKey(params.key);
      if (!s) return UNKNOWN_SETTING();
      const { value } = (await request.json()) as { value: unknown };
      const error = settingError(s, value);
      if (error) return HttpResponse.json({ detail: error }, { status: 422 });
      s.value = value as number | boolean;
      s.overridden = true;
      s.updated_by = "admin@g.nsu.ru";
      s.updated_at = new Date().toISOString();
      return HttpResponse.json(settingOut(s));
    }),
  ),
  http.delete(
    "/api/admin/settings/:key",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const s = settingByKey(params.key);
      if (!s) return UNKNOWN_SETTING();
      s.value = s.default;
      s.overridden = false;
      s.updated_by = null;
      s.updated_at = null;
      return HttpResponse.json(settingOut(s));
    }),
  ),
  http.post(
    "/api/admin/key-limits/apply",
    adminOnly(({ request }) => {
      if (!csrfOk(request)) return forbidden();
      if (state.keyJob.state === "running") return HttpResponse.json({ detail: "Обновление ключей уже идёт" }, { status: 409 });
      const get = (key: string) => Number(settingByKey(key)?.value);
      state.keyJob = {
        state: "running",
        total: 4,
        done: 0,
        failed: 0,
        rpm_limit: get("key_rpm_limit"),
        tpm_limit: get("key_tpm_limit"),
        started_at: new Date().toISOString(),
        finished_at: null,
        error: null,
      };
      return HttpResponse.json(state.keyJob, { status: 202 });
    }),
  ),
  http.get("/api/admin/key-limits/job", adminOnly(() => HttpResponse.json(pollKeyJob()))),
  http.post(
    "/api/admin/hf-mirror/restart",
    adminOnly(({ request }) => {
      if (!csrfOk(request)) return forbidden();
      const s = settingByKey("hf_mirror_max_gib");
      if (!s) return UNKNOWN_SETTING();
      state.mirrorApplied = Number(s.value);
      return HttpResponse.json(settingOut(s));
    }),
  ),
  http.get("/api/admin/users", adminOnly(({ request }) => usersList(new URL(request.url).searchParams))),
  http.patch("/api/admin/users/:id", adminOnly(userPatch)),
  http.get(
    "/api/admin/users/:id/keys",
    adminOnly(({ params }) => HttpResponse.json(state.userKeys[String(params.id)] ?? [])),
  ),
  http.delete(
    "/api/admin/users/:id/keys/:keyId",
    adminOnly(({ request, params }) => {
      if (!csrfOk(request)) return forbidden();
      const list = state.userKeys[String(params.id)] ?? [];
      if (!list.some((k) => k.id === params.keyId)) return HttpResponse.json({ detail: "Ключ не найден" }, { status: 404 });
      state.userKeys[String(params.id)] = list.filter((k) => k.id !== params.keyId);
      const user = state.users.find((u) => u.id === params.id);
      if (user) user.keys = Math.max(0, user.keys - 1);
      return new HttpResponse(null, { status: 204 });
    }),
  ),
  http.get("/api/admin/audit/export", adminOnly(() => new HttpResponse("id,time\n", { headers: { "Content-Type": "text/csv" } }))),
  http.get("/api/admin/audit", adminOnly(({ request }) => auditList(new URL(request.url).searchParams))),
  http.get(
    "/api/admin/pods/:name/logs",
    adminOnly(({ params }) => HttpResponse.json({ ...podLogs, name: params.name })),
  ),
];
