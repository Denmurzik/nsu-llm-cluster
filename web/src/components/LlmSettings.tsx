import { useEffect, useRef } from "react";
import { api } from "../api";
import {
  KV_HINT,
  MAX_SYSTEM_PROMPT,
  REASONING_LABEL,
  SAMPLING_FIELDS,
  pickMmproj,
  type LlmErrors,
  type LlmSettings as Settings,
} from "../lib/llm";
import { fmtBytes } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { KvCacheType, Reasoning } from "../types";
import { ModelFilePicker } from "./ModelFilePicker";

/** The section «Генерация и модель» of the deployment forms (wizard and edit page). */
export function LlmSettings({
  idPrefix,
  value,
  onChange,
  errors,
  mainRepo,
  autoMmproj = false,
  serverError,
}: {
  idPrefix: string;
  value: Settings;
  onChange: (next: Settings) => void;
  errors: LlmErrors;
  /** repository of the main model: the projector is one of its files */
  mainRepo: string;
  /** the wizard turns images on by itself when the repository has a projector (until the admin decides) */
  autoMmproj?: boolean;
  /** the portal's 422 text for the settings (shown by the own flags when it is about them) */
  serverError?: string | null;
}) {
  const p = idPrefix;
  const set = (patch: Partial<Settings>) => onChange({ ...value, ...patch });

  const projectors = useResource(() => api.hfMmproj(mainRepo), [mainRepo], {});
  const files = projectors.data ?? [];
  // an automatic choice is made once per repository, and only while the admin has not touched the option
  const decided = useRef<string | null>(null);
  useEffect(() => {
    if (!autoMmproj || !projectors.data || decided.current === mainRepo) return;
    decided.current = mainRepo;
    const best = pickMmproj(projectors.data);
    onChange({ ...value, mmprojOn: best !== null, mmprojFile: best });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectors.data, mainRepo, autoMmproj]);

  const flagsError = errors.extraArgs ?? (serverError && /флаг|кавыч|Пути к файлам|ASCII/i.test(serverError) ? serverError : null);

  return (
    <div className="card stack">
      <h2>Генерация и модель</h2>

      <div className="row">
        <div className="field">
          <label htmlFor={`${p}-reasoning`}>Рассуждение</label>
          <select
            id={`${p}-reasoning`}
            className="select"
            value={value.reasoning}
            onChange={(e) => set({ reasoning: e.target.value as Reasoning })}
          >
            {(Object.keys(REASONING_LABEL) as Reasoning[]).map((r) => (
              <option key={r} value={r}>
                {REASONING_LABEL[r]}
              </option>
            ))}
          </select>
          <span className="hint">Модель сначала думает вслух: ответ точнее, но дольше</span>
        </div>
        <div className="field">
          <label htmlFor={`${p}-kv`}>KV-кэш</label>
          <select id={`${p}-kv`} className="select" value={value.kv} onChange={(e) => set({ kv: e.target.value as KvCacheType })}>
            {(Object.keys(KV_HINT) as KvCacheType[]).map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
          <span className="hint">q8_0 вдвое уменьшает память под контекст почти без потери качества</span>
        </div>
      </div>

      <fieldset>
        <legend>Параметры генерации по умолчанию</legend>
        <div className="row">
          {SAMPLING_FIELDS.map((f) => (
            <div className="field" key={f.key}>
              <label htmlFor={`${p}-s-${f.key}`}>{f.label}</label>
              <input
                id={`${p}-s-${f.key}`}
                className="input"
                type="number"
                inputMode="decimal"
                min={f.min}
                max={f.max}
                step={f.step}
                placeholder="из модели"
                value={value.sampling[f.key]}
                aria-invalid={!!errors.sampling?.[f.key]}
                onChange={(e) => set({ sampling: { ...value.sampling, [f.key]: e.target.value } })}
              />
              {errors.sampling?.[f.key] ? (
                <span className="error-text">{errors.sampling[f.key]}</span>
              ) : (
                <span className="hint">
                  от {f.min} до {f.max}
                </span>
              )}
            </div>
          ))}
        </div>
        <p className="hint" style={{ marginTop: 4 }}>
          Значения по умолчанию: клиент может передать свои. Пустое поле оставляет настройку самой модели
        </p>
      </fieldset>

      <div className="section">
        <label className="check">
          <input
            type="checkbox"
            checked={value.draftOn}
            onChange={(e) => set({ draftOn: e.target.checked, ...(e.target.checked ? {} : { draftRepo: "", draftFile: null }) })}
          />
          <span>Draft-модель</span>
        </label>
        <span className="hint">
          Малая модель предлагает несколько токенов вперёд, основная проверяет их: ответы быстрее. Draft и её контекст
          занимают память на каждой ноде (при разделении только на лидере)
        </span>
        {value.draftOn && (
          <ModelFilePicker
            idPrefix={`${p}-draft`}
            legend="Файл draft-модели"
            repo={value.draftRepo}
            file={value.draftFile}
            onChange={(repo, file) => set({ draftRepo: repo, draftFile: file })}
          />
        )}
        {errors.draft && <span className="error-text">{errors.draft}</span>}
      </div>

      <div className="section">
        <label className="check">
          <input
            type="checkbox"
            checked={value.mmprojOn}
            onChange={(e) => {
              decided.current = mainRepo; // the admin decides from now on
              set({ mmprojOn: e.target.checked, mmprojFile: e.target.checked ? (value.mmprojFile ?? pickMmproj(files)) : null });
            }}
          />
          <span>Изображения (mmproj)</span>
        </label>
        <span className="hint">Модель понимает картинки в запросах; проектор занимает память рядом с моделью</span>
        {value.mmprojOn && (
          <div className="field fixed" style={{ maxWidth: 360 }}>
            <label htmlFor={`${p}-mmproj`}>Файл проектора</label>
            <select
              id={`${p}-mmproj`}
              className="select"
              value={value.mmprojFile ?? ""}
              onChange={(e) => set({ mmprojFile: e.target.value || null })}
            >
              {!value.mmprojFile && <option value="">выберите файл</option>}
              {value.mmprojFile && !files.some((f) => f.file === value.mmprojFile) && (
                <option value={value.mmprojFile}>{value.mmprojFile}</option>
              )}
              {files.map((f) => (
                <option key={f.file} value={f.file}>
                  {f.file}, {fmtBytes(f.size_bytes)}
                </option>
              ))}
            </select>
          </div>
        )}
        {value.mmprojOn && !projectors.loading && files.length === 0 && !value.mmprojFile && (
          <span className="hint">В репозитории модели нет файлов mmproj</span>
        )}
        {errors.mmproj && <span className="error-text">{errors.mmproj}</span>}
      </div>

      <div className="field">
        <label htmlFor={`${p}-system`}>Системный промпт</label>
        <textarea
          id={`${p}-system`}
          className="textarea"
          rows={4}
          value={value.systemPrompt}
          aria-invalid={!!errors.systemPrompt}
          onChange={(e) => set({ systemPrompt: e.target.value })}
        />
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap", alignItems: "flex-start" }}>
          {errors.systemPrompt ? (
            <span className="error-text">{errors.systemPrompt}</span>
          ) : (
            <span className="hint">Добавляется, только если в запросе нет своего. Виден пользователям с ключом через /v1/model/info</span>
          )}
          <span className="hint mono nowrap" style={{ flex: "none" }} aria-label="Длина промпта">
            {value.systemPrompt.length} / {MAX_SYSTEM_PROMPT}
          </span>
        </div>
      </div>

      <label className="check">
        <input type="checkbox" checked={value.experimental} onChange={(e) => set({ experimental: e.target.checked })} />
        <span>Экспериментальная (видна только администраторам)</span>
      </label>

      <div className="field">
        <label htmlFor={`${p}-extra`}>Свои флаги llama-server</label>
        <input
          id={`${p}-extra`}
          className="input mono"
          value={value.extraArgs}
          placeholder="--no-warmup --seed 42"
          autoComplete="off"
          spellCheck={false}
          aria-invalid={!!flagsError}
          onChange={(e) => set({ extraArgs: e.target.value })}
        />
        {flagsError ? (
          <span className="error-text">{flagsError}</span>
        ) : (
          <span className="hint">Добавляются в конец команды. Порт, пути, модель, контекст и сэмплинг задаются выше</span>
        )}
      </div>
    </div>
  );
}
