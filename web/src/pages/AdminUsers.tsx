import { api } from "../api";
import { ErrorNotice, Loading, PageHead } from "../components/ui";
import { fmtDateTime, fmtInt } from "../lib/format";
import { useResource } from "../lib/useResource";

export function AdminUsers() {
  const users = useResource(() => api.users(), []);
  return (
    <div className="page">
      <PageHead title="Пользователи" lead="Все, кто входил через NSU ID. Токены считаются по всем ключам пользователя за 30 дней." />
      <ErrorNotice error={users.error} />
      {users.loading && !users.data && <Loading />}
      {users.data && (
        <div className="table-wrap">
          <table className="data" aria-label="Пользователи">
            <thead>
              <tr>
                <th>Пользователь</th>
                <th>Роль</th>
                <th className="num">Ключи</th>
                <th className="num">Токены за 30 дней</th>
                <th>Последний вход</th>
                <th>Первый вход</th>
              </tr>
            </thead>
            <tbody>
              {users.data.map((u) => (
                <tr key={u.id}>
                  <td>
                    <div className="stackcell">
                      <span>{u.full_name || u.email}</span>
                      <span className="mono small muted">{u.email}</span>
                    </div>
                  </td>
                  <td>{u.role === "admin" ? "Администратор" : "Пользователь"}</td>
                  <td className="num">{u.keys}</td>
                  <td className="num">{u.tokens_30d === null ? "—" : fmtInt(u.tokens_30d)}</td>
                  <td>{fmtDateTime(u.last_login_at)}</td>
                  <td>{fmtDateTime(u.created_at)}</td>
                </tr>
              ))}
              {users.data.length === 0 && (
                <tr>
                  <td colSpan={6} className="empty-row">
                    Пользователей пока нет
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
