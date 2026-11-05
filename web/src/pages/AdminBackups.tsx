import { DatabaseBackup, Download, RotateCcw, Trash2 } from "lucide-react";
import { useState } from "react";
import { api, ApiError } from "../api";
import { CopyButton } from "../components/CopyButton";
import { Confirm, Modal } from "../components/Modal";
import { ErrorNotice, Ic, Loading, Notice, PageHead } from "../components/ui";
import { fmtBytes, fmtDateTime } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { BackupEntry, BackupsData } from "../types";
import "../styles/admin-ops.css";

const POLL_MS = 5000;

/** «17 21 * * *» -> «каждый день в 21:17 UTC»; any other schedule is shown as it is. */
export function scheduleText(cron: string): string {
  const m = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(cron.trim());
  if (!m) return `по расписанию cron «${cron}», время UTC`;
  return `каждый день в ${m[2].padStart(2, "0")}:${m[1].padStart(2, "0")} UTC`;
}

const kindLabel = (e: BackupEntry) => (e.kind === "pre-restore" ? "Перед восстановлением" : "Ежедневная копия");
const download = (file: string) => `/api/admin/backups/${encodeURIComponent(file)}/download`;

export function AdminBackups() {
  const backups = useResource(() => api.backups(), [], { interval: POLL_MS });
  const data = backups.data;
  const [starting, setStarting] = useState(false);
  const [started, setStarted] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [restoring, setRestoring] = useState<BackupEntry | null>(null);
  const [deleting, setDeleting] = useState<BackupEntry | null>(null);
  const [busy, setBusy] = useState(false);

  async function run() {
    setStarting(true);
    setActionError(null);
    setStarted(null);
    try {
      const r = await api.runBackup();
      setStarted(r.job);
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setStarting(false);
      void backups.reload();
    }
  }

  async function remove() {
    if (!deleting) return;
    setBusy(true);
    setActionError(null);
    try {
      await api.deleteBackup(deleting.name);
      setDeleting(null);
    } catch (e) {
      setDeleting(null);
      setActionError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setBusy(false);
      void backups.reload();
    }
  }

  const running = data?.running === true;
  return (
    <div className="page">
      <PageHead
        title="Резервные копии"
        lead="Копии баз portal и litellm главного сервера: пользователи, ключи, развёртывания и статистика. Восстановление запускается в терминале главного сервера."
        actions={
          <button type="button" className="btn primary" onClick={() => void run()} disabled={starting || running}>
            <Ic icon={DatabaseBackup} />
            {running ? "Копия делается" : "Сделать копию сейчас"}
          </button>
        }
      />
      <ErrorNotice error={backups.error && !data ? backups.error : null} />
      {actionError && <ErrorNotice error={actionError} />}
      {started && !actionError && (
        <Notice kind="info">Копия запущена, она появится в списке через несколько секунд. Задание: {started}</Notice>
      )}
      {backups.loading && !data && <Loading />}
      {backups.error && data && (
        <Notice kind="warn">Не удалось обновить данные, показаны данные последней успешной загрузки: {backups.error.message}</Notice>
      )}

      {data && (
        <>
          <Facts data={data} />
          <section className="section">
            <h2>Копии</h2>
            {!data.available && <Notice kind="warn">Каталог копий недоступен</Notice>}
            <div className="table-wrap">
              <table className="data" aria-label="Резервные копии">
                <thead>
                  <tr>
                    <th>Время</th>
                    <th>Вид</th>
                    <th className="num">Размер</th>
                    <th>Файлы</th>
                    <th>
                      <span className="visually-hidden">Действия</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((e) => (
                    <tr key={e.name}>
                      <td className="nowrap">{fmtDateTime(e.at)}</td>
                      <td>
                        <div className="stackcell">
                          <span>{kindLabel(e)}</span>
                          {!e.complete && <span className="muted small">Неполный набор: есть не все базы</span>}
                        </div>
                      </td>
                      <td className="num">{fmtBytes(e.size_bytes)}</td>
                      <td>
                        <div className="file-links">
                          {e.files.map((f) => (
                            <div className="stackcell" key={f.name}>
                              <a className="mono" href={download(f.name)} download aria-label={`Скачать ${f.name}`}>
                                <Ic icon={Download} /> {f.db}, {fmtBytes(f.size_bytes)}
                              </a>
                              {f.sha256 && (
                                <span className="sha" title={`sha256 ${f.sha256}`}>
                                  sha256 {f.sha256.slice(0, 16)}…
                                </span>
                              )}
                            </div>
                          ))}
                        </div>
                      </td>
                      <td>
                        <div className="row-actions">
                          {e.kind === "set" && (
                            <button
                              type="button"
                              className="btn sm"
                              disabled={!e.restore_command}
                              title={e.restore_command ? undefined : "В наборе есть не все базы"}
                              onClick={() => setRestoring(e)}
                              aria-label={`Как восстановить копию от ${fmtDateTime(e.at)}`}
                            >
                              <Ic icon={RotateCcw} />
                              Восстановить
                            </button>
                          )}
                          <button
                            type="button"
                            className="btn sm"
                            onClick={() => setDeleting(e)}
                            aria-label={`Удалить копию от ${fmtDateTime(e.at)}`}
                          >
                            <Ic icon={Trash2} />
                            Удалить
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {data.items.length === 0 && (
                    <tr>
                      <td colSpan={5} className="empty-row">
                        Копий пока нет
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <p className="hint">
              Страховочная копия снимается автоматически перед каждым восстановлением; в ней лежат базы, которые были заменены.
            </p>
          </section>
        </>
      )}

      {restoring && <Restore entry={restoring} onClose={() => setRestoring(null)} />}
      {deleting && (
        <Confirm
          title="Удаление копии"
          confirmLabel="Удалить"
          busy={busy}
          onCancel={() => setDeleting(null)}
          onConfirm={() => void remove()}
        >
          <p>
            Удалить копию от {fmtDateTime(deleting.at)} ({kindLabel(deleting).toLowerCase()}, {fmtBytes(deleting.size_bytes)}) со всеми
            её файлами? Файлы удаляются с диска главного, это действие нельзя отменить.
          </p>
        </Confirm>
      )}
    </div>
  );
}

function Facts({ data }: { data: BackupsData }) {
  const { schedule, disk, last_run: last } = data;
  return (
    <section className="section">
      <h2>Расписание и место</h2>
      <div className="card">
        <dl className="facts">
          <dt>Расписание</dt>
          <dd>
            {schedule ? (
              <>
                {scheduleText(schedule.cron)}
                {schedule.suspended && <span className="muted"> (приостановлено)</span>}
              </>
            ) : (
              <span className="muted">Нет данных: Kubernetes недоступен</span>
            )}
          </dd>
          <dt>Хранится</dt>
          <dd>
            {data.retention_keep !== null ? (
              `${data.retention_keep} последних наборов`
            ) : (
              <span className="muted">Нет данных</span>
            )}
          </dd>
          <dt>Свободно на диске</dt>
          <dd>
            {disk ? (
              `${fmtBytes(disk.free_bytes)} из ${fmtBytes(disk.total_bytes)}`
            ) : (
              <span className="muted">Нет данных</span>
            )}
          </dd>
          <dt>Последний запуск</dt>
          <dd>
            {last ? (
              <>
                {last.state === "succeeded" ? "успешно" : "с ошибкой"}, {fmtDateTime(last.finished_at ?? last.started_at)}
                {last.manual && <span className="muted"> (вручную)</span>}
              </>
            ) : (
              <span className="muted">Нет данных</span>
            )}
          </dd>
        </dl>
      </div>
      {last?.state === "failed" && (
        <Notice kind="warn">
          Последний запуск задания закончился ошибкой. Журнал на главном сервере:{" "}
          <span className="mono">docker exec llm-main k3s kubectl -n llm logs job/{last.name}</span>
        </Notice>
      )}
    </section>
  );
}

function Restore({ entry, onClose }: { entry: BackupEntry; onClose: () => void }) {
  const command = entry.restore_command ?? "";
  return (
    <Modal
      title="Восстановление из копии"
      onClose={onClose}
      wide
      footer={
        <button type="button" className="btn" onClick={onClose} data-autofocus>
          Закрыть
        </button>
      }
    >
      <p>
        Базы portal и litellm вернутся к состоянию на {fmtDateTime(entry.at)}. Всё, что записано после этой копии, пропадёт.
        Команда выполняется в терминале главного сервера:
      </p>
      <div className="code inline">
        <code>{command}</code>
        <CopyButton text={command} label="Копировать команду" />
      </div>
      <ol>
        <li>Скрипт проверит, что файлы набора читаются, и загрузит их во временные базы: пока что-то не так, рабочие базы не тронуты.</li>
        <li>Затем он остановит шлюз моделей и портал, снимет страховочную копию текущих баз, заменит базы и запустит всё обратно.</li>
        <li>На время замены эта страница и API недоступны. Проверка после восстановления: открыть портал через минуту.</li>
      </ol>
      <p className="hint">
        Восстановление не запускается из панели: оно останавливает сам портал. Файл secrets.env в копию не входит: храните его отдельно.
      </p>
    </Modal>
  );
}
