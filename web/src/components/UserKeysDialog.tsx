import { useState } from "react";
import { api, ApiError } from "../api";
import { fmtDate, fmtInt } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { AdminUserFull, ApiKey } from "../types";
import { Confirm, Modal } from "./Modal";
import { ErrorNotice, Loading } from "./ui";
import "../styles/admin-users.css";

/**
 * The keys of one user as the user sees them (only the masked value), with «Отозвать». Revoking asks first and takes
 * the place of the list while it does, so two dialogs are never open at once.
 */
export function UserKeysDialog({
  user,
  onClose,
  onChanged,
}: {
  user: AdminUserFull;
  onClose: () => void;
  onChanged: () => void;
}) {
  const keys = useResource(() => api.userKeys(user.id), [user.id]);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function revoke(key: ApiKey) {
    setBusy(true);
    setError(null);
    try {
      await api.revokeUserKey(user.id, key.id);
      await keys.reload();
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Не удалось отозвать ключ");
    } finally {
      setBusy(false);
      setRevoking(null);
    }
  }

  if (revoking) {
    return (
      <Confirm
        title="Отзыв ключа"
        confirmLabel="Отозвать"
        busy={busy}
        onCancel={() => setRevoking(null)}
        onConfirm={() => void revoke(revoking)}
      >
        <p>
          Отозвать ключ «{revoking.alias}» пользователя {user.full_name || user.email}?
        </p>
        <p className="secondary">Ключ перестанет работать сразу, вернуть его нельзя.</p>
      </Confirm>
    );
  }

  return (
    <Modal
      title={`Ключи: ${user.email}`}
      wide
      onClose={onClose}
      footer={
        <button type="button" className="btn" onClick={onClose} data-autofocus>
          Закрыть
        </button>
      }
    >
      <div className="user-keys">
        <ErrorNotice error={keys.error} />
        <ErrorNotice error={error} />
        {keys.loading && !keys.data && <Loading />}
        {keys.data && (
          <div className="table-wrap">
            <table className="data" aria-label="Ключи пользователя">
              <thead>
                <tr>
                  <th>Название</th>
                  <th>Ключ</th>
                  <th>Создан</th>
                  <th>Истекает</th>
                  <th className="num">Токены за 30 дней</th>
                  <th className="num">Запросы</th>
                  <th className="actions">Действия</th>
                </tr>
              </thead>
              <tbody>
                {keys.data.keys.map((k) => (
                  <tr key={k.id}>
                    <td>{k.alias}</td>
                    <td className="mono">{k.masked}</td>
                    <td>{fmtDate(k.created_at)}</td>
                    <td>{k.expires_at ? fmtDate(k.expires_at) : "Без срока"}</td>
                    <td className="num">{keys.data?.usageUnavailable ? "—" : fmtInt(k.tokens_30d)}</td>
                    <td className="num">{keys.data?.usageUnavailable ? "—" : fmtInt(k.requests_30d)}</td>
                    <td className="actions">
                      <button type="button" className="btn sm" aria-label={`Отозвать ключ ${k.alias}`} onClick={() => setRevoking(k)}>
                        Отозвать
                      </button>
                    </td>
                  </tr>
                ))}
                {keys.data.keys.length === 0 && (
                  <tr>
                    <td colSpan={7} className="empty-row">
                      У пользователя нет ключей
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
        {keys.data?.usageUnavailable && <p className="hint">Шлюз не отдал расход, поэтому токены и запросы не показаны.</p>}
      </div>
    </Modal>
  );
}
