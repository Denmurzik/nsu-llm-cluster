import { api } from "../api";
import { ErrorNotice, Loading, Notice, PageHead } from "../components/ui";
import { NodeWake } from "../components/NodeWake";
import { StatusLabel, backendsText } from "../components/Status";
import { fmtBytes, fmtDateTime, fmtInt, fmtMib } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { AdminStatus as StatusData, StatusNode } from "../types";

const DAY_S = 24 * 3600;

/** 9 -> «9 с назад», 1260 -> «21 мин назад»; null when the node has never shown a sign of life. */
function ageText(seconds: number | null): string {
  if (seconds === null) return "нет данных";
  if (seconds < 60) return `${seconds} с назад`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`;
  if (seconds < DAY_S) return `${Math.floor(seconds / 3600)} ч назад`;
  return `${Math.floor(seconds / DAY_S)} сут назад`;
}

function NodeState({ node }: { node: StatusNode }) {
  const [cls, text] = node.maintenance
    ? ["starting", "На обслуживании"]
    : node.ready
      ? ["ready", "Готова"]
      : ["down", "Не в сети"];
  return (
    <span className="status">
      <i className={`dot ${cls}`} aria-hidden="true" />
      <span>{text}</span>
    </span>
  );
}

function Gateway({ gateway }: { gateway: StatusData["gateway"] }) {
  if (!gateway.available || !gateway.last_5m || !gateway.last_1h) {
    return <Notice kind="warn">Счётчики шлюза недоступны</Notice>;
  }
  const rows = [
    { code: "429", text: "отказ из-за перегрузки", key: "rate_limited" as const },
    { code: "5xx", text: "ошибка модели или шлюза", key: "server_errors" as const },
  ];
  return (
    <div className="table-wrap">
      <table className="data" aria-label="Отказы шлюза">
        <thead>
          <tr>
            <th>Ответ</th>
            <th className="num">За 5 минут</th>
            <th className="num">За час</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.code}>
              <td>
                <div className="stackcell">
                  <span className="mono">{r.code}</span>
                  <span className="muted small">{r.text}</span>
                </div>
              </td>
              <td className="num">{fmtInt(gateway.last_5m![r.key])}</td>
              <td className="num">{fmtInt(gateway.last_1h![r.key])}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Backups({ backups }: { backups: StatusData["backups"] }) {
  if (!backups.available) return <span>Каталог копий недоступен</span>;
  if (!backups.last_at) return <span>Копий пока нет</span>;
  const stale = backups.last_age_s !== null && backups.last_age_s > 1.5 * DAY_S;
  return (
    <div className="stackcell" data-testid="backup-summary">
      <span>
        {fmtDateTime(backups.last_at)}
        {backups.last_size_bytes !== null ? `, ${fmtBytes(backups.last_size_bytes)}` : ""}
      </span>
      <span className="muted small">Хранится наборов: {backups.count}</span>
      {stale && <Notice kind="warn">Последней копии больше полутора суток: проверьте задание postgres-backup</Notice>}
    </div>
  );
}

export function AdminStatus() {
  const status = useResource(() => api.adminStatus(), [], { interval: 10000 });
  const data = status.data;

  return (
    <div className="page">
      <PageHead
        title="Состояние"
        lead="Ноды, развёртывания и шлюз моделей. Страница обновляется каждые 10 секунд."
      />
      <ErrorNotice error={status.error && !data ? status.error : null} />
      {status.loading && !data && <Loading />}
      {status.error && data && (
        <Notice kind="warn">Не удалось обновить данные, показаны данные последней успешной загрузки: {status.error.message}</Notice>
      )}
      {data && !data.k8s_available && (
        <Notice kind="warn">Kubernetes недоступен, показаны только standalone-ноды</Notice>
      )}

      {data && (
        <>
          <section className="section">
            <h2>Ноды</h2>
            <div className="table-wrap">
              <table className="data" aria-label="Состояние нод">
                <thead>
                  <tr>
                    <th>Нода</th>
                    <th>Ускоритель</th>
                    <th className="num">VRAM</th>
                    <th className="num">RAM</th>
                    <th>Сигнал</th>
                    <th>Состояние</th>
                    <th>Развёртывание</th>
                  </tr>
                </thead>
                <tbody>
                  {data.nodes.map((n) => (
                    <tr key={n.name}>
                      <td>
                        <div className="stackcell">
                          <span className="mono">{n.name}</span>
                          <span className="muted small">{n.is_main ? "главная" : n.source}</span>
                        </div>
                      </td>
                      <td>{n.accel}</td>
                      <td className="num">{fmtMib(n.vram_mib)}</td>
                      <td className="num">{fmtMib(n.mem_mib)}</td>
                      <td className="muted">{ageText(n.heartbeat_age_s)}</td>
                      <td>
                        <NodeState node={n} />
                        <NodeWake node={n} />
                      </td>
                      <td>{n.is_main ? <span className="muted">Не используется для моделей</span> : (n.deployment ?? <span className="muted">Не назначено</span>)}</td>
                    </tr>
                  ))}
                  {data.nodes.length === 0 && (
                    <tr>
                      <td colSpan={7} className="empty-row">
                        Нод пока нет
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="section">
            <h2>Развёртывания</h2>
            <div className="table-wrap">
              <table className="data" aria-label="Состояние развёртываний">
                <thead>
                  <tr>
                    <th>Название</th>
                    <th>Режим</th>
                    <th>Состояние</th>
                    <th className="num">Готовые экземпляры</th>
                    <th className="num">Ноды</th>
                  </tr>
                </thead>
                <tbody>
                  {data.deployments.map((d) => (
                    <tr key={d.name}>
                      <td className="mono">{d.name}</td>
                      <td>{d.mode === "split" ? "Разделение" : "Реплики"}</td>
                      <td>
                        <StatusLabel status={d.status === "off" ? "unknown" : d.status} off={d.status === "off"} />
                      </td>
                      <td className="num">{d.status === "off" ? "—" : backendsText(d.ready_backends)}</td>
                      <td className="num">{d.assigned_nodes}</td>
                    </tr>
                  ))}
                  {data.deployments.length === 0 && (
                    <tr>
                      <td colSpan={5} className="empty-row">
                        Развёртываний пока нет
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="section">
            <h2>Шлюз</h2>
            <Gateway gateway={data.gateway} />
          </section>

          <section className="section">
            <h2>Главный сервер</h2>
            <div className="table-wrap">
              <table className="data" aria-label="Главный сервер">
                <tbody>
                  <tr>
                    <th scope="row">GPU</th>
                    <td>{data.main.gpu ?? "Не обнаружена"}</td>
                  </tr>
                  <tr>
                    <th scope="row">Резервная копия Postgres</th>
                    <td>
                      <Backups backups={data.backups} />
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p className="secondary">
              В разделение моделей главный сервер не входит, даже если на нём есть GPU.
            </p>
          </section>
        </>
      )}
    </div>
  );
}
