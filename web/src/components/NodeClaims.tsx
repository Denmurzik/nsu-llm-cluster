import { useState } from "react";
import { api, ApiError } from "../api";
import { fmtDateTime } from "../lib/format";
import type { NodeClaim, NodeInfo } from "../types";
import { Confirm, Modal } from "./Modal";

/**
 * Claims on a known name (portal task SEC-1, fix round 2): registrations of the name without its key. The name keeps
 * working; the admin compares each claim's address with every address the portal knows for the name and either makes one
 * claim the node (an explicit confirmation of that one claim, never in bulk) or rejects them.
 */
export function NodeClaimsDialog({ node, onClose, onDone }: { node: NodeInfo; onClose: () => void; onDone: () => void | Promise<void> }) {
  const claims = node.claims ?? [];
  const known = node.known_addresses ?? [];
  const [picked, setPicked] = useState<NodeClaim | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<unknown>, fallback: string) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await onDone();
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : fallback);
    } finally {
      setBusy(false);
    }
  }

  if (picked) {
    return (
      <Confirm
        title={`Сделать заявку нодой ${node.name}`}
        confirmLabel="Сделать нодой"
        busy={busy}
        onCancel={() => setPicked(null)}
        onConfirm={() => void run(() => api.approveNode(node.name, picked.id), "Не удалось подтвердить заявку")}
      >
        <p>
          Нодой <span className="mono">{node.name}</span> станет машина с адреса <span className="mono">{picked.ip ?? "неизвестно"}</span>.
          Остальные заявки будут отклонены, нынешняя нода с этим именем потеряет его.
        </p>
        <p>
          Знакомые адреса имени: <span className="mono">{known.length ? known.join(", ") : "нет"}</span>.
        </p>
        {!picked.known && (
          <p className="error-text" role="alert">
            Адрес заявки не совпадает ни с одним знакомым адресом. Подтверждайте, только если вы сами перенесли или переустановили
            эту машину.
          </p>
        )}
        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
      </Confirm>
    );
  }

  return (
    <Modal
      title={`Заявки на имя ${node.name}`}
      onClose={onClose}
      footer={
        <>
          <button
            type="button"
            className="btn danger"
            disabled={busy || claims.length === 0}
            onClick={() => void run(() => api.rejectClaims(node.name), "Не удалось отклонить заявки")}
          >
            Отклонить все
          </button>
          <button type="button" className="btn" onClick={onClose} data-autofocus>
            Закрыть
          </button>
        </>
      }
    >
      <p>
        Имя уже известно порталу, но эти регистрации пришли без ключа ноды. Пока вы не выберете заявку, нода работает как раньше.
        Сверьте адреса: заявка с незнакомого адреса — повод не подтверждать.
      </p>
      <p>
        Знакомые адреса имени: <span className="mono">{known.length ? known.join(", ") : "нет"}</span>
      </p>
      {(node.claims_refused ?? 0) > 0 && (
        <p className="error-text">Ещё {node.claims_refused} регистраций отклонено: заявок было слишком много (см. журнал, node.claim_refused).</p>
      )}
      <table className="data dense" aria-label="Заявки">
        <thead>
          <tr>
            <th>Адрес</th>
            <th>Когда</th>
            <th>Сверка</th>
            <th aria-label="Действия" />
          </tr>
        </thead>
        <tbody>
          {claims.map((c) => (
            <tr key={c.id}>
              <td className="mono">{c.ip ?? "?"}</td>
              <td className="nowrap">{fmtDateTime(c.at)}</td>
              <td>{c.known ? "знакомый адрес" : <span className="error-text">незнакомый адрес</span>}</td>
              <td className="actions nowrap">
                <button type="button" className="btn sm" disabled={busy} onClick={() => setPicked(c)}>
                  Сделать нодой…
                </button>
                <button
                  type="button"
                  className="btn quiet sm"
                  disabled={busy}
                  onClick={() => void run(() => api.rejectClaims(node.name, c.id), "Не удалось отклонить заявку")}
                >
                  Отклонить
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
    </Modal>
  );
}
