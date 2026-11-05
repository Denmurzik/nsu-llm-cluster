import { ChevronDown, ChevronRight, Download } from "lucide-react";
import { Fragment, useState } from "react";
import { api, type AuditQuery } from "../api";
import { ErrorNotice, Ic, Loading, PageHead } from "../components/ui";
import { ACTION_GROUPS, actionLabel, detailLabel, detailText, targetText, targetTypeLabel } from "../lib/auditLabels";
import { fmtDateTime, fmtInt } from "../lib/format";
import { useDebounced, useResource } from "../lib/useResource";
import "../styles/admin-users.css";

const PAGE_SIZE = 50;
const EXPORT_MAX = 50_000; // the same cut as the portal's CSV export

/** «2026-11-02T10:30» from a datetime-local field (the browser's time zone) -> ISO time for the portal */
const isoOf = (local: string): string | undefined => {
  if (!local) return undefined;
  const date = new Date(local);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
};

export function AdminAudit() {
  const [group, setGroup] = useState("");
  const [actor, setActor] = useState("");
  const [target, setTarget] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<Set<number>>(new Set());
  const dActor = useDebounced(actor.trim(), 300);
  const dTarget = useDebounced(target.trim(), 300);

  const sinceIso = isoOf(since);
  const untilIso = isoOf(until);

  const query: AuditQuery = {
    action: group || undefined,
    actor: dActor || undefined,
    target: dTarget || undefined,
    since: sinceIso,
    until: untilIso,
  };
  const log = useResource(
    () => api.audit({ ...query, limit: PAGE_SIZE, offset: page * PAGE_SIZE }),
    [group, dActor, dTarget, since, until, page],
  );
  const rows = log.data?.items ?? [];
  const total = log.data?.total ?? 0;
  const last = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);

  const filter = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setPage(0);
  };
  const toggle = (id: number) =>
    setOpen((s) => {
      const next = new Set(s);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  return (
    <div className="page">
      <PageHead
        title="Журнал"
        lead="Кто и что менял в панели: развёртывания, ноды, пользователи, ключи и настройки. Новые записи сверху."
        actions={
          <a className="btn" href={api.auditExportUrl(query)} download>
            <Ic icon={Download} />
            Скачать CSV
          </a>
        }
      />
      <ErrorNotice error={log.error} />
      <div className="filter-bar">
        <div className="field">
          <label htmlFor="audit-action">Действие</label>
          <select id="audit-action" className="select" value={group} onChange={(e) => filter(setGroup)(e.target.value)}>
            {ACTION_GROUPS.map((g) => (
              <option key={g.prefix} value={g.prefix}>
                {g.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="audit-actor">Исполнитель</label>
          <input
            id="audit-actor"
            className="input"
            placeholder="Почта"
            value={actor}
            onChange={(e) => filter(setActor)(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="audit-target">Объект</label>
          <input
            id="audit-target"
            className="input"
            placeholder="Имя или номер"
            value={target}
            onChange={(e) => filter(setTarget)(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="audit-since">С</label>
          <input
            id="audit-since"
            className="input"
            type="datetime-local"
            value={since}
            onChange={(e) => filter(setSince)(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="audit-until">По</label>
          <input
            id="audit-until"
            className="input"
            type="datetime-local"
            value={until}
            onChange={(e) => filter(setUntil)(e.target.value)}
          />
        </div>
      </div>
      {log.loading && !log.data && <Loading />}
      {log.data && (
        <div className="list-block">
          <div className="table-wrap">
            <table className="data" aria-label="Журнал действий">
              <thead>
                <tr>
                  <th className="chev">
                    <span className="visually-hidden">Подробности</span>
                  </th>
                  <th>Время</th>
                  <th>Исполнитель</th>
                  <th>Действие</th>
                  <th>Объект</th>
                  <th>Адрес</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((e) => {
                  const expanded = open.has(e.id);
                  const hasDetails = e.details !== null && Object.keys(e.details).length > 0;
                  return (
                    <Fragment key={e.id}>
                      <tr>
                        <td className="chev">
                          {hasDetails && (
                            <button
                              type="button"
                              className="btn quiet sm icon-only"
                              aria-expanded={expanded}
                              aria-label={`Подробности: ${actionLabel(e.action)}, ${targetText(e)}`}
                              onClick={() => toggle(e.id)}
                            >
                              <Ic icon={expanded ? ChevronDown : ChevronRight} />
                            </button>
                          )}
                        </td>
                        <td className="nowrap">{fmtDateTime(e.at)}</td>
                        <td>{e.actor_email ?? <span className="muted">система</span>}</td>
                        <td>
                          <div className="stackcell">
                            <span>{actionLabel(e.action)}</span>
                            <span className="mono small muted">{e.action}</span>
                          </div>
                        </td>
                        <td>
                          <div className="stackcell">
                            <span className="mono">{targetText(e)}</span>
                            <span className="small muted">{targetTypeLabel(e.target_type)}</span>
                          </div>
                        </td>
                        <td className="mono small">{e.ip ?? "—"}</td>
                      </tr>
                      {expanded && hasDetails && (
                        <tr className="audit-detail">
                          <td colSpan={6}>
                            <dl className="audit-details" aria-label={`Подробности записи ${e.id}`}>
                              {Object.entries(e.details ?? {}).map(([k, v]) => (
                                <Fragment key={k}>
                                  <dt>{detailLabel(k)}</dt>
                                  <dd>{detailText(v)}</dd>
                                </Fragment>
                              ))}
                            </dl>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={6} className="empty-row">
                      {group || dActor || dTarget || since || until ? "По этим условиям записей нет" : "В журнале пока пусто"}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="pager">
            <span>
              {total === 0
                ? "Нет записей"
                : `Показано ${fmtInt(page * PAGE_SIZE + 1)}–${fmtInt(page * PAGE_SIZE + rows.length)} из ${fmtInt(total)}`}
              {total > EXPORT_MAX &&
                `. В файл CSV попадут только ${fmtInt(EXPORT_MAX)} самых новых записей: сузьте период или фильтры`}
            </span>
            <div className="pages">
              <button type="button" className="btn sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
                Назад
              </button>
              <button type="button" className="btn sm" disabled={page >= last} onClick={() => setPage(page + 1)}>
                Дальше
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
