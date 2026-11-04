import { FolderOpen, FolderSearch, Pause, Play, Trash2, Upload, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError } from "../api";
import { Confirm } from "../components/Modal";
import { ErrorNotice, Ic, Loading, Notice, PageHead } from "../components/ui";
import { fmtBytes, fmtInt } from "../lib/format";
import { ChunkedUpload, MODEL_NAME_RE, suggestModelName, type UploadState } from "../lib/upload";
import { useResource } from "../lib/useResource";
import type { LocalModel, LocalModelFile, ModelUpload } from "../types";

const pct = (part: number, whole: number) => (whole > 0 ? Math.floor((part / whole) * 100) : 0);
const SHA_RE = /^[0-9a-fA-F]{64}$/;

const FILE_STATUS: Record<LocalModelFile["status"], string> = {
  ready: "Готов",
  incomplete: "Не хватает частей",
  invalid: "Ошибка",
};

const ORIGIN: Record<LocalModelFile["origin"], string> = { upload: "загружен в портале", folder: "из каталога" };

function Progress({ value, label }: { value: number; label: string }) {
  return (
    <span className="meter wide" role="progressbar" aria-label={label} aria-valuenow={value} aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${value}%` }} />
    </span>
  );
}

interface Running {
  key: string;
  job: ChunkedUpload;
  state: UploadState;
}

/** what «Отменить» would throw away, asked before the upload is cancelled */
interface Cancelling {
  file: string;
  sent: number;
  size: number;
  run: () => Promise<void>;
}

export function AdminLocalModels() {
  const catalog = useResource(() => api.localModels(), [], { interval: 10000 });
  const [running, setRunning] = useState<Running[]>([]);
  const runningRef = useRef(running);
  runningRef.current = running;
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [deleting, setDeleting] = useState<{ model: LocalModel; file: LocalModelFile | null } | null>(null);
  const [cancelling, setCancelling] = useState<Cancelling | null>(null);
  const [busy, setBusy] = useState(false);
  /** the uploads of this page in the order they were chosen: the queue sends them one at a time */
  const jobs = useRef(new Map<string, ChunkedUpload>());
  const pumping = useRef(false);
  const alive = useRef(true);

  // a closed page stops sending: the uploads stay on the server and continue from the same byte later
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      jobs.current.forEach((job) => job.pause());
    };
  }, []);

  function track(key: string, file: File, upload: ModelUpload) {
    const job = new ChunkedUpload(file, upload, (state) => {
      if (!alive.current) return;
      setRunning((list) => list.map((r) => (r.key === key ? { ...r, state } : r)));
      if (state.phase === "verifying" || state.phase === "failed" || state.phase === "cancelled") void catalog.reload();
    });
    jobs.current.set(key, job);
    setRunning((list) => [...list.filter((r) => r.key !== key), { key, job, state: job.current }]);
    return job;
  }

  /** Sends the queued uploads one after another. A file that pauses, fails or is cancelled stops the queue: the next
   * ones wait for the admin («Начать» or «Продолжить» of the stopped one), they never start on their own. */
  async function pump() {
    if (pumping.current) return;
    pumping.current = true;
    try {
      while (alive.current) {
        const next = [...jobs.current.values()].find((j) => j.current.phase === "queued");
        if (!next) break;
        await next.start();
        if (next.current.phase !== "verifying") break;
      }
    } finally {
      pumping.current = false;
    }
  }

  /** «Продолжить» or «Начать» of one upload; once it is through, the queue goes on. */
  async function go(job: ChunkedUpload) {
    if (job.current.phase === "queued") await job.start();
    else await job.resume();
    if (job.current.phase === "verifying") await pump();
  }

  /** Creates (or, for the same file and size, resumes) the upload of every file, then sends them one after another. */
  async function startUploads(files: File[], model: string, sha256: string | null) {
    setError(null);
    for (const file of files) {
      try {
        const up = await api.createUpload({ model, file: file.name, size_bytes: file.size, sha256 });
        track(up.id, file, up);
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : "Не удалось начать загрузку");
        break;
      }
    }
    await catalog.reload();
    await pump();
  }

  /** Cancels at once when nothing reached the server, otherwise asks first, naming what would be lost. */
  function askCancel(file: string, sent: number, size: number, run: () => Promise<void>) {
    if (sent <= 0)
      void run().catch((e) => setError(e instanceof ApiError ? e.detail : "Не удалось отменить загрузку"));
    else setCancelling({ file, sent, size, run });
  }

  async function confirmCancel() {
    if (!cancelling) return;
    setBusy(true);
    setError(null);
    try {
      await cancelling.run();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Не удалось отменить загрузку");
    } finally {
      setBusy(false);
      setCancelling(null);
      await catalog.reload();
    }
  }

  async function scan() {
    setScanning(true);
    setError(null);
    try {
      await api.scanLocalModels();
      await catalog.reload();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Не удалось просмотреть каталог");
    } finally {
      setScanning(false);
    }
  }

  async function remove() {
    if (!deleting) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteLocalModel(deleting.model.name, deleting.file?.file);
      setDeleting(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Не удалось удалить");
      setDeleting(null);
    } finally {
      setBusy(false);
      await catalog.reload();
    }
  }

  async function dismiss(up: ModelUpload) {
    setError(null);
    try {
      await api.cancelUpload(up.id);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Не удалось отменить загрузку");
    }
    await catalog.reload();
  }

  const data = catalog.data;
  const here = new Set(running.map((r) => r.key));
  // an upload this page handed over for verification is shown from the server's state: a failed check stays visible
  const handed = new Set(running.filter((r) => r.state.phase === "verifying").map((r) => r.key));
  const elsewhere = (data?.uploads ?? []).filter((u) => !here.has(u.id) || handed.has(u.id) || u.status === "verifying");
  const shown = running.filter((r) => r.state.phase !== "verifying" && r.state.phase !== "cancelled");

  function cancelRun(r: Running) {
    const { job, state } = r;
    askCancel(state.upload.file, state.sent, state.upload.size_bytes, async () => {
      await job.cancel();
      void pump(); // the queue goes on with the next file
    });
  }

  return (
    <div className="page">
      <PageHead
        title="Мои модели"
        lead="Свои файлы GGUF на главном сервере, помимо Hugging Face. Ноды получают их через зеркало главного, развёртывание выбирает их на вкладке «Свои файлы»."
        actions={
          <button type="button" className="btn" onClick={() => void scan()} disabled={scanning}>
            <Ic icon={FolderSearch} />
            Найти файлы в каталоге
          </button>
        }
      />
      {error && <ErrorNotice error={error} />}
      <ErrorNotice error={catalog.error && !data ? catalog.error : null} />
      {catalog.loading && !data && <Loading />}

      {data && (
        <>
          <UploadForm storage={data.storage} onStart={startUploads} />

          {(shown.length > 0 || elsewhere.length > 0) && (
            <section className="card stack" aria-label="Загрузки">
              <h2>Загрузки</h2>
              <ul className="stack" style={{ listStyle: "none", margin: 0, padding: 0 }}>
                {shown.map((r) => (
                  <RunningRow key={r.key} run={r} onGo={() => void go(r.job)} onCancel={() => cancelRun(r)} />
                ))}
                {elsewhere.map((u) => (
                  <PendingRow
                    key={u.id}
                    upload={u}
                    onResume={(file) => void startUploads([file], u.model, null)}
                    onDismiss={() => void dismiss(u)}
                    onCancel={() => askCancel(u.file, u.received_bytes, u.size_bytes, () => api.cancelUpload(u.id))}
                  />
                ))}
              </ul>
            </section>
          )}

          {data.models.length === 0 ? (
            <Notice>
              Своих моделей пока нет. Загрузите файл выше или положите его в каталог{" "}
              <span className="mono">{data.storage.folder}</span> на главном сервере и нажмите «Найти файлы в каталоге».
            </Notice>
          ) : (
            <div className="table-wrap">
              <table className="data" aria-label="Мои модели">
                <thead>
                  <tr>
                    <th>Модель и файл</th>
                    <th>Квант</th>
                    <th className="num">Размер</th>
                    <th>Архитектура</th>
                    <th className="num">Контекст модели</th>
                    <th className="num">Память</th>
                    <th>Состояние</th>
                    <th>Развёртывания</th>
                    <th aria-label="Действия" />
                  </tr>
                </thead>
                <tbody>
                  {data.models.flatMap((m) => [
                    <tr key={m.name} className="group-row">
                      <td colSpan={8}>
                        <span className="mono">{m.repo}</span>{" "}
                        <span className="muted small">{fmtBytes(m.size_bytes)}</span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn quiet sm"
                          aria-label={`Удалить модель ${m.name}`}
                          onClick={() => setDeleting({ model: m, file: null })}
                        >
                          <Ic icon={Trash2} />
                        </button>
                      </td>
                    </tr>,
                    ...m.files.map((f) => <FileRow key={`${m.name}/${f.file}`} model={m} file={f} onDelete={() => setDeleting({ model: m, file: f })} />),
                  ])}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {cancelling && (
        <Confirm
          title="Отменить загрузку"
          confirmLabel="Отменить загрузку"
          busy={busy}
          onCancel={() => setCancelling(null)}
          onConfirm={() => void confirmCancel()}
        >
          <p>
            Отменить загрузку {cancelling.file}? На главный уже передано {fmtBytes(cancelling.sent)} из{" "}
            {fmtBytes(cancelling.size)}. Принятая часть удаляется, продолжить потом будет нельзя, только загрузить заново.
          </p>
        </Confirm>
      )}

      {deleting && (
        <Confirm
          title={deleting.file ? "Удаление файла" : "Удаление модели"}
          confirmLabel="Удалить"
          busy={busy}
          onCancel={() => setDeleting(null)}
          onConfirm={() => void remove()}
        >
          <p>
            {deleting.file
              ? `Удалить файл ${deleting.file.file}${deleting.file.shards.length > 1 ? ` и все его части (${deleting.file.shards.length})` : ""} из модели ${deleting.model.name}?`
              : `Удалить модель ${deleting.model.name} со всеми файлами?`}{" "}
            Файлы удаляются с диска главного, это действие нельзя отменить.
          </p>
        </Confirm>
      )}
    </div>
  );
}

function UploadForm({
  storage,
  onStart,
}: {
  storage: { free_bytes: number; used_bytes: number; limit_bytes: number | null; folder: string };
  onStart: (files: File[], model: string, sha256: string | null) => Promise<void>;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [model, setModel] = useState("");
  const [modelTouched, setModelTouched] = useState(false);
  const [sha, setSha] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const total = files.reduce((s, f) => s + f.size, 0);
  const nameOk = MODEL_NAME_RE.test(model) && !model.includes("..");
  const shaOk = sha.trim() === "" || SHA_RE.test(sha.trim());
  const notGguf = files.filter((f) => !/\.gguf$/i.test(f.name));
  const canStart = files.length > 0 && nameOk && shaOk && notGguf.length === 0;

  function pick(list: FileList | null) {
    const picked = Array.from(list ?? []).sort((a, b) => a.name.localeCompare(b.name));
    setFiles(picked);
    if (!modelTouched && picked.length) setModel(suggestModelName(picked[0].name));
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!canStart) return;
    const chosen = files;
    setFiles([]);
    setSha("");
    setModelTouched(false);
    if (inputRef.current) inputRef.current.value = "";
    void onStart(chosen, model, chosen.length === 1 && sha.trim() ? sha.trim().toLowerCase() : null);
  }

  return (
    <form className="card stack" onSubmit={submit} aria-label="Загрузка файла">
      <h2>Загрузка файла</h2>
      <p className="hint">
        Файлы любого размера загружаются частями: загрузку можно поставить на паузу и продолжить, а после обрыва связи
        или закрытия страницы она продолжится с того же места.
      </p>
      <div className="row">
        <div className="field grow">
          <label htmlFor="lm-files">Файлы GGUF</label>
          <div className="file-pick">
            <input
              id="lm-files"
              ref={inputRef}
              className="visually-hidden"
              type="file"
              accept=".gguf"
              multiple
              onChange={(e) => pick(e.target.files)}
            />
            <label htmlFor="lm-files" className="btn sm" aria-hidden="true">
              <Ic icon={FolderOpen} />
              Выбрать файлы
            </label>
            <span className={`file-pick-name${files.length ? "" : " muted"}`}>
              {files.length === 0
                ? "Файлы не выбраны"
                : files.length === 1
                  ? files[0].name
                  : `${files[0].name} и ещё ${files.length - 1}`}
            </span>
          </div>
          {notGguf.length > 0 ? (
            <span className="error-text">Нужны файлы с расширением .gguf</span>
          ) : (
            <span className="hint">Многофайловую модель выберите целиком, все части сразу</span>
          )}
        </div>
        <div className="field">
          <label htmlFor="lm-model">Модель</label>
          <input
            id="lm-model"
            className="input mono"
            value={model}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={model !== "" && !nameOk}
            onChange={(e) => {
              setModel(e.target.value);
              setModelTouched(true);
            }}
          />
          {model !== "" && !nameOk ? (
            <span className="error-text">Латинские буквы, цифры, точка, дефис и подчёркивание, до 96 символов</span>
          ) : (
            <span className="hint">Развёртывание получит её как local/{model || "имя"}</span>
          )}
        </div>
      </div>
      {files.length <= 1 && (
        <div className="field">
          <label htmlFor="lm-sha">sha256 файла, необязательно</label>
          <input
            id="lm-sha"
            className="input mono"
            value={sha}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={!shaOk}
            onChange={(e) => setSha(e.target.value)}
          />
          {shaOk ? (
            <span className="hint">Сумму портал считает сам, а если указать ожидаемую, сверит её после загрузки</span>
          ) : (
            <span className="error-text">64 шестнадцатеричных символа</span>
          )}
        </div>
      )}
      <div className="row" style={{ alignItems: "center" }}>
        <button type="submit" className="btn primary" disabled={!canStart}>
          <Ic icon={Upload} />
          Загрузить
        </button>
        {files.length > 0 && (
          <span className="hint">
            {files.length > 1 ? `${files.length} файла, ` : ""}
            {fmtBytes(total)}
          </span>
        )}
      </div>
      <p className="hint">
        Свободно на томе зеркала: {fmtBytes(storage.free_bytes)}, свои модели занимают {fmtBytes(storage.used_bytes)}
        {storage.limit_bytes !== null ? ` из ${fmtBytes(storage.limit_bytes)}` : ""}. Файлы можно и положить в каталог{" "}
        <span className="mono">{storage.folder}</span> на главном (scp, rsync): портал найдёт их сам в течение минуты.
      </p>
    </form>
  );
}

function RunningRow({ run, onGo, onCancel }: { run: Running; onGo: () => void; onCancel: () => void }) {
  const { state, job } = run;
  const { upload } = state;
  const value = pct(state.sent, upload.size_bytes);
  const label = `Загрузка ${upload.file}`;
  return (
    <li className="stack" aria-label={label}>
      <div className="row" style={{ alignItems: "center" }}>
        <span className="mono small grow">
          local/{upload.model}/{upload.file}
        </span>
        <span className="small">
          {state.phase === "queued"
            ? `Ждёт очереди, ${fmtBytes(upload.size_bytes)}`
            : `${state.phase === "paused" ? "Пауза, " : state.phase === "failed" ? "Ошибка, " : ""}${value} %, ${fmtBytes(state.sent)} из ${fmtBytes(upload.size_bytes)}`}
        </span>
        {state.phase === "queued" && (
          <button type="button" className="btn sm" onClick={onGo}>
            <Ic icon={Play} />
            Начать
          </button>
        )}
        {state.phase === "uploading" && (
          <button type="button" className="btn sm" onClick={() => job.pause()}>
            <Ic icon={Pause} />
            Пауза
          </button>
        )}
        {(state.phase === "paused" || job.resumable) && (
          <button type="button" className="btn sm" onClick={onGo}>
            <Ic icon={Play} />
            Продолжить
          </button>
        )}
        {state.phase !== "verifying" && state.phase !== "cancelled" && (
          <button type="button" className="btn quiet sm" onClick={onCancel}>
            <Ic icon={X} />
            Отменить
          </button>
        )}
      </div>
      <Progress value={value} label={label} />
      {state.error && <ErrorNotice error={state.error} />}
    </li>
  );
}

function PendingRow({
  upload,
  onResume,
  onDismiss,
  onCancel,
}: {
  upload: ModelUpload;
  onResume: (file: File) => void;
  onDismiss: () => void;
  onCancel: () => void;
}) {
  const [mismatch, setMismatch] = useState(false);
  const label = `Загрузка ${upload.file}`;
  if (upload.status === "verifying") {
    const value = pct(upload.verified_bytes, upload.size_bytes);
    return (
      <li className="stack" aria-label={label}>
        <div className="row">
          <span className="mono small grow">
            local/{upload.model}/{upload.file}
          </span>
          <span className="small">Проверка sha256 и заголовка GGUF, {value} %</span>
        </div>
        <Progress value={value} label={label} />
      </li>
    );
  }
  if (upload.status === "failed") {
    return (
      <li className="stack" aria-label={label}>
        <div className="row" style={{ alignItems: "center" }}>
          <span className="mono small grow">
            local/{upload.model}/{upload.file}
          </span>
          <button type="button" className="btn quiet sm" onClick={onDismiss}>
            Убрать из списка
          </button>
        </div>
        <ErrorNotice error={upload.error ?? "Загрузка завершилась ошибкой"} />
      </li>
    );
  }
  const value = pct(upload.received_bytes, upload.size_bytes);
  return (
    <li className="stack" aria-label={label}>
      <div className="row" style={{ alignItems: "center" }}>
        <span className="mono small grow">
          local/{upload.model}/{upload.file}
        </span>
        <span className="small">
          Прервана на {value} %, {fmtBytes(upload.received_bytes)} из {fmtBytes(upload.size_bytes)}
        </span>
        <label className="btn sm">
          <Ic icon={Play} />
          Продолжить
          <input
            type="file"
            accept=".gguf"
            aria-label={`Выбрать файл ${upload.file}, чтобы продолжить`}
            className="visually-hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (!file) return;
              if (file.name !== upload.file || file.size !== upload.size_bytes) setMismatch(true);
              else {
                setMismatch(false);
                onResume(file);
              }
            }}
          />
        </label>
        <button type="button" className="btn quiet sm" onClick={onCancel}>
          <Ic icon={X} />
          Отменить
        </button>
      </div>
      <Progress value={value} label={label} />
      {mismatch ? (
        <span className="error-text">
          Это другой файл: нужен {upload.file} размером {fmtInt(upload.size_bytes)} байт
        </span>
      ) : (
        <span className="hint">Чтобы продолжить, выберите тот же файл на этом компьютере</span>
      )}
    </li>
  );
}

function FileRow({ model, file, onDelete }: { model: LocalModel; file: LocalModelFile; onDelete: () => void }) {
  const meta = file.meta;
  return (
    <tr aria-label={`${model.name}/${file.file}`}>
      <td>
        <div className="stackcell">
          <span className="mono small">{file.file}</span>
          <span className="muted small">
            {file.shards.length > 1 ? `${file.shards.length} части, ` : ""}
            {ORIGIN[file.origin]}
            {file.sha256 ? `, sha256 ${file.sha256.slice(0, 12)}…` : ""}
          </span>
        </div>
      </td>
      <td>{file.quant ? <span className="tag">{file.quant}</span> : <span className="muted">—</span>}</td>
      <td className="num mono">{fmtBytes(file.size_bytes)}</td>
      <td className="mono small">{meta?.arch ?? "—"}</td>
      <td className="num">{meta ? fmtInt(meta.max_ctx) : "—"}</td>
      <td className="num">
        {file.estimate ? (
          <span title={`контекст ${fmtInt(file.estimate.ctx_per_request)} × ${file.estimate.parallel}`}>
            {fmtBytes(file.estimate.total_bytes)}
          </span>
        ) : (
          "—"
        )}
      </td>
      <td>
        {file.status === "ready" ? FILE_STATUS.ready : <span className="error-text">{file.error ?? FILE_STATUS[file.status]}</span>}
      </td>
      <td className="mono small">{file.used_by.length ? file.used_by.join(", ") : <span className="muted">—</span>}</td>
      <td>
        <button type="button" className="btn quiet sm" aria-label={`Удалить файл ${file.file}`} onClick={onDelete}>
          <Ic icon={Trash2} />
        </button>
      </td>
    </tr>
  );
}
