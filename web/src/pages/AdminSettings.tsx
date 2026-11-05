import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Confirm } from "../components/Modal";
import { ErrorNotice, Loading, PageHead } from "../components/ui";
import { fmtInt } from "../lib/format";
import { fieldText, fmtSetting, groupSettings, parseNumber } from "../lib/settings";
import { useResource } from "../lib/useResource";
import type { KeyLimitsJob, SystemSetting } from "../types";
import "../styles/settings.css";

const RESET_NOTE = "Сброшено к значению из .env";
const KEY_GROUP_KEYS = ["key_rpm_limit", "key_tpm_limit"];

type Draft = string | boolean;

const draftOf = (s: SystemSetting): Draft => (typeof s.value === "boolean" ? s.value : fieldText(s.value));

interface Undo {
  value: number | boolean;
  /** the setting had its own value before; false: it followed the .env */
  overridden: boolean;
}

/** One setting: edit, save, undo the edit, undo the save, reset to the .env. Everything is per field. */
function SettingRow({
  setting,
  onChange,
  onRestart,
  restart,
}: {
  setting: SystemSetting;
  onChange: (s: SystemSetting) => void;
  onRestart: () => void;
  /** the answer of the last mirror restart, shown in the row of the limit (not above the page) */
  restart?: { error: string | null; done: boolean };
}) {
  const [draft, setDraft] = useState<Draft>(draftOf(setting));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [undo, setUndo] = useState<Undo | null>(null);

  // the saved value changed from outside (a restart answer, a reload): the field follows it unless it is being edited
  const [seen, setSeen] = useState(setting.value);
  useEffect(() => {
    if (setting.value !== seen) {
      setSeen(setting.value);
      setDraft(draftOf(setting));
    }
  }, [setting, seen]);

  const isBool = setting.type === "bool";
  const parsed = isBool ? ({ ok: true, value: draft as boolean } as const) : parseNumber(setting, String(draft));
  const dirty = isBool ? draft !== setting.value : String(draft).trim() !== fieldText(setting.value);
  const invalid = dirty && !parsed.ok;
  const inputId = `setting-${setting.key}`;

  async function run(action: () => Promise<SystemSetting>, after: (s: SystemSetting) => void) {
    setBusy(true);
    setError(null);
    try {
      const saved = await action();
      onChange(saved);
      setDraft(draftOf(saved));
      setSeen(saved.value);
      after(saved);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function save() {
    if (!parsed.ok) return;
    const before: Undo = { value: setting.value, overridden: setting.overridden };
    void run(
      () => api.putSetting(setting.key, parsed.value),
      () => {
        setUndo(before);
        setNote("Сохранено");
      },
    );
  }

  function reset() {
    const before: Undo = { value: setting.value, overridden: setting.overridden };
    void run(
      () => api.resetSetting(setting.key),
      () => {
        setUndo(before.overridden ? before : null);
        setNote(RESET_NOTE);
      },
    );
  }

  function undoSave() {
    if (!undo) return;
    const back = undo;
    void run(
      () => (back.overridden ? api.putSetting(setting.key, back.value) : api.resetSetting(setting.key)),
      () => {
        setUndo(null);
        setNote("Возвращено прежнее значение");
      },
    );
  }

  function edit(value: Draft) {
    setDraft(value);
    setNote(null);
    setError(null);
  }

  return (
    <div className="setting" role="group" aria-label={setting.key}>
      <div className="setting-info">
        <span className="desc" id={`${inputId}-desc`}>
          {setting.description}
        </span>
        <span className="mono small muted">{setting.key}</span>
        <div className="setting-meta">
          {setting.overridden ? <span className="tag set">Изменено в панели</span> : <span className="tag">Из .env</span>}
          <span className="small muted">По умолчанию из .env: {fmtSetting(setting.default)}</span>
        </div>
        {setting.restart_required && (
          <div className="setting-meta restart-line">
            {setting.pending_restart ? (
              <span className="tag warn">Применится после перезапуска зеркала</span>
            ) : (
              <span className="small muted">Зеркало читает значение при запуске</span>
            )}
            {setting.pending_restart && (
              <button type="button" className="btn sm" onClick={onRestart}>
                Перезапустить зеркало
              </button>
            )}
          </div>
        )}
        {setting.restart_required && (
          <p className="setting-note restart-note" role="status">
            {restart?.error ? (
              <span className="error-text" role="alert">
                {restart.error}
              </span>
            ) : restart?.done && !setting.pending_restart ? (
              "Зеркало перезапускается: новый предел применится после запуска"
            ) : null}
          </p>
        )}
      </div>

      <div className="setting-control">
        {isBool ? (
          <label className="check">
            <input
              id={inputId}
              type="checkbox"
              aria-labelledby={`${inputId}-desc`}
              checked={draft as boolean}
              disabled={busy}
              onChange={(e) => edit(e.target.checked)}
            />
            {draft ? "Включено" : "Выключено"}
          </label>
        ) : (
          <input
            id={inputId}
            className="input mono"
            type="text"
            inputMode={setting.type === "int" ? "numeric" : "decimal"}
            autoComplete="off"
            value={draft as string}
            disabled={busy}
            aria-labelledby={`${inputId}-desc`}
            aria-invalid={invalid}
            aria-describedby={`${inputId}-note`}
            onChange={(e) => edit(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && dirty && parsed.ok) save();
              if (e.key === "Escape" && dirty) edit(draftOf(setting));
            }}
          />
        )}
        <div className="setting-buttons">
          <button type="button" className="btn sm primary" disabled={busy || !dirty || !parsed.ok} onClick={save}>
            Сохранить
          </button>
          {dirty && (
            <button type="button" className="btn sm" disabled={busy} onClick={() => edit(draftOf(setting))}>
              Отменить
            </button>
          )}
          {!dirty && undo && (
            <button type="button" className="btn sm" disabled={busy} onClick={undoSave}>
              Вернуть прежнее
            </button>
          )}
          {!dirty && setting.overridden && (
            <button type="button" className="btn sm quiet" disabled={busy} onClick={reset}>
              Сбросить к .env
            </button>
          )}
        </div>
        <p className="setting-note" id={`${inputId}-note`} role="status">
          {invalid && !parsed.ok ? (
            <span className="error-text">{parsed.error}</span>
          ) : error ? (
            <span className="error-text" role="alert">
              {error}
            </span>
          ) : (
            note
          )}
        </p>
      </div>
    </div>
  );
}

function describeJob(job: KeyLimitsJob): string {
  const limits =
    job.rpm_limit != null && job.tpm_limit != null
      ? `${fmtInt(job.rpm_limit)} запросов и ${fmtInt(job.tpm_limit)} токенов в минуту`
      : "";
  if (job.state === "running") {
    const total = job.total == null ? "…" : fmtInt(job.total);
    return `Обновлено ${fmtInt(job.done ?? 0)} из ${total}${job.failed ? `, не удалось ${fmtInt(job.failed)}` : ""}`;
  }
  if (job.state === "done")
    return `Готово: обновлено ${fmtInt(job.done ?? 0)}, не удалось ${fmtInt(job.failed ?? 0)}. Лимиты: ${limits}`;
  if (job.state === "failed") return `Остановлено: ${job.error ?? "ошибка"}. Обновлено ${fmtInt(job.done ?? 0)}`;
  return "";
}

/** «Применить к существующим ключам»: a background job on the portal, shown while it runs */
function KeyLimitsApply() {
  const [job, setJob] = useState<KeyLimitsJob>({ state: "none" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .keyLimitsJob()
      .then((j) => alive && setJob(j))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (job.state !== "running") return;
    const timer = setInterval(() => {
      api
        .keyLimitsJob()
        .then((j) => setJob(j))
        .catch(() => undefined);
    }, 1500);
    return () => clearInterval(timer);
  }, [job.state]);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      setJob(await api.applyKeyLimits());
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const running = job.state === "running";
  const text = describeJob(job);
  return (
    <div className="key-apply">
      <p>
        Новые ключи получают лимиты сразу. Кнопка ниже проставляет сохранённые лимиты уже выданным ключам; личные лимиты
        пользователей остаются как есть.
      </p>
      <button type="button" className="btn" disabled={busy || running} onClick={() => void start()}>
        Применить к существующим ключам
      </button>
      <p className="setting-note key-apply-status" role="status">
        {error ? (
          <span className="error-text" role="alert">
            {error}
          </span>
        ) : (
          text
        )}
      </p>
    </div>
  );
}

export function AdminSettings() {
  const res = useResource(() => api.settings(), []);
  const [items, setItems] = useState<SystemSetting[] | null>(null);
  const [asking, setAsking] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [restartError, setRestartError] = useState<string | null>(null);
  const [restarted, setRestarted] = useState(false);

  useEffect(() => {
    if (res.data) setItems(res.data);
  }, [res.data]);

  const replace = (s: SystemSetting) => setItems((cur) => cur && cur.map((x) => (x.key === s.key ? s : x)));

  async function restart() {
    setRestarting(true);
    setRestartError(null);
    try {
      replace(await api.restartMirror());
      setRestarted(true);
      setAsking(false);
    } catch (e) {
      setRestartError(e instanceof ApiError ? e.detail : (e as Error).message);
      setAsking(false);
    } finally {
      setRestarting(false);
    }
  }

  return (
    <div className="page settings-page">
      <PageHead
        title="Настройки"
        lead="Значения применяются без перезапуска в течение нескольких секунд, кроме отмеченных. Сброс возвращает значение из .env."
      />
      <ErrorNotice error={res.error} />
      {res.loading && !items && <Loading />}
      {items &&
        groupSettings(items).map(({ group, items: rows }, i) => (
          <section key={group} className="card stack settings-group" aria-labelledby={`settings-group-${i}`}>
            <h2 id={`settings-group-${i}`}>{group}</h2>
            {rows.map((s) => (
              <SettingRow
                key={s.key}
                setting={s}
                onChange={replace}
                onRestart={() => setAsking(true)}
                restart={{ error: restartError, done: restarted }}
              />
            ))}
            {rows.some((s) => KEY_GROUP_KEYS.includes(s.key)) && <KeyLimitsApply />}
          </section>
        ))}
      {asking && (
        <Confirm
          title="Перезапуск зеркала"
          confirmLabel="Перезапустить"
          busy={restarting}
          onCancel={() => setAsking(false)}
          onConfirm={() => void restart()}
        >
          <p>Перезапустить зеркало Hugging Face, чтобы оно взяло новый предел кэша?</p>
          <p className="secondary">Скачивания моделей через зеркало прервутся на время перезапуска.</p>
        </Confirm>
      )}
    </div>
  );
}
