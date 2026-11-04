import { CopyPlus, Trash2 } from "lucide-react";
import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, type EstimateParams } from "../api";
import { CommandPreview } from "../components/CommandPreview";
import { EstimatePanel, useEstimate } from "../components/EstimatePanel";
import { LlmSettings } from "../components/LlmSettings";
import { Confirm } from "../components/Modal";
import { NodePicker, takenNodes, TakeOverConfirm } from "../components/NodePicker";
import { NodeRamFields } from "../components/NodeRamFields";
import { PodStatusTable } from "../components/PodStatusTable";
import { Cells, StatusLabel } from "../components/Status";
import { ErrorNotice, Ic, Loading, Notice, PageHead } from "../components/ui";
import { ACCEL_LABEL, ENGINE_LABEL } from "../lib/labels";
import {
  checkLlm,
  hasLlmErrors,
  llmBody,
  llmChanges,
  llmFromDeployment,
  LLM_RESTART_KEYS,
  type LlmSettings as LlmState,
} from "../lib/llm";
import { checkOperation, checkRules, hasErrors } from "../lib/rules";
import { useResource } from "../lib/useResource";
import type {
  Accel,
  Deployment,
  DeploymentBody,
  DeployMode,
  DeploymentStatus,
  ModelStatus,
  NodeInfo,
  Schedule,
} from "../types";

const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));

/** ISO weekdays, Monday is 1 */
const DAYS: [number, string][] = [
  [1, "Пн"],
  [2, "Вт"],
  [3, "Ср"],
  [4, "Чт"],
  [5, "Пт"],
  [6, "Сб"],
  [7, "Вс"],
];
const WORKDAYS = [1, 2, 3, 4, 5];
/** the end of a window that runs to midnight; `<input type="time">` cannot show it */
const DAY_END = "24:00";
/** the portal defaults (SPLIT_REGROUP_GRACE_S, NODE_NOTREADY_GRACE_S) when the deployment cannot tell */
const GRACE_DEFAULT: Record<DeployMode, number> = { split: 600, replicas: 120 };
/** the portal default (SCHEDULE_DRAIN_S) when the deployment cannot tell */
const DRAIN_DEFAULT = 60;

const sameSchedule = (a: Schedule | null, b: Schedule | null) => JSON.stringify(a) === JSON.stringify(b);

function liveStatus(status: DeploymentStatus | undefined): ModelStatus {
  if (!status || status.ready_backends === null) return "unknown";
  if (status.ready_backends > 0) return "ready";
  return status.nodes.length > 0 ? "starting" : "down";
}

export function AdminDeploymentEdit() {
  const { id } = useParams();
  const depId = Number(id);
  const dep = useResource(() => api.deployment(depId), [depId]);
  const status = useResource(() => api.deploymentStatus(depId), [depId], { interval: 5000 });
  const nodes = useResource(() => api.nodes(), []);
  const [version, setVersion] = useState(0);
  const [saved, setSaved] = useState(false);

  if (dep.error && !dep.data) {
    return (
      <div className="page">
        <PageHead title="Развёртывание" />
        <ErrorNotice error={dep.error} />
        <Link to="/admin/deployments">К списку</Link>
      </div>
    );
  }
  if (!dep.data || nodes.loading) {
    return (
      <div className="page">
        <Loading />
      </div>
    );
  }

  const d = dep.data;
  const live = d.enabled ? liveStatus(status.data) : null;

  return (
    <div className="page">
      <PageHead
        title={d.name}
        lead={
          <span className="mono small">
            {d.repo}
            {d.file ? ` / ${d.file}` : ""}
          </span>
        }
        actions={
          <>
            {d.engine !== "vllm" && (
              <Link className="btn" to={`/admin/deployments/new?from=${d.id}`}>
                <Ic icon={CopyPlus} />
                Создать похожее
              </Link>
            )}
            <Link className="btn quiet" to="/admin/deployments">
              К списку
            </Link>
          </>
        }
      />

      <div className="statusbar">
        <span className="status">
          {live ? <StatusLabel status={live} /> : <StatusLabel status="unknown" off />}
          {live && <Cells ready={status.data?.ready_backends ?? null} status={live} />}
        </span>
        <span className="muted small">
          Хеш конфигурации <span className="mono">{status.data?.spec_hash ?? d.spec_hash ?? "—"}</span>
        </span>
        <span className="muted small">
          Движок {ENGINE_LABEL[d.engine]}, ускоритель {ACCEL_LABEL[d.accel]}
        </span>
      </div>

      {!d.repo.startsWith("local/") && (
        <RevisionBlock
          dep={d}
          onChanged={async () => {
            await dep.reload();
            await status.reload();
            setVersion((v) => v + 1);
          }}
        />
      )}

      <EditForm
        key={`${d.id}-${version}`}
        dep={d}
        allNodes={(nodes.data?.nodes ?? []).filter((n) => !n.is_main)}
        onSaved={async () => {
          await dep.reload();
          await status.reload();
          setSaved(true);
          setVersion((v) => v + 1);
        }}
      />
      {saved && (
        <p className="secondary" role="status">
          Изменения сохранены
        </p>
      )}

      <section className="section">
        <h2>Состояние на нодах</h2>
        {status.error && !status.data && <ErrorNotice error={status.error} />}
        {status.data?.placement_problems?.map((p) => (
          <Notice key={p} kind="error">
            {p}
          </Notice>
        ))}
        {status.data ? <PodStatusTable status={status.data} /> : !status.error && <Loading />}
      </section>

      <DeleteBlock dep={d} />
    </div>
  );
}

function EditForm({
  dep,
  allNodes,
  onSaved,
}: {
  dep: Deployment;
  allNodes: NodeInfo[];
  onSaved: () => Promise<void>;
}) {
  const initialNodes = dep.assigned_nodes ?? [];
  const [enabled, setEnabled] = useState(dep.enabled);
  const [autoAssign, setAutoAssign] = useState(dep.auto_assign);
  const [mode, setMode] = useState<DeployMode>(dep.mode);
  const [accel, setAccel] = useState<Accel>(dep.accel);
  const [confirming, setConfirming] = useState(false);
  const [group, setGroup] = useState(String(dep.group_size));
  const [ctx, setCtx] = useState(String(dep.ctx_per_request));
  const [parallel, setParallel] = useState(String(dep.parallel));
  const [useNodeRam, setUseNodeRam] = useState(dep.use_node_ram ?? true);
  const [ramPct, setRamPct] = useState(String(dep.ram_budget_pct ?? 70));
  const [selected, setSelected] = useState<string[]>(initialNodes);
  const [spare, setSpare] = useState(String(dep.spare_nodes ?? 0));
  const [grace, setGrace] = useState(dep.regroup_grace_s == null ? "" : String(dep.regroup_grace_s));
  const [scheduled, setScheduled] = useState(!!dep.schedule);
  const [days, setDays] = useState<number[]>(dep.schedule?.days ?? WORKDAYS);
  const [start, setStart] = useState(dep.schedule?.start ?? "08:00");
  const [end, setEnd] = useState(dep.schedule?.end ?? "20:00");
  /** the last clock time of the end, restored when «До конца дня» is unchecked */
  const lastEnd = useRef(dep.schedule?.end && dep.schedule.end !== DAY_END ? dep.schedule.end : "20:00");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [llm, setLlm] = useState<LlmState>(() => llmFromDeployment(dep));
  const [previewError, setPreviewError] = useState<string | null>(null);

  const isVllm = dep.engine === "vllm";
  const ctxNum = Number(ctx);
  const parallelNum = Number(parallel);
  const groupNum = Number(group);
  const ramPctNum = Number(ramPct);

  const lastGood = useRef({ ctx: dep.ctx_per_request, parallel: dep.parallel, ramPct: dep.ram_budget_pct ?? 70 });
  if (Number.isInteger(ctxNum) && ctxNum >= 1) lastGood.current.ctx = ctxNum;
  if (Number.isInteger(parallelNum) && parallelNum >= 1) lastGood.current.parallel = parallelNum;
  if (Number.isInteger(ramPctNum) && ramPctNum >= 10 && ramPctNum <= 90) lastGood.current.ramPct = ramPctNum;

  // nodes offered in the picker: everything known plus anything still assigned
  const pickable: NodeInfo[] = [
    ...allNodes,
    ...initialNodes
      .filter((n) => !allNodes.some((x) => x.name === n))
      .map((n) => ({
        name: n,
        source: "k8s" as const,
        accel: "cpu" as const,
        ip: null,
        ready: false,
        vram_mib: null,
        mem_mib: null,
        capacity_bytes: null,
        deployment: dep.name,
        backends: 0,
        version: null,
        is_main: false,
        maintenance: false,
        wakeable: false,
        wake: null,
      })),
  ];

  const params: EstimateParams = {
    repo: dep.repo,
    file: dep.file,
    engine: dep.engine,
    ctx_per_request: lastGood.current.ctx,
    parallel: lastGood.current.parallel,
    group_size: mode === "split" ? Math.max(2, groupNum || 2) : 1,
    nodes: selected.length ? selected : pickable.map((n) => n.name),
    ...(mode === "split" ? { mode: "split", use_node_ram: useNodeRam, ram_budget_pct: lastGood.current.ramPct } : {}),
    ...(isVllm
      ? {}
      : {
          kv_cache_type: llm.kv,
          draft_repo: llm.draftOn && llm.draftFile ? llm.draftRepo.trim() : null,
          draft_file: llm.draftOn ? llm.draftFile : null,
          mmproj_file: llm.mmprojOn ? llm.mmprojFile : null,
        }),
  };
  const estimate = useEstimate(params);
  const maxCtx = estimate.data?.meta.max_ctx ?? dep.meta?.max_ctx;

  const errors = checkRules({
    ctx: ctxNum,
    parallel: parallelNum,
    mode,
    group: groupNum,
    accel,
    maxCtx,
    useNodeRam,
    ramPct: mode === "split" ? ramPctNum : undefined,
  });
  const schedule: Schedule | null = scheduled ? { days: [...days].sort((a, b) => a - b), start, end } : null;
  const opErrors = checkOperation({ spare, grace, schedule });
  const llmErrors = isVllm ? {} : checkLlm(llm);
  const graceDefault =
    dep.regroup_grace_s == null && mode === dep.mode && dep.regroup_grace_effective_s != null
      ? dep.regroup_grace_effective_s
      : GRACE_DEFAULT[mode];

  const body: Partial<DeploymentBody> = {};
  if (ctxNum !== dep.ctx_per_request) body.ctx_per_request = ctxNum;
  if (parallelNum !== dep.parallel) body.parallel = parallelNum;
  if (mode !== dep.mode) body.mode = mode;
  if (accel !== dep.accel) body.accel = accel;
  if (mode === "split" && (mode !== dep.mode || groupNum !== dep.group_size)) body.group_size = groupNum;
  if (mode === "split" && useNodeRam !== (dep.use_node_ram ?? true)) body.use_node_ram = useNodeRam;
  if (mode === "split" && ramPctNum !== (dep.ram_budget_pct ?? 70)) body.ram_budget_pct = ramPctNum;
  if (enabled !== dep.enabled) body.enabled = enabled;
  if (autoAssign !== dep.auto_assign) body.auto_assign = autoAssign;
  if (!sameSet(selected, initialNodes)) body.nodes = selected;
  if (mode === "split" && Number(spare) !== (dep.spare_nodes ?? 0)) body.spare_nodes = Number(spare);
  const graceValue = grace.trim() === "" ? null : Number(grace);
  if (graceValue !== (dep.regroup_grace_s ?? null)) body.regroup_grace_s = graceValue;
  if (!sameSchedule(schedule, dep.schedule ?? null)) body.schedule = schedule;
  if (!isVllm) Object.assign(body, llmChanges(llm, dep));

  const dirty = Object.keys(body).length > 0;
  const restarts =
    "ctx_per_request" in body ||
    "parallel" in body ||
    "mode" in body ||
    "group_size" in body ||
    "accel" in body ||
    "use_node_ram" in body ||
    "ram_budget_pct" in body ||
    LLM_RESTART_KEYS.some((k) => k in body);
  const canSave = dirty && !hasErrors(errors) && !hasErrors(opErrors) && !hasLlmErrors(llmErrors) && !saving;

  // the command of the saved deployment, or of the form with its changes once it differs
  const formBody: DeploymentBody = {
    name: dep.name,
    engine: dep.engine,
    repo: dep.repo,
    file: dep.file,
    mode,
    group_size: mode === "split" && Number.isInteger(groupNum) && groupNum >= 2 ? groupNum : dep.group_size,
    accel,
    ctx_per_request: ctxNum,
    parallel: parallelNum,
    auto_assign: autoAssign,
    enabled,
    ...(mode === "split" ? { use_node_ram: useNodeRam, ram_budget_pct: ramPctNum } : {}),
    ...llmBody(llm),
    nodes: [],
  };
  const formWhole = !hasErrors(errors) && !hasLlmErrors(llmErrors);
  const commandChanged = dirty && ("mode" in body || "group_size" in body || "accel" in body || "ctx_per_request" in body ||
    "parallel" in body || "use_node_ram" in body || LLM_RESTART_KEYS.some((k) => k in body) || "system_prompt" in body || "experimental" in body);

  const taken = takenNodes(pickable, selected, dep.name, initialNodes);

  function save(e: FormEvent) {
    e.preventDefault();
    if (!canSave) return;
    if (taken.length > 0) setConfirming(true);
    else void persist();
  }

  async function persist() {
    setConfirming(false);
    setSaving(true);
    setError(null);
    try {
      await api.updateDeployment(dep.id, body);
      await onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Не удалось сохранить");
      setSaving(false);
    }
  }

  return (
    <div className="cols">
      <form className="section" onSubmit={save}>
        <div className="card stack">
          <h2>Параметры</h2>
          {dep.meta_missing && (
            <Notice kind="warn">Метаданные модели ещё не загружены, максимум контекста не проверяется</Notice>
          )}
          {dep.ctx_exceeds_model && (
            <Notice kind="warn">
              Контекст на запрос больше предела модели{dep.meta?.max_ctx ? `: ${dep.meta.max_ctx} токенов` : ""}.
              Уменьшите его, иначе сервер может отвечать с ошибками
            </Notice>
          )}
          <div className="row" style={{ alignItems: "center" }}>
            <label className="check">
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              <span>Развёртывание включено</span>
            </label>
          </div>
          {!isVllm && (
            <fieldset>
              <legend>Режим</legend>
              <div className="choices inline" role="radiogroup" aria-label="Режим">
                <label className="choice">
                  <input type="radio" name="ed-mode" checked={mode === "replicas"} onChange={() => setMode("replicas")} />
                  <span>Реплики</span>
                </label>
                <label className="choice">
                  <input type="radio" name="ed-mode" checked={mode === "split"} onChange={() => setMode("split")} />
                  <span>Разделение по нодам</span>
                </label>
              </div>
            </fieldset>
          )}
          <div className="row">
            <div className="field">
              <label htmlFor="ed-accel">Ускоритель</label>
              <select
                id="ed-accel"
                className="select"
                value={accel}
                disabled={isVllm}
                onChange={(e) => setAccel(e.target.value as Accel)}
              >
                <option value="any">любой</option>
                <option value="cpu">cpu</option>
                <option value="nvidia">nvidia</option>
              </select>
              {errors.accel && <span className="error-text">{errors.accel}</span>}
            </div>
            {mode === "split" && (
              <div className="field">
                <label htmlFor="ed-group">Размер группы</label>
                <input
                  id="ed-group"
                  className="input"
                  type="number"
                  min={2}
                  value={group}
                  onChange={(e) => setGroup(e.target.value)}
                />
                {errors.group && <span className="error-text">{errors.group}</span>}
              </div>
            )}
            <div className="field">
              <label htmlFor="ed-ctx">Контекст на запрос</label>
              <input
                id="ed-ctx"
                className="input"
                type="number"
                min={256}
                value={ctx}
                aria-invalid={!!errors.ctx}
                onChange={(e) => setCtx(e.target.value)}
              />
              {errors.ctx && <span className="error-text">{errors.ctx}</span>}
            </div>
            <div className="field">
              <label htmlFor="ed-parallel">Параллельность</label>
              <input
                id="ed-parallel"
                className="input"
                type="number"
                min={1}
                max={16}
                value={parallel}
                aria-invalid={!!errors.parallel}
                onChange={(e) => setParallel(e.target.value)}
              />
              {errors.parallel && <span className="error-text">{errors.parallel}</span>}
            </div>
          </div>
          {mode === "split" && (
            <NodeRamFields
              idPrefix="ed"
              accel={accel}
              useNodeRam={useNodeRam}
              onUseNodeRam={setUseNodeRam}
              ramPct={ramPct}
              onRamPct={setRamPct}
              error={errors.ramPct}
            />
          )}
        </div>

        {!isVllm && (
          <LlmSettings
            idPrefix="ed"
            value={llm}
            onChange={setLlm}
            errors={llmErrors}
            mainRepo={dep.repo}
            serverError={previewError}
          />
        )}

        <div className="card stack">
          <h2>Ноды</h2>
          <label className="check">
            <input type="checkbox" checked={autoAssign} onChange={(e) => setAutoAssign(e.target.checked)} />
            <span>Назначать свободные ноды автоматически</span>
          </label>
          <NodePicker nodes={pickable} selected={selected} onChange={setSelected} own={dep.name} />
        </div>

        <div className="card stack">
          <h2>Эксплуатация</h2>
          <div className="row">
            {mode === "split" && (
              <div className="field">
                <label htmlFor="ed-spare">Резервные ноды</label>
                <input
                  id="ed-spare"
                  className="input"
                  type="number"
                  min={0}
                  max={100}
                  value={spare}
                  aria-invalid={!!opErrors.spare}
                  onChange={(e) => setSpare(e.target.value)}
                />
                <span className="hint">
                  Не входят в группы: свободная резервная нода заменяет пропавшую сразу, без паузы
                </span>
                {opErrors.spare && <span className="error-text">{opErrors.spare}</span>}
              </div>
            )}
            <div className="field">
              <label htmlFor="ed-grace">Пауза перед пересборкой, с</label>
              <input
                id="ed-grace"
                className="input"
                type="number"
                min={0}
                max={86400}
                placeholder={String(graceDefault)}
                value={grace}
                aria-invalid={!!opErrors.grace}
                onChange={(e) => setGrace(e.target.value)}
              />
              <span className="hint">
                Сколько ждать пропавшую ноду, прежде чем снять её с развёртывания
                {mode === "split" ? " и пересобрать группу; пауза нужна, только когда свободной резервной ноды нет" : ""}.
                Пустое поле: {graceDefault} с по умолчанию
              </span>
              {opErrors.grace && <span className="error-text">{opErrors.grace}</span>}
            </div>
          </div>
          <label className="check">
            <input type="checkbox" checked={scheduled} onChange={(e) => setScheduled(e.target.checked)} />
            <span>Работать по расписанию</span>
          </label>
          {dep.schedule && dep.schedule_active === false && (
            <Notice kind="warn">Сейчас вне окна расписания: развёртывание остановлено, ноды свободны для других развёртываний</Notice>
          )}
          {scheduled && (
            <>
              <fieldset>
                <legend>Дни</legend>
                <div className="choices inline">
                  {DAYS.map(([n, label]) => (
                    <label key={n} className="check">
                      <input
                        type="checkbox"
                        checked={days.includes(n)}
                        onChange={(e) => setDays(e.target.checked ? [...days, n] : days.filter((d) => d !== n))}
                      />
                      <span>{label}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <div className="row">
                <div className="field">
                  <label htmlFor="ed-start">Начало окна</label>
                  <input
                    id="ed-start"
                    className="input"
                    type="time"
                    value={start}
                    onChange={(e) => setStart(e.target.value)}
                  />
                </div>
                <div className="field">
                  <label htmlFor="ed-end">Конец окна</label>
                  <input
                    id="ed-end"
                    className="input"
                    type="time"
                    value={end === DAY_END ? "" : end}
                    disabled={end === DAY_END}
                    onChange={(e) => {
                      lastEnd.current = e.target.value;
                      setEnd(e.target.value);
                    }}
                  />
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={end === DAY_END}
                      onChange={(e) => setEnd(e.target.checked ? DAY_END : lastEnd.current)}
                    />
                    <span>До конца дня</span>
                  </label>
                </div>
              </div>
              <span className="hint">
                Время новосибирское. Если конец раньше начала, окно идёт через полночь. В конце окна новые запросы
                перестают поступать, начатые ответы дописываются ещё {dep.schedule_drain_s ?? DRAIN_DEFAULT} с, затем
                поды останавливаются, а ноды берут другие развёртывания с автоназначением
              </span>
              {opErrors.schedule && <span className="error-text">{opErrors.schedule}</span>}
            </>
          )}
        </div>

        {!isVllm && (
          <CommandPreview
            title={commandChanged ? "Команда llama-server с несохранёнными изменениями" : "Команда llama-server"}
            requestKey={commandChanged ? (formWhole ? JSON.stringify(formBody) : null) : `saved:${dep.id}:${dep.spec_hash ?? ""}`}
            request={() => (commandChanged ? api.commandPreview(formBody) : api.deploymentCommand(dep.id))}
            onError={setPreviewError}
          />
        )}

        {restarts && (
          <Notice kind="warn">
            <span className="title">Поды перезапустятся по одной ноде</span>
            <br />
            Пока идёт обновление, остальные ноды продолжают отвечать на запросы.
          </Notice>
        )}
        {error && <ErrorNotice error={error} />}
        <div>
          <button type="submit" className="btn primary" disabled={!canSave}>
            Сохранить
          </button>
        </div>
      </form>
      {confirming && (
        <TakeOverConfirm
          taken={taken}
          confirmLabel="Забрать ноды и сохранить"
          onCancel={() => setConfirming(false)}
          onConfirm={() => void persist()}
        />
      )}

      <div className="section" style={{ position: "sticky", top: 24 }}>
        <EstimatePanel
          state={estimate}
          groupSize={mode === "split" ? Math.max(2, groupNum || 2) : 1}
          split={mode === "split"}
          canApplyGroup={mode === "split"}
          onApplyGroup={(n) => setGroup(String(n))}
        />
      </div>
    </div>
  );
}

/** The pinned Hugging Face commit and «Обновить до последней версии», which pins the commit of main now: a new one
 * reloads the model on the nodes one at a time (a split group restarts). */
function RevisionBlock({ dep, onChanged }: { dep: Deployment; onChanged: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      const r = await api.refreshRevision(dep.id);
      const sha = r.deployment.revision?.slice(0, 7) ?? "main";
      setMessage(
        r.changed
          ? `Зафиксирована версия ${sha}: ноды перезагрузят модель по одной`
          : `Версия ${sha} уже последняя`,
      );
      if (r.changed) await onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Не удалось обновить версию");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="statusbar">
      <span className="muted small">
        Версия модели{" "}
        {dep.revision ? (
          <span className="mono" title={dep.revision}>
            {dep.revision.slice(0, 7)}
          </span>
        ) : (
          <span>main, не зафиксирована</span>
        )}
      </span>
      <button type="button" className="btn quiet" onClick={refresh} disabled={busy}>
        Обновить до последней версии
      </button>
      {message && (
        <span className="secondary small" role="status">
          {message}
        </span>
      )}
      {error && <Notice kind="error">{error}</Notice>}
    </div>
  );
}

function DeleteBlock({ dep }: { dep: Deployment }) {
  const navigate = useNavigate();
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function remove() {
    setBusy(true);
    try {
      await api.deleteDeployment(dep.id);
      navigate("/admin/deployments");
    } catch (err) {
      setAsking(false);
      setBusy(false);
      setError(err instanceof ApiError ? err.detail : "Не удалось удалить");
    }
  }

  return (
    <section className="section">
      <h2>Удаление</h2>
      <p className="secondary">Поды остановятся, модель пропадёт из каталога, ноды освободятся.</p>
      {error && <ErrorNotice error={error} />}
      <div>
        <button type="button" className="btn danger" onClick={() => setAsking(true)}>
          <Ic icon={Trash2} />
          Удалить развёртывание
        </button>
      </div>
      {asking && (
        <Confirm
          title="Удаление развёртывания"
          confirmLabel="Удалить"
          busy={busy}
          onCancel={() => setAsking(false)}
          onConfirm={remove}
        >
          <p>Удалить развёртывание «{dep.name}»? Это действие нельзя отменить.</p>
        </Confirm>
      )}
    </section>
  );
}
