import { Download, Search, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Confirm } from "../components/Modal";
import { ErrorNotice, Ic, Loading, Notice, PageHead } from "../components/ui";
import { fmtBytes, fmtDateTime, fmtPercent } from "../lib/format";
import { plural } from "../lib/labels";
import { useResource } from "../lib/useResource";
import type { CacheFile, HfFile, MmprojFile, ModelCache, PrefetchJob } from "../types";

/** seconds since the epoch (the mirror's clock) as «02.11.2026, 14:05» */
const fmtEpoch = (sec: number | null) => (sec === null ? "—" : fmtDateTime(new Date(sec * 1000).toISOString()));

const pct = (part: number, whole: number) => (whole > 0 ? Math.min(100, Math.floor((part / whole) * 100)) : 0);

function fmtEta(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 60) return "меньше минуты";
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  return `${hours} ч ${minutes % 60} мин`;
}

function Meter({ value, label }: { value: number; label: string }) {
  return (
    <span className="meter wide" role="progressbar" aria-label={label} aria-valuenow={value} aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${value}%` }} />
    </span>
  );
}

/** Why a file cannot be deleted now; null: it can. Deployments come first: their weights are what the nodes fetch. */
function blocker(f: CacheFile): string | null {
  if (f.used_by.length)
    return `Используется ${plural(f.used_by.length, ["развёртыванием", "развёртываниями", "развёртываниями"])} ${f.used_by.map((u) => u.name).join(", ")}`;
  if (f.downloading) return "Скачивается сейчас";
  if (f.busy) return "Отдаётся нодам сейчас";
  return null;
}

function stateOf(f: CacheFile): string {
  if (f.downloading) return f.total ? `Скачивается, ${pct(f.written ?? f.size, f.total)} %` : "Скачивается";
  if (f.partial) return "Недокачан";
  return "В кэше";
}

/** what the mirror can still take: the rest of its limit, and the free disk without the reserve it keeps */
function roomBytes(c: ModelCache): number {
  return Math.max(0, Math.min(c.limit_bytes - c.used_bytes, c.disk_free_bytes - c.disk_reserve_bytes));
}

interface Deleting {
  repo: string;
  path: string | null;
  files: CacheFile[];
}

export function AdminModelCache() {
  const [fast, setFast] = useState(false);
  const data = useResource(
    async () => {
      const [cache, jobs] = await Promise.all([api.modelCache(), api.prefetchJobs()]);
      return { cache, jobs: jobs.jobs, concurrency: jobs.concurrency };
    },
    [fast],
    { interval: fast ? 2000 : 10000 },
  );
  const active = data.data?.jobs.some((j) => j.state === "queued" || j.state === "running") ?? false;
  // a download in progress is watched closely, an idle page only now and then
  useEffect(() => setFast(active), [active]);

  const [deleting, setDeleting] = useState<Deleting | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function remove() {
    if (!deleting) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.deleteCached(deleting.repo, deleting.path ?? undefined);
      setMessage(`Из кэша удалено: ${fmtBytes(r.freed_bytes)}`);
    } catch (e) {
      setMessage(null);
      setError(e instanceof ApiError ? e.detail : "Не удалось удалить");
    } finally {
      setBusy(false);
      setDeleting(null);
      await data.reload();
    }
  }

  const cache = data.data?.cache;
  const jobs = data.data?.jobs ?? [];
  const byRepo = new Map<string, CacheFile[]>();
  for (const f of cache?.files ?? []) byRepo.set(f.repo, [...(byRepo.get(f.repo) ?? []), f]);

  return (
    <div className="page">
      <PageHead
        title="Кэш моделей"
        lead="Файлы, которые зеркало главного скачало с Hugging Face. Ноды берут их отсюда по сети, интернет нужен один раз. Когда кэш заполнен, зеркало само удаляет давно не использованные файлы."
      />
      {error && <ErrorNotice error={error} />}
      <ErrorNotice error={data.error} />
      {message && (
        <p className="secondary" role="status">
          {message}
        </p>
      )}
      {data.loading && !data.data && <Loading />}

      {cache && (
        <>
          <UsageCard cache={cache} />
          <PrefetchForm cache={cache} concurrency={data.data?.concurrency ?? 2} onQueued={() => data.reload()} />
          {jobs.length > 0 && <JobsCard jobs={jobs} onChanged={() => data.reload()} />}

          {byRepo.size === 0 ? (
            <Notice>
              В кэше пока нет файлов. Они появятся, когда нода впервые запросит модель, или после «Скачать заранее».
            </Notice>
          ) : (
            <div className="table-wrap">
              <table className="data" aria-label="Файлы в кэше">
                <thead>
                  <tr>
                    <th>Файл</th>
                    <th className="num">Размер</th>
                    <th>Коммит</th>
                    <th>Последнее использование</th>
                    <th>Состояние</th>
                    <th aria-label="Действия" />
                  </tr>
                </thead>
                <tbody>
                  {[...byRepo].flatMap(([repo, files]) => {
                    const blocked = [...new Set(files.map(blocker).filter((b): b is string => b !== null))];
                    const users = [...new Set(files.flatMap((f) => f.used_by.map((u) => u.name)))];
                    return [
                      <tr key={repo} className="group-row">
                        <td colSpan={4}>
                          <span className="mono">{repo}</span>{" "}
                          <span className="muted small">{fmtBytes(files.reduce((s, f) => s + f.size, 0))}</span>
                        </td>
                        <td className="small secondary">
                          {users.length > 0
                            ? `Есть файлы ${plural(users.length, ["развёртывания", "развёртываний", "развёртываний"])} ${users.join(", ")}`
                            : blocked[0]}
                        </td>
                        <td>
                          <button
                            type="button"
                            className="btn quiet sm"
                            aria-label={`Удалить репозиторий ${repo} из кэша`}
                            disabled={blocked.length > 0}
                            onClick={() => setDeleting({ repo, path: null, files })}
                          >
                            <Ic icon={Trash2} />
                          </button>
                        </td>
                      </tr>,
                      ...files.map((f) => (
                        <FileRow key={`${f.repo}/${f.path}`} file={f} onDelete={() => setDeleting({ repo, path: f.path, files: [f] })} />
                      )),
                    ];
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {deleting && (
        <Confirm
          title={deleting.path ? "Удаление файла из кэша" : "Удаление репозитория из кэша"}
          confirmLabel="Удалить"
          busy={busy}
          onCancel={() => setDeleting(null)}
          onConfirm={() => void remove()}
        >
          <p>
            {deleting.path
              ? `Удалить ${deleting.path} из кэша зеркала?`
              : `Удалить из кэша все файлы ${deleting.repo} (${deleting.files.length})?`}{" "}
            Освободится {fmtBytes(deleting.files.reduce((s, f) => s + f.size, 0))}. Если файл понадобится нодам, зеркало
            скачает его с Hugging Face заново.
          </p>
        </Confirm>
      )}
    </div>
  );
}

function UsageCard({ cache }: { cache: ModelCache }) {
  const value = pct(cache.used_bytes, cache.limit_bytes);
  return (
    <section className="card stack" aria-label="Занятое место">
      <h2>Занятое место</h2>
      <p>
        Занято <span className="mono">{fmtBytes(cache.used_bytes)}</span> из лимита{" "}
        <span className="mono">{fmtBytes(cache.limit_bytes)}</span>, {fmtPercent(value)}
      </p>
      <Meter value={value} label="Занято в пределах лимита кэша" />
      <p className="hint">
        Можно скачать ещё {fmtBytes(roomBytes(cache))}. Свободно на диске {fmtBytes(cache.disk_free_bytes)} из{" "}
        {fmtBytes(cache.disk_total_bytes)}, зеркало держит в запасе {fmtBytes(cache.disk_reserve_bytes)}. Лимит кэша
        задаёт HF_MIRROR_MAX_GIB в .env главного.
      </p>
    </section>
  );
}

function FileRow({ file, onDelete }: { file: CacheFile; onDelete: () => void }) {
  const why = blocker(file);
  return (
    <tr aria-label={`${file.repo}/${file.path}`}>
      <td>
        <span className="mono small">{file.path}</span>
      </td>
      <td className="num mono">
        {file.partial && file.total ? `${fmtBytes(file.size)} из ${fmtBytes(file.total)}` : fmtBytes(file.size)}
      </td>
      <td className="mono small">{file.revision ? file.revision.slice(0, 7) : <span className="muted">—</span>}</td>
      <td className="small">{fmtEpoch(file.last_served)}</td>
      <td>
        <div className="stackcell">
          <span>{stateOf(file)}</span>
          {why && <span className="secondary small">{why}</span>}
        </div>
      </td>
      <td>
        <button
          type="button"
          className="btn quiet sm"
          aria-label={`Удалить файл ${file.path}`}
          disabled={why !== null}
          onClick={onDelete}
        >
          <Ic icon={Trash2} />
        </button>
      </td>
    </tr>
  );
}

/** «Скачать заранее»: a repository of Hugging Face, its GGUF files (a multi-file model is one choice) and projectors. */
function PrefetchForm({ cache, concurrency, onQueued }: { cache: ModelCache; concurrency: number; onQueued: () => Promise<void> }) {
  const [repo, setRepo] = useState("");
  const [found, setFound] = useState<{ repo: string; files: HfFile[]; mmproj: MmprojFile[] } | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [searching, setSearching] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [queued, setQueued] = useState<string | null>(null);

  async function search() {
    const target = repo.trim();
    if (!target) return;
    setSearching(true);
    setError(null);
    setQueued(null);
    setFound(null);
    setPicked(new Set());
    try {
      const [files, mmproj] = await Promise.all([api.hfFiles(target), api.hfMmproj(target).catch(() => [] as MmprojFile[])]);
      setFound({ repo: target, files, mmproj });
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Не удалось обратиться к Hugging Face");
    } finally {
      setSearching(false);
    }
  }

  const choices = [
    ...(found?.files ?? []).map((f) => ({ key: f.file, size: f.size_bytes, files: f.shards.length ? f.shards : [f.file], f })),
    ...(found?.mmproj ?? []).map((m) => ({ key: m.file, size: m.size_bytes, files: [m.file], f: null })),
  ];
  const chosen = choices.filter((c) => picked.has(c.key));
  const total = chosen.reduce((s, c) => s + c.size, 0);
  const files = chosen.flatMap((c) => c.files);
  const here = new Set(cache.files.filter((f) => !f.partial).map((f) => `${f.repo}/${f.path}`));

  function toggle(key: string) {
    setPicked((p) => {
      const next = new Set(p);
      if (!next.delete(key)) next.add(key);
      return next;
    });
  }

  async function submit() {
    if (!found || files.length === 0) return;
    setSending(true);
    setError(null);
    try {
      const r = await api.prefetch({ repo: found.repo, files });
      setQueued(`Поставлено в очередь: ${r.jobs.length} ${plural(r.jobs.length, ["файл", "файла", "файлов"])}. Ход загрузки виден ниже.`);
      setPicked(new Set());
      await onQueued();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Не удалось поставить загрузку в очередь");
    } finally {
      setSending(false);
    }
  }

  return (
    <form
      className="card stack"
      aria-label="Скачать заранее"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <h2>Скачать заранее</h2>
      <p className="hint">
        Зеркало скачает файлы с Hugging Face в фоне, одновременно не больше {concurrency}. Когда развёртывание дойдёт до
        нод, они возьмут готовые файлы по сети.
      </p>
      <div className="row end">
        <div className="field grow">
          <label htmlFor="pf-repo">Репозиторий Hugging Face</label>
          <input
            id="pf-repo"
            className="input mono"
            value={repo}
            placeholder="владелец/репозиторий"
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => {
              setRepo(e.target.value);
              setFound(null);
              setPicked(new Set());
              setQueued(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void search();
              }
            }}
          />
        </div>
        <button type="button" className="btn" disabled={!repo.trim() || searching} onClick={() => void search()}>
          <Ic icon={Search} />
          Найти файлы
        </button>
      </div>
      {error && <ErrorNotice error={error} />}
      {found && choices.length === 0 && <p className="muted">В репозитории нет файлов GGUF</p>}
      {choices.length > 0 && (
        <fieldset>
          <legend>Что скачать</legend>
          <div className="choices">
            {choices.map((c) => (
              <label key={c.key} className="choice">
                <input type="checkbox" checked={picked.has(c.key)} onChange={() => toggle(c.key)} />
                {c.f ? <span className="tag">{c.f.quant ?? "без метки"}</span> : <span className="tag">проектор</span>}{" "}
                <span className="mono">{fmtBytes(c.size)}</span>{" "}
                <span className="grow mono muted small">{c.key}</span>
                {c.files.length > 1 && <span className="muted small">{c.files.length} части</span>}
                {c.files.every((p) => here.has(`${found?.repo}/${p}`)) && <span className="muted small">уже в кэше</span>}
              </label>
            ))}
          </div>
        </fieldset>
      )}
      {total > cache.limit_bytes ? (
        <Notice kind="error">
          Выбрано {fmtBytes(total)}, а лимит кэша {fmtBytes(cache.limit_bytes)}: такой объём не поместится. Увеличьте
          HF_MIRROR_MAX_GIB.
        </Notice>
      ) : (
        total > roomBytes(cache) && (
          <Notice kind="warn">
            Выбрано {fmtBytes(total)}, свободно {fmtBytes(roomBytes(cache))}. Чтобы освободить место, зеркало удалит давно
            не использованные файлы, в том числе нужные развёртываниям.
          </Notice>
        )
      )}
      <div className="row" style={{ alignItems: "center" }}>
        <button type="submit" className="btn primary" disabled={files.length === 0 || sending || total > cache.limit_bytes}>
          <Ic icon={Download} />
          Скачать заранее
        </button>
        {files.length > 0 && (
          <span className="hint">
            {files.length} {plural(files.length, ["файл", "файла", "файлов"])}, {fmtBytes(total)}
          </span>
        )}
        {queued && (
          <span className="secondary small" role="status">
            {queued}
          </span>
        )}
      </div>
    </form>
  );
}

function JobsCard({ jobs, onChanged }: { jobs: PrefetchJob[]; onChanged: () => Promise<void> }) {
  const [error, setError] = useState<string | null>(null);
  const finished = jobs.filter((j) => j.state === "done" || j.state === "failed" || j.state === "cancelled");

  async function run(action: () => Promise<void>, fail: string) {
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : fail);
    }
    await onChanged();
  }

  return (
    <section className="card stack" aria-label="Загрузки">
      <div className="row" style={{ alignItems: "center" }}>
        <h2 className="grow">Загрузки</h2>
        {finished.length > 0 && (
          <button type="button" className="btn quiet sm" onClick={() => void run(() => api.clearPrefetch(), "Не удалось очистить список")}>
            Убрать завершённые
          </button>
        )}
      </div>
      {error && <ErrorNotice error={error} />}
      <ul className="stack" style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {jobs.map((j) => (
          <JobRow key={j.id} job={j} onAct={(fail) => run(() => api.cancelPrefetch(j.id), fail)} />
        ))}
      </ul>
    </section>
  );
}

function JobRow({ job, onAct }: { job: PrefetchJob; onAct: (fail: string) => Promise<void> }) {
  const label = `Загрузка ${job.path}`;
  const live = job.state === "queued" || job.state === "running";
  const value = job.size ? pct(job.written, job.size) : 0;
  let text: string;
  if (job.state === "queued") text = "В очереди";
  else if (job.state === "running") {
    const eta = job.size && job.speed_bps > 0 ? fmtEta((job.size - job.written) / job.speed_bps) : "";
    text = job.size
      ? `${value} %, ${fmtBytes(job.written)} из ${fmtBytes(job.size)}${job.speed_bps > 0 ? `, ${fmtBytes(job.speed_bps)}/с` : ""}${eta ? `, осталось ${eta}` : ""}`
      : `Начало загрузки, ${fmtBytes(job.written)}`;
  } else if (job.state === "done") text = job.cached ? "Уже в кэше" : `Готово, ${fmtBytes(job.size ?? job.written)}`;
  else if (job.state === "cancelled") text = "Отменено";
  else text = "Ошибка";
  return (
    <li className="stack" aria-label={label}>
      <div className="row" style={{ alignItems: "center" }}>
        <span className="mono small grow">
          {job.repo}/{job.path}
        </span>
        <span className="small">{text}</span>
        <button
          type="button"
          className="btn quiet sm"
          aria-label={live ? `Отменить загрузку ${job.path}` : `Убрать ${job.path} из списка`}
          onClick={() => void onAct(live ? "Не удалось отменить загрузку" : "Не удалось убрать из списка")}
        >
          <Ic icon={X} />
          {live ? "Отменить" : "Убрать"}
        </button>
      </div>
      {job.state === "running" && job.size ? <Meter value={value} label={label} /> : null}
      {job.state === "failed" && job.error && <span className="error-text">{job.error}</span>}
    </li>
  );
}
