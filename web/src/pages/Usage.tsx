import { useState } from "react";
import { api } from "../api";
import { UsageChart } from "../components/UsageChart";
import { UsageOwner } from "../components/UsageOwner";
import { ErrorNotice, Loading, PageHead } from "../components/ui";
import { fmtInt } from "../lib/format";
import { useResource } from "../lib/useResource";

const PERIODS = [
  { days: 1, label: "День" },
  { days: 7, label: "7 дней" },
  { days: 30, label: "30 дней" },
];

export function Usage() {
  const [days, setDays] = useState(7);
  const usage = useResource(() => api.usage(days), [days]);
  const data = usage.data;

  return (
    <div className="page">
      <PageHead
        title="Использование"
        lead="Токены и запросы по всем вашим ключам, включая playground."
        actions={
          <div className="tabs" role="tablist" aria-label="Период">
            {PERIODS.map((p) => (
              <button
                key={p.days}
                type="button"
                role="tab"
                className="tab"
                aria-selected={days === p.days}
                onClick={() => setDays(p.days)}
              >
                {p.label}
              </button>
            ))}
          </div>
        }
      />
      <UsageOwner />
      <ErrorNotice error={usage.error} />
      {usage.loading && !data && <Loading />}
      {data && (
        <>
          <div className="stats">
            <div className="stat">
              <span className="muted">Токены запроса</span>
              <span className="figure">{fmtInt(data.prompt_tokens)}</span>
            </div>
            <div className="stat">
              <span className="muted">Токены ответа</span>
              <span className="figure">{fmtInt(data.completion_tokens)}</span>
            </div>
            <div className="stat">
              <span className="muted">Запросы</span>
              <span className="figure">{fmtInt(data.requests)}</span>
            </div>
          </div>
          <section className="section">
            <h2>Токены по дням</h2>
            {data.by_day.length === 0 || data.requests === 0 ? (
              <p className="muted">За выбранный период запросов не было</p>
            ) : (
              <UsageChart days={data.by_day} />
            )}
          </section>
        </>
      )}
    </div>
  );
}
