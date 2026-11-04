import { ScrollText } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { fmtBytes } from "../lib/format";
import { loadLabel, phaseLabel, roleLabel } from "../lib/labels";
import { useResource } from "../lib/useResource";
import type { DeploymentStatus, PodStatus } from "../types";
import { Modal } from "./Modal";
import { ErrorNotice, Ic, Loading } from "./ui";

export function PodStatusTable({ status }: { status: DeploymentStatus }) {
  const [logsFor, setLogsFor] = useState<string | null>(null);
  const rows = status.nodes.flatMap((n) => n.pods.map((p) => ({ node: n, pod: p })));
  const emptyNodes = status.nodes.filter((n) => n.pods.length === 0);

  return (
    <div className="section">
      {status.groups.length > 0 && (
        <p className="secondary">
          Группы:{" "}
          {status.groups.map((g, i) => (
            <span key={i} className="tag" style={{ marginRight: 4 }}>
              {g.join(" + ")}
            </span>
          ))}
        </p>
      )}
      <div className="table-wrap">
        <table className="data" aria-label="Поды развёртывания">
          <thead>
            <tr>
              <th>Нода</th>
              <th>Под</th>
              <th>Роль</th>
              <th>Фаза</th>
              <th>Готовность</th>
              <th className="num">Перезапуски</th>
              <th>Сообщение</th>
              <th aria-label="Действия" />
            </tr>
          </thead>
          <tbody>
            {rows.map(({ node, pod }) => (
              <tr key={pod.name}>
                <td>
                  <div className="stackcell">
                    <span className="mono nowrap">{node.name}</span>
                    <span className="muted small">{node.applied ? "Конфигурация применена" : "Ожидает применения"}</span>
                  </div>
                </td>
                <td className="mono nowrap">{pod.name}</td>
                <td>{roleLabel(pod.role)}</td>
                <td>{phaseLabel(pod.phase)}</td>
                <td>
                  <Readiness pod={pod} />
                </td>
                <td className="num">{pod.restarts}</td>
                <td>
                  {pod.message || pod.last_log ? (
                    <div className="stackcell">
                      {pod.message && <span>{pod.message}</span>}
                      {pod.last_log && <span className="mono small muted">{pod.last_log}</span>}
                    </div>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td className="actions">
                  <button
                    type="button"
                    className="btn quiet sm"
                    aria-label={`Логи пода ${pod.name}`}
                    onClick={() => setLogsFor(pod.name)}
                  >
                    <Ic icon={ScrollText} />
                    Логи
                  </button>
                </td>
              </tr>
            ))}
            {emptyNodes.map((n) => (
              <tr key={`empty-${n.name}`}>
                <td>
                  <div className="stackcell">
                    <span className="mono nowrap">{n.name}</span>
                    <span className="muted small">{n.applied ? "Конфигурация применена" : "Ожидает применения"}</span>
                  </div>
                </td>
                <td colSpan={7} className="muted">
                  Подов на ноде пока нет
                </td>
              </tr>
            ))}
            {status.nodes.length === 0 && (
              <tr>
                <td colSpan={8} className="empty-row">
                  Ноды ещё не назначены
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {logsFor && <LogsDialog pod={logsFor} onClose={() => setLogsFor(null)} />}
    </div>
  );
}

/** Ready, or how far the pod is: downloading with a share of the model, loading the weights, starting. */
function Readiness({ pod }: { pod: PodStatus }) {
  const p = pod.progress;
  const label = pod.ready ? "Готов" : p ? loadLabel(p) : "Не готов";
  const bytes =
    !pod.ready && p?.stage === "downloading" && p.done_bytes !== null && p.total_bytes !== null
      ? `${fmtBytes(p.done_bytes)} из ${fmtBytes(p.total_bytes)}`
      : null;
  return (
    <div className="stackcell">
      <span className="status">
        <i className={`dot ${pod.ready ? "ready" : "starting"}`} aria-hidden="true" />
        <span>{label}</span>
      </span>
      {bytes && <span className="muted small nowrap">{bytes}</span>}
    </div>
  );
}

function LogsDialog({ pod, onClose }: { pod: string; onClose: () => void }) {
  const logs = useResource(() => api.podLogs(pod, 200), [pod]);
  return (
    <Modal
      title={`Логи пода ${pod}`}
      wide
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={() => void logs.reload()}>
            Обновить
          </button>
          <button type="button" className="btn primary" onClick={onClose} data-autofocus>
            Закрыть
          </button>
        </>
      }
    >
      {logs.loading && !logs.data && <Loading />}
      <ErrorNotice error={logs.error} />
      {logs.data && <pre className="logs">{logs.data.lines || "Строк нет"}</pre>}
    </Modal>
  );
}
