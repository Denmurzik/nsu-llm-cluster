import type { Deployment, DeploymentBody, KvCacheType, MmprojFile, Reasoning, Sampling } from "../types";

/** The llama.cpp settings of a deployment as the form holds them (numbers stay text while they are typed). */
export interface LlmSettings {
  reasoning: Reasoning;
  kv: KvCacheType;
  sampling: Record<SamplingKey, string>;
  draftOn: boolean;
  draftRepo: string;
  draftFile: string | null;
  mmprojOn: boolean;
  mmprojFile: string | null;
  systemPrompt: string;
  experimental: boolean;
  extraArgs: string;
}

export type SamplingKey = keyof Sampling;

export interface SamplingField {
  key: SamplingKey;
  label: string;
  min: number;
  max: number;
  step: number;
  integer: boolean;
}

/** Same ranges as the portal (app/deployments/llama_args.py SAMPLING_LIMITS). */
export const SAMPLING_FIELDS: SamplingField[] = [
  { key: "temperature", label: "Температура", min: 0, max: 2, step: 0.1, integer: false },
  { key: "top_p", label: "Top-p", min: 0, max: 1, step: 0.05, integer: false },
  { key: "top_k", label: "Top-k", min: 0, max: 1000, step: 1, integer: true },
  { key: "min_p", label: "Min-p", min: 0, max: 1, step: 0.01, integer: false },
  { key: "repeat_penalty", label: "Штраф за повторы", min: 0.5, max: 2, step: 0.05, integer: false },
];

export const MAX_SYSTEM_PROMPT = 8000;
export const MAX_EXTRA_ARGS = 2000;
export const MAX_EXTRA_TOKENS = 40;

export const REASONING_LABEL: Record<Reasoning, string> = { off: "Выключено", on: "Включено", auto: "Авто" };
export const KV_HINT: Record<KvCacheType, string> = {
  f16: "Точный кэш, занимает больше всего памяти",
  q8_0: "Вдвое меньше памяти под контекст почти без потери качества",
  q4_0: "Вчетверо меньше памяти, качество на длинных контекстах заметно ниже",
};

const EMPTY_SAMPLING: Record<SamplingKey, string> = {
  temperature: "",
  top_p: "",
  top_k: "",
  min_p: "",
  repeat_penalty: "",
};

/** A new deployment: no reasoning, q8_0 cache (the portal's default for new ones), nothing else. */
export const DEFAULT_LLM: LlmSettings = {
  reasoning: "off",
  kv: "q8_0",
  sampling: EMPTY_SAMPLING,
  draftOn: false,
  draftRepo: "",
  draftFile: null,
  mmprojOn: false,
  mmprojFile: null,
  systemPrompt: "",
  experimental: false,
  extraArgs: "",
};

export function llmFromDeployment(d: Deployment): LlmSettings {
  return {
    reasoning: d.reasoning ?? "off",
    kv: d.kv_cache_type ?? "f16",
    sampling: {
      ...EMPTY_SAMPLING,
      ...Object.fromEntries(Object.entries(d.sampling ?? {}).map(([k, v]) => [k, String(v)])),
    },
    draftOn: !!d.draft_repo,
    draftRepo: d.draft_repo ?? "",
    draftFile: d.draft_file ?? null,
    mmprojOn: !!d.mmproj_file,
    mmprojFile: d.mmproj_file ?? null,
    systemPrompt: d.system_prompt ?? "",
    experimental: d.experimental ?? false,
    extraArgs: d.extra_args ?? "",
  };
}

/** The sampling object of the form: only the filled fields, numbers; null when none. */
export function samplingOf(s: LlmSettings): Sampling | null {
  const out: Sampling = {};
  for (const f of SAMPLING_FIELDS) {
    const raw = s.sampling[f.key].trim();
    if (raw !== "") out[f.key] = Number(raw);
  }
  return Object.keys(out).length > 0 ? out : null;
}

/** Every llama.cpp field of a create request. */
export function llmBody(s: LlmSettings): Pick<
  DeploymentBody,
  | "reasoning"
  | "kv_cache_type"
  | "sampling"
  | "draft_repo"
  | "draft_file"
  | "mmproj_file"
  | "extra_args"
  | "system_prompt"
  | "experimental"
> {
  return {
    reasoning: s.reasoning,
    kv_cache_type: s.kv,
    sampling: samplingOf(s),
    draft_repo: s.draftOn && s.draftFile ? s.draftRepo.trim() : null,
    draft_file: s.draftOn && s.draftFile ? s.draftFile : null,
    mmproj_file: s.mmprojOn && s.mmprojFile ? s.mmprojFile : null,
    extra_args: s.extraArgs.trim() === "" ? null : s.extraArgs.trim(),
    system_prompt: s.systemPrompt.trim() === "" ? null : s.systemPrompt,
    experimental: s.experimental,
  };
}

const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/** The fields of the form that differ from the saved deployment, for a PATCH. */
export function llmChanges(s: LlmSettings, d: Deployment): Partial<DeploymentBody> {
  const now = llmBody(s);
  const was = llmBody(llmFromDeployment(d));
  const body: Partial<DeploymentBody> = {};
  for (const key of Object.keys(now) as (keyof typeof now)[]) {
    if (!same(now[key], was[key])) (body as Record<string, unknown>)[key] = now[key];
  }
  // a draft is one choice: repo and file always travel together
  if ("draft_repo" in body || "draft_file" in body) {
    body.draft_repo = now.draft_repo;
    body.draft_file = now.draft_file;
  }
  return body;
}

/** Fields that restart the pods when changed (the system prompt and the experimental mark are the gateway's). */
export const LLM_RESTART_KEYS = [
  "reasoning",
  "kv_cache_type",
  "sampling",
  "draft_repo",
  "draft_file",
  "mmproj_file",
  "extra_args",
] as const;

/** mmproj to offer first: F16, then BF16, then the rest by name. */
export function pickMmproj(files: MmprojFile[]): string | null {
  if (files.length === 0) return null;
  const rank = (f: MmprojFile) => {
    const q = (f.quant ?? "").toUpperCase();
    return q === "F16" ? 0 : q === "BF16" ? 1 : 2;
  };
  return [...files].sort((a, b) => rank(a) - rank(b) || a.file.localeCompare(b.file))[0].file;
}

// ---------- own flags: the portal's rules (llama_args.py validate_extra_args), checked while typing ----------

/** Flags the portal or the node sets itself, normalised (`normalizeFlag`); the same list as the portal's
 * FORBIDDEN_FLAGS (a portal test compares them). */
export const FORBIDDEN_FLAGS: readonly string[] = [
  "--alias", "--api-key", "--api-key-file", "--api-prefix", "--cache-list", "--cache-type-k", "--cache-type-k-draft",
  "--cache-type-v", "--cache-type-v-draft", "--chat-template-file", "--completion-bash", "--control-vector",
  "--cpu-moe", "--cpu-moe-draft", "--ctx-size", "--ctx-size-draft", "--device", "--device-draft", "--docker-repo",
  "--embedding", "--embeddings", "--file", "--fit", "--fit-ctx", "--fit-target", "--gpu-layers", "--gpu-layers-draft",
  "--grammar-file", "--help", "--hf-file", "--hf-file-draft", "--hf-file-mmproj", "--hf-file-v", "--hf-repo",
  "--hf-repo-draft", "--hf-repo-v", "--hf-token", "--host", "--json-schema-file", "--list-devices", "--log-file",
  "--lookup-cache-dynamic", "--lookup-cache-static", "--lora", "--lora-scaled", "--main-gpu", "--media-path",
  "--metrics", "--min-p", "--mmproj", "--mmproj-auto", "--mmproj-device", "--mmproj-offload", "--mmproj-url",
  "--model", "--model-draft", "--model-url", "--model-vocoder", "--models-autoload", "--models-dir", "--models-max",
  "--models-memory-margin", "--models-preset", "--n-cpu-moe", "--n-cpu-moe-draft", "--n-gpu-layers",
  "--n-gpu-layers-draft", "--no-host", "--no-mmproj", "--no-mmproj-auto", "--no-mmproj-offload",
  "--no-models-autoload", "--no-slots", "--no-webui", "--offline", "--override-tensor", "--override-tensor-draft",
  "--parallel", "--path", "--pooling", "--port", "--prompt-cache", "--props", "--reasoning", "--reasoning-budget",
  "--reasoning-format", "--repeat-penalty", "--rerank", "--reranking", "--reuse-port", "--rpc", "--slot-save-path",
  "--slots", "--spec-draft-cpu-moe", "--spec-draft-device", "--spec-draft-hf", "--spec-draft-model",
  "--spec-draft-n-cpu-moe", "--spec-draft-ncmoe", "--spec-draft-ngl", "--spec-draft-override-tensor",
  "--spec-draft-type-k", "--spec-draft-type-v", "--split-mode", "--ssl-cert-file", "--ssl-key-file", "--static-path",
  "--temp", "--temperature", "--tensor-split", "--threads", "--tools", "--top-k", "--top-p", "--ui", "--usage",
  "--version", "--webui", "--webui-config-file", "-a", "-c", "-cd", "-cl", "-cmoe", "-cmoed", "-ctk", "-ctkd", "-ctv",
  "-ctvd", "-dev", "-devd", "-dr", "-f", "-fit", "-fitc", "-fitt", "-h", "-hf", "-hfd", "-hff", "-hffd", "-hffv",
  "-hfr", "-hfrd", "-hfrv", "-hft", "-hfv", "-jf", "-lcd", "-lcs", "-m", "-md", "-mg", "-mm", "-mmdev", "-mmu", "-mu",
  "-mv", "-ncmoe", "-ncmoed", "-ngl", "-ngld", "-np", "-ot", "-otd", "-rea", "-sm", "-t", "-ts",
];
const PORTAL_OWNED = "Флаги с файлами, путями и ключами задаются порталом";
const PORTAL_SUFFIXES = ["-file", "-path", "-dir", "-url"];
const PORTAL_PARTS = ["cache-static", "cache-dynamic", "-key"];

/** llama.cpp reads `_` as `-` in a long flag and the portal compares in lower case; a short flag stays as typed. */
export const normalizeFlag = (name: string) => (name.startsWith("--") ? name.replaceAll("_", "-").toLowerCase() : name);

const looksLikePath = (v: string) => v.includes("/") || v.includes("\\") || v.startsWith("~") || v === "." || v === "..";

/** posix shlex.split: whitespace separates, single and double quotes group (a backslash is refused before). */
function shellSplit(text: string): string[] | null {
  const tokens: string[] = [];
  let cur = "";
  let started = false;
  let quote: string | null = null;
  for (const ch of text) {
    if (quote) {
      if (ch === quote) quote = null;
      else cur += ch;
    } else if (ch === "'" || ch === '"') {
      quote = ch;
      started = true;
    } else if (/\s/.test(ch)) {
      if (started) tokens.push(cur);
      cur = "";
      started = false;
    } else {
      cur += ch;
      started = true;
    }
  }
  if (quote) return null;
  if (started) tokens.push(cur);
  return tokens;
}

/** The first problem of the own flags in the portal's words, or null. */
export function validateExtraArgs(text: string): string | null {
  if (text.trim() === "") return null;
  if (text.length > MAX_EXTRA_ARGS) return `Свои флаги не длиннее ${MAX_EXTRA_ARGS} символов`;
  if (text.includes("\\"))
    return "Пути к файлам в своих флагах запрещены: обратная косая черта не допускается";
  const tokens = shellSplit(text);
  if (tokens === null) return "В своих флагах не закрыта кавычка";
  if (tokens.length > MAX_EXTRA_TOKENS) return `В своих флагах не больше ${MAX_EXTRA_TOKENS} частей`;
  for (const [i, token] of tokens.entries()) {
    if (token === "") return "Пустое значение флага: уберите пустые кавычки";
    if (!/^[ -~]*$/.test(token)) return `В своих флагах допустимы только печатные символы ASCII: '${token}'`;
    if (token.startsWith("-") && token.length > 1) {
      const eq = token.indexOf("=");
      const name = eq < 0 ? token : token.slice(0, eq);
      const flag = normalizeFlag(name);
      if (FORBIDDEN_FLAGS.includes(flag)) return `Флаг ${name} задаётся порталом`;
      if (flag.startsWith("--") && (PORTAL_SUFFIXES.some((x) => flag.endsWith(x)) || PORTAL_PARTS.some((x) => flag.includes(x))))
        return `${PORTAL_OWNED}: ${name}`;
      if (eq >= 0 && looksLikePath(token.slice(eq + 1))) return `Пути к файлам в своих флагах запрещены: ${token}`;
    } else {
      if (i === 0) return `Свои флаги начинаются с «-»: ${token}`;
      if (looksLikePath(token)) return `Пути к файлам в своих флагах запрещены: ${token}`;
    }
  }
  return null;
}

export interface LlmErrors {
  sampling?: Partial<Record<SamplingKey, string>>;
  draft?: string;
  mmproj?: string;
  systemPrompt?: string;
  extraArgs?: string;
}

export function checkLlm(s: LlmSettings): LlmErrors {
  const e: LlmErrors = {};
  for (const f of SAMPLING_FIELDS) {
    const raw = s.sampling[f.key].trim();
    if (raw === "") continue;
    const v = Number(raw);
    const msg = !Number.isFinite(v)
      ? "нужно число"
      : f.integer && !Number.isInteger(v)
        ? "нужно целое число"
        : v < f.min || v > f.max
          ? `от ${f.min} до ${f.max}`
          : null;
    if (msg) (e.sampling ??= {})[f.key] = msg;
  }
  if (s.draftOn && (!s.draftRepo.trim() || !s.draftFile)) e.draft = "Выберите файл черновой модели";
  if (s.mmprojOn && !s.mmprojFile) e.mmproj = "Выберите файл проектора";
  if (s.systemPrompt.length > MAX_SYSTEM_PROMPT) e.systemPrompt = `Не больше ${MAX_SYSTEM_PROMPT} символов`;
  const args = validateExtraArgs(s.extraArgs);
  if (args) e.extraArgs = args;
  return e;
}

export const hasLlmErrors = (e: LlmErrors) => Object.keys(e).length > 0;
