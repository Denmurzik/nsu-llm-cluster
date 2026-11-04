import { Search } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, type EstimateParams } from "../api";
import { fmtBytes, NAME_RE, suggestName } from "../lib/format";
import { checkLlm, DEFAULT_LLM, hasLlmErrors, llmBody, llmFromDeployment, type LlmSettings as LlmState } from "../lib/llm";
import { checkRules, hasErrors } from "../lib/rules";
import { useResource } from "../lib/useResource";
import type { Accel, Deployment, DeploymentBody, DeployMode, HfFile } from "../types";
import { CommandPreview } from "./CommandPreview";
import { EstimatePanel, useEstimate } from "./EstimatePanel";
import { LlmSettings } from "./LlmSettings";
import { LocalSourcePicker } from "./ModelFilePicker";
import { NodePicker, takenNodes, TakeOverConfirm } from "./NodePicker";
import { NodeRamFields } from "./NodeRamFields";
import { ErrorNotice, Ic, Notice } from "./ui";

const NAME_HINT = "Имя: строчные латинские буквы, цифры и дефис, до 40 символов";

type Source = "hf" | "local";

export function DeploymentWizard() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const fromId = Number(searchParams.get("from")) || null;
  /** «Создать похожее»: the deployment the form is copied from */
  const origin = useResource<Deployment | null>(() => (fromId ? api.deployment(fromId) : Promise.resolve(null)), [fromId]);
  const copied = useRef<number | null>(null);
  const [copiedFrom, setCopiedFrom] = useState<string | null>(null);
  const nodesRes = useResource(() => api.nodes(), []);
  const allNodes = (nodesRes.data?.nodes ?? []).filter((n) => !n.is_main);

  const [source, setSource] = useState<Source>("hf");
  // vLLM is not offered for new deployments (existing ones still open on their edit page)
  const engine = "llamacpp";
  const [repo, setRepo] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [files, setFiles] = useState<HfFile[] | null>(null);
  const [file, setFile] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [mode, setMode] = useState<DeployMode>("replicas");
  const [group, setGroup] = useState("2");
  const [accel, setAccel] = useState<Accel>("any");
  const [ctx, setCtx] = useState("8192");
  const [parallel, setParallel] = useState("2");
  const [autoAssign, setAutoAssign] = useState(false);
  const [useNodeRam, setUseNodeRam] = useState(true);
  const [ramPct, setRamPct] = useState("70");
  const [selected, setSelected] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [llm, setLlm] = useState<LlmState>(DEFAULT_LLM);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const ready = file !== null;
  const effMode: DeployMode = mode;
  const effAccel: Accel = accel;
  const ctxNum = Number(ctx);
  const parallelNum = Number(parallel);
  const groupNum = Number(group);
  const ramPctNum = Number(ramPct);
  const isSplit = effMode === "split";

  // keep the last sane numbers while a field is being retyped
  const lastGood = useRef({ ctx: 8192, parallel: 2, ramPct: 70 });
  if (Number.isInteger(ctxNum) && ctxNum >= 1) lastGood.current.ctx = ctxNum;
  if (Number.isInteger(parallelNum) && parallelNum >= 1) lastGood.current.parallel = parallelNum;
  if (Number.isInteger(ramPctNum) && ramPctNum >= 10 && ramPctNum <= 90) lastGood.current.ramPct = ramPctNum;

  const candidates = allNodes.filter((n) => effAccel === "any" || n.accel === effAccel);
  const nodeParam = selected.length ? selected : candidates.map((n) => n.name);

  const params: EstimateParams | null =
    ready && !nodesRes.loading
      ? {
          repo: repo.trim(),
          file,
          engine,
          kv_cache_type: llm.kv,
          draft_repo: llm.draftOn && llm.draftFile ? llm.draftRepo.trim() : null,
          draft_file: llm.draftOn ? llm.draftFile : null,
          mmproj_file: llm.mmprojOn ? llm.mmprojFile : null,
          ctx_per_request: lastGood.current.ctx,
          parallel: lastGood.current.parallel,
          group_size: effMode === "split" ? Math.max(2, Number.isInteger(groupNum) ? groupNum : 2) : 1,
          nodes: nodeParam,
          ...(isSplit ? { mode: "split", use_node_ram: useNodeRam, ram_budget_pct: lastGood.current.ramPct } : {}),
        }
      : null;
  const estimate = useEstimate(params);

  const errors = checkRules({
    ctx: ctxNum,
    parallel: parallelNum,
    mode: effMode,
    group: groupNum,
    accel: effAccel,
    maxCtx: estimate.data?.meta.max_ctx,
    useNodeRam,
    ramPct: isSplit ? ramPctNum : undefined,
  });
  const llmErrors = checkLlm(llm);
  const nameValid = NAME_RE.test(name);
  const blocker = !ready
    ? "Выберите файл модели"
    : hasErrors(errors) || hasLlmErrors(llmErrors)
      ? "Исправьте выделенные поля"
      : !nameValid
        ? name === ""
          ? "Укажите имя развёртывания"
          : "Исправьте имя развёртывания"
        : estimate.pending
          ? "Идёт оценка памяти"
          : null;
  const canSubmit = ready && nameValid && !hasErrors(errors) && !hasLlmErrors(llmErrors) && !estimate.pending && !submitting;
  const taken = takenNodes(candidates, selected, null);

  // a chosen node that no longer fits the accelerator would otherwise stay selected out of sight
  useEffect(() => {
    if (nodesRes.loading) return;
    setSelected((s) => {
      const next = s.filter((name) => candidates.some((c) => c.name === name));
      return next.length === s.length ? s : next;
    });
  }, [effAccel, nodesRes.loading, nodesRes.data]);

  useEffect(() => {
    if (!nameTouched) setName(suggestName(repo));
  }, [repo, nameTouched]);

  function resetSource() {
    setFiles(null);
    setFile(null);
    setSearchError(null);
    // the projector is a file of the model's repository: another repository, another projector
    setLlm((l) => (l.mmprojOn || l.mmprojFile ? { ...l, mmprojOn: false, mmprojFile: null } : l));
  }

  function changeSource(next: Source) {
    if (next === source) return;
    setSource(next);
    setRepo("");
    resetSource();
  }

  // «Создать похожее»: everything but the name and the nodes comes from the deployment in ?from=
  useEffect(() => {
    const d = origin.data;
    if (!d || copied.current === d.id) return;
    copied.current = d.id;
    if (d.engine === "vllm") return;
    setSource(d.repo.startsWith("local/") ? "local" : "hf");
    setRepo(d.repo);
    setFile(d.file);
    setMode(d.mode);
    setGroup(String(d.group_size));
    setAccel(d.accel);
    setCtx(String(d.ctx_per_request));
    setParallel(String(d.parallel));
    setAutoAssign(d.auto_assign);
    setUseNodeRam(d.use_node_ram ?? true);
    setRamPct(String(d.ram_budget_pct ?? 70));
    setLlm(llmFromDeployment(d));
    setName("");
    setNameTouched(true);
    setSelected([]);
    setCopiedFrom(d.name);
    if (!d.repo.startsWith("local/")) {
      api
        .hfFiles(d.repo)
        .then(setFiles)
        .catch(() => undefined); // the chosen file stays; only the list of the others is missing
    }
  }, [origin.data]);

  async function search(e: FormEvent) {
    e.preventDefault();
    const target = repo.trim();
    if (!target || source !== "hf") return;
    setSearching(true);
    resetSource();
    try {
      const list = await api.hfFiles(target);
      setFiles(list);
      if (list.length === 1) setFile(list[0].file);
    } catch (err) {
      setSearchError(err instanceof ApiError ? err.detail : "Не удалось обратиться к Hugging Face");
    } finally {
      setSearching(false);
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    if (taken.length > 0) setConfirming(true);
    else void create();
  }

  function bodyFor(deploymentName: string): DeploymentBody {
    return {
      name: deploymentName,
      engine,
      repo: repo.trim(),
      file,
      mode: effMode,
      group_size: Number.isInteger(groupNum) && groupNum >= 1 ? groupNum : 2,
      accel: effAccel,
      ctx_per_request: ctxNum,
      parallel: parallelNum,
      auto_assign: autoAssign,
      enabled: true,
      ...(isSplit ? { use_node_ram: useNodeRam, ram_budget_pct: ramPctNum } : {}),
      ...llmBody(llm),
      nodes: selected,
    };
  }

  // the command is asked for only while the form is whole: the portal validates it like a create
  const previewKey =
    ready && !hasErrors(errors) && !hasLlmErrors(llmErrors) ? JSON.stringify({ ...bodyFor(name || "model"), nodes: [] }) : null;

  async function create() {
    setConfirming(false);
    setSubmitting(true);
    setSubmitError(null);
    try {
      await api.createDeployment(bodyFor(name));
      navigate("/admin/deployments");
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.detail : "Не удалось создать развёртывание");
      setSubmitting(false);
    }
  }

  return (
    <div className="cols">
      <div className="section">
        <form className="card stack" onSubmit={search}>
          <div className="row" style={{ alignItems: "center", justifyContent: "space-between" }}>
            <h2>Источник</h2>
            <div className="tabs" role="tablist" aria-label="Источник модели">
              <button type="button" role="tab" className="tab" aria-selected={source === "hf"} onClick={() => changeSource("hf")}>
                Hugging Face
              </button>
              <button
                type="button"
                role="tab"
                className="tab"
                aria-selected={source === "local"}
                onClick={() => changeSource("local")}
              >
                Свои файлы
              </button>
            </div>
          </div>
          {source === "local" && (
            <LocalSourcePicker
              repo={repo}
              file={file}
              onPick={(r, f) => {
                setRepo(r);
                setFile(f);
              }}
            />
          )}
          {source === "hf" && (
            <>
              <div className="row end">
                <div className="field grow">
                  <label htmlFor="wz-repo">Репозиторий Hugging Face</label>
                  <input
                    id="wz-repo"
                    className="input mono"
                    value={repo}
                    placeholder="владелец/репозиторий"
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(e) => {
                      setRepo(e.target.value);
                      resetSource();
                    }}
                  />
                </div>
                <button type="submit" className="btn" disabled={!repo.trim() || searching}>
                  <Ic icon={Search} />
                  Найти файлы
                </button>
              </div>
              {searchError && <ErrorNotice error={searchError} />}
              {files && files.length === 0 && <p className="muted">В репозитории нет файлов GGUF</p>}
              {files && files.length > 0 && (
                <fieldset>
                  <legend>Файл модели</legend>
                  <div className="choices" role="radiogroup" aria-label="Файл модели">
                    {files.map((f) => (
                      <label key={f.file} className="choice">
                        <input
                          type="radio"
                          name="wz-file"
                          checked={file === f.file}
                          onChange={() => setFile(f.file)}
                        />
                        <span className="tag">{f.quant ?? "без метки"}</span>{" "}
                        <span className="mono">{fmtBytes(f.size_bytes)}</span>{" "}
                        <span className="grow mono muted small">{f.file}</span>
                        {f.shards.length > 1 && <span className="muted small">{f.shards.length} части</span>}
                      </label>
                    ))}
                  </div>
                </fieldset>
              )}
            </>
          )}
        </form>

        {ready && (
          <form className="section" onSubmit={submit}>
            {copiedFrom && (
              <Notice>
                Скопировано из <span className="mono">{copiedFrom}</span>: название и ноды нужно выбрать заново
              </Notice>
            )}
            <div className="card stack">
              <h2>Параметры</h2>
              <fieldset>
                <legend>Режим</legend>
                <div className="choices inline" role="radiogroup" aria-label="Режим">
                  <label className="choice">
                    <input type="radio" name="wz-mode" checked={mode === "replicas"} onChange={() => setMode("replicas")} />
                    <span>Реплики</span>
                  </label>
                  <label className="choice">
                    <input type="radio" name="wz-mode" checked={mode === "split"} onChange={() => setMode("split")} />
                    <span>Разделение по нодам</span>
                  </label>
                </div>
                <p className="hint" style={{ marginTop: 4 }}>
                  {mode === "split"
                    ? "Одна копия модели делится между несколькими нодами, подходит для моделей, которые не помещаются на одну"
                    : "Каждая назначенная нода держит полную копию модели"}
                </p>
              </fieldset>
              <div className="row">
                {effMode === "split" && (
                  <div className="field">
                    <label htmlFor="wz-group">Размер группы</label>
                    <input
                      id="wz-group"
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
                  <label htmlFor="wz-accel">Ускоритель</label>
                  <select
                    id="wz-accel"
                    className="select"
                    value={effAccel}
                    onChange={(e) => setAccel(e.target.value as Accel)}
                  >
                    <option value="any">любой</option>
                    <option value="cpu">cpu</option>
                    <option value="nvidia">nvidia</option>
                  </select>
                  {errors.accel && <span className="error-text">{errors.accel}</span>}
                </div>
              </div>
              {isSplit && (
                <NodeRamFields
                  idPrefix="wz"
                  accel={effAccel}
                  useNodeRam={useNodeRam}
                  onUseNodeRam={setUseNodeRam}
                  ramPct={ramPct}
                  onRamPct={setRamPct}
                  error={errors.ramPct}
                />
              )}
              <div className="row">
                <div className="field">
                  <label htmlFor="wz-ctx">Контекст на запрос</label>
                  <input
                    id="wz-ctx"
                    className="input"
                    type="number"
                    min={256}
                    value={ctx}
                    aria-invalid={!!errors.ctx}
                    onChange={(e) => setCtx(e.target.value)}
                  />
                  {errors.ctx ? (
                    <span className="error-text">{errors.ctx}</span>
                  ) : (
                    <span className="hint">Токенов на один запрос, вместе с ответом</span>
                  )}
                </div>
                <div className="field">
                  <label htmlFor="wz-parallel">Параллельность</label>
                  <input
                    id="wz-parallel"
                    className="input"
                    type="number"
                    min={1}
                    max={16}
                    value={parallel}
                    aria-invalid={!!errors.parallel}
                    onChange={(e) => setParallel(e.target.value)}
                  />
                  {errors.parallel ? (
                    <span className="error-text">{errors.parallel}</span>
                  ) : (
                    <span className="hint">Сколько запросов нода обслуживает одновременно</span>
                  )}
                </div>
              </div>
            </div>

            <LlmSettings
              idPrefix="wz"
              value={llm}
              onChange={setLlm}
              errors={llmErrors}
              mainRepo={repo.trim()}
              autoMmproj={!fromId}
              serverError={previewError}
            />

            <div className="card stack">
              <h2>Ноды</h2>
              <label className="check">
                <input type="checkbox" checked={autoAssign} onChange={(e) => setAutoAssign(e.target.checked)} />
                <span>Назначать свободные ноды автоматически</span>
              </label>
              <NodePicker nodes={candidates} selected={selected} onChange={setSelected} />
              {!autoAssign && selected.length === 0 && (
                <p className="hint">Выберите ноды или включите автоназначение, иначе модель не запустится</p>
              )}
            </div>

            <CommandPreview
              requestKey={previewKey}
              request={() => api.commandPreview({ ...bodyFor(name || "model"), nodes: [] })}
              onError={setPreviewError}
            />

            <div className="card stack">
              <div className="field">
                <label htmlFor="wz-name">Имя развёртывания</label>
                <input
                  id="wz-name"
                  className="input mono"
                  value={name}
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={name !== "" && !nameValid}
                  onChange={(e) => {
                    setName(e.target.value);
                    setNameTouched(true);
                  }}
                />
                {name !== "" && !nameValid ? (
                  <span className="error-text">{NAME_HINT}</span>
                ) : (
                  <span className="hint">Так модель называется в запросах к API</span>
                )}
              </div>
              {submitError && <ErrorNotice error={submitError} />}
              <div className="row" style={{ alignItems: "center" }}>
                <button type="submit" className="btn primary" disabled={!canSubmit}>
                  Создать развёртывание
                </button>
                {blocker && <span className="hint">{blocker}</span>}
              </div>
            </div>
          </form>
        )}
        {confirming && (
          <TakeOverConfirm
            taken={taken}
            confirmLabel="Забрать ноды и создать"
            onCancel={() => setConfirming(false)}
            onConfirm={() => void create()}
          />
        )}
      </div>

      <div className="section" style={{ position: "sticky", top: 24 }}>
        {ready ? (
          <EstimatePanel
            state={estimate}
            groupSize={effMode === "split" ? Math.max(2, groupNum || 2) : 1}
            split={effMode === "split"}
            canApplyGroup={effMode === "split"}
            onApplyGroup={(n) => setGroup(String(n))}
          />
        ) : (
          <Notice>Расчёт памяти появится после выбора файла модели</Notice>
        )}
      </div>
    </div>
  );
}
