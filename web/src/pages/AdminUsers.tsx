import { useState } from "react";
import { api } from "../api";
import { useMe } from "../auth";
import { UserRowActions } from "../components/UserRowActions";
import { ErrorNotice, Loading, PageHead } from "../components/ui";
import { fmtDateTime, fmtInt } from "../lib/format";
import { useDebounced, useResource } from "../lib/useResource";
import type { AdminUserFull } from "../types";
import "../styles/admin-users.css";

const PAGE_SIZE = 25;

const limitsText = (u: AdminUserFull): string | null => {
  const parts = [
    u.rpm_limit !== null ? `${fmtInt(u.rpm_limit)} запр./мин` : null,
    u.tpm_limit !== null ? `${fmtInt(u.tpm_limit)} ток./мин` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(", ") : null;
};

export function AdminUsers() {
  const me = useMe();
  const [q, setQ] = useState("");
  const [role, setRole] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(0);
  const [actionError, setActionError] = useState<string | null>(null);
  const search = useDebounced(q.trim(), 300);

  const users = useResource(
    () =>
      api.users({
        q: search,
        role,
        blocked: status === "" ? undefined : status === "blocked",
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    [search, role, status, page],
  );
  const provider = useResource(() => api.publicStatus(), []).data?.login_provider || "SSO";
  const rows = users.data?.items ?? [];
  const total = users.data?.total ?? 0;
  const last = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);

  const filter = (set: (v: string) => void) => (v: string) => {
    set(v);
    setPage(0);
  };

  return (
    <div className="page">
      <PageHead
        title="Пользователи"
        lead={`Все, кто входил через ${provider}. Токены считаются по всем ключам пользователя за 30 дней и обновляются раз в пять минут.`}
      />
      <ErrorNotice error={users.error} />
      <ErrorNotice error={actionError} />
      <div className="filter-bar">
        <div className="field grow">
          <label htmlFor="user-search">Поиск</label>
          <input
            id="user-search"
            className="input"
            type="search"
            placeholder="Имя или почта"
            value={q}
            onChange={(e) => filter(setQ)(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="user-role">Роль</label>
          <select id="user-role" className="select" value={role} onChange={(e) => filter(setRole)(e.target.value)}>
            <option value="">Все роли</option>
            <option value="admin">Администраторы</option>
            <option value="user">Пользователи</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="user-status">Доступ</label>
          <select id="user-status" className="select" value={status} onChange={(e) => filter(setStatus)(e.target.value)}>
            <option value="">Все</option>
            <option value="active">Активные</option>
            <option value="blocked">Заблокированные</option>
          </select>
        </div>
      </div>
      {users.loading && !users.data && <Loading />}
      {users.data && (
        <div className="list-block">
          <div className="table-wrap">
            <table className="data" aria-label="Пользователи">
              <thead>
                <tr>
                  <th>Пользователь</th>
                  <th>Роль</th>
                  <th className="num">Ключи</th>
                  <th>Лимиты</th>
                  <th className="num">Токены за 30 дней</th>
                  <th>Последний вход</th>
                  <th>Первый вход</th>
                  <th className="actions">Действия</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((u) => (
                  <tr key={u.id}>
                    <td className="reason">
                      <div className="stackcell">
                        <span>
                          {u.full_name || u.email}
                          {u.blocked && (
                            <>
                              {" "}
                              <span className="tag bad">заблокирован</span>
                            </>
                          )}
                        </span>
                        <span className="mono small muted">{u.email}</span>
                        {u.blocked && u.blocked_reason && <span className="small muted">Причина: {u.blocked_reason}</span>}
                      </div>
                    </td>
                    <td>
                      <div className="stackcell">
                        <span>{u.role === "admin" ? "Администратор" : "Пользователь"}</span>
                        {u.role_source === "manual" && <span className="small muted">назначена вручную</span>}
                      </div>
                    </td>
                    <td className="num">{u.max_keys !== null ? `${u.keys} из ${u.max_keys}` : u.keys}</td>
                    <td>{limitsText(u) ?? <span className="muted">общие</span>}</td>
                    <td className="num">{u.tokens_30d === null ? "—" : fmtInt(u.tokens_30d)}</td>
                    <td>{fmtDateTime(u.last_login_at)}</td>
                    <td>{fmtDateTime(u.created_at)}</td>
                    <td className="actions">
                      <UserRowActions
                        user={u}
                        myId={me.id}
                        onError={setActionError}
                        onChanged={() => void users.reload()}
                      />
                    </td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={8} className="empty-row">
                      {search || role || status ? "Никого не нашли" : "Пользователей пока нет"}
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
