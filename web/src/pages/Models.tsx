import { api } from "../api";
import { Cells, StatusLabel } from "../components/Status";
import { ErrorNotice, Loading, PageHead } from "../components/ui";
import { fmtInt } from "../lib/format";
import { ENGINE_LABEL, modeLabel } from "../lib/labels";
import { useResource } from "../lib/useResource";

export function Models() {
  const models = useResource(() => api.models(), [], { interval: 15000 });

  return (
    <div className="page">
      <PageHead
        title="Модели"
        lead="Модели работают на машинах аудитории. Один ключ действует для всех моделей из списка."
      />
      <ErrorNotice error={models.error} />
      {models.loading && !models.data && <Loading />}
      {models.data && (
        <div className="table-wrap">
          <table className="data" aria-label="Каталог моделей">
            <thead>
              <tr>
                <th>Модель</th>
                <th>Статус</th>
                <th className="num">Контекст на запрос</th>
                <th className="num">Параллельных запросов</th>
                <th>Режим</th>
                <th>Движок</th>
              </tr>
            </thead>
            <tbody>
              {models.data.map((m) => (
                <tr key={m.name}>
                  <td>
                    <span className="mono">{m.name}</span>
                    {m.experimental && (
                      <>
                        {" "}
                        <span className="tag" title="Эта модель видна и доступна только администраторам">
                          экспериментальная
                        </span>
                      </>
                    )}
                  </td>
                  <td>
                    <span className="status">
                      <StatusLabel status={m.status} />
                      <Cells ready={m.ready_backends} status={m.status} />
                    </span>
                  </td>
                  <td className="num">
                    <span>{fmtInt(m.ctx_per_request)}</span> <span className="muted small">токенов</span>
                  </td>
                  <td className="num">{m.parallel}</td>
                  <td>
                    <span>{modeLabel(m.mode, m.group_size)}</span>
                  </td>
                  <td className="muted">{ENGINE_LABEL[m.engine]}</td>
                </tr>
              ))}
              {models.data.length === 0 && (
                <tr>
                  <td colSpan={6} className="empty-row">
                    Моделей пока нет
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
