import { useState } from "react";
import { api } from "../api";
import { ErrorNotice, Loading, PageHead } from "../components/ui";
import { fmtDay, fmtInt } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { Usage as UsageData } from "../types";

const PERIODS = [
  { days: 1, label: "День" },
  { days: 7, label: "7 дней" },
  { days: 30, label: "30 дней" },
];

/** Round the top of the axis to 1, 2 or 5 times a power of ten. */
function niceMax(v: number): number {
  if (v <= 0) return 1;
  const pow = 10 ** Math.floor(Math.log10(v));
  const n = v / pow;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * pow;
}

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
              <Chart data={data} />
            )}
          </section>
        </>
      )}
    </div>
  );
}

function Chart({ data }: { data: UsageData }) {
  const max = niceMax(Math.max(...data.by_day.map((d) => d.tokens)));
  const ticks = [1, 0.75, 0.5, 0.25];
  const every = Math.ceil(data.by_day.length / 10);
  const last = data.by_day.length - 1;
  return (
    <div className="card">
      <div className="chart">
        <div className="yaxis" aria-hidden="true">
          {ticks.map((t) => (
            <span key={t} style={{ bottom: `${t * 100}%` }}>
              {fmtInt(Math.round(max * t))}
            </span>
          ))}
        </div>
        <div
          className="plot"
          role="img"
          aria-label={`Токены по дням, всего ${fmtInt(data.by_day.reduce((s, d) => s + d.tokens, 0))}`}
        >
          {ticks.map((t) => (
            <i key={t} className="gridline" style={{ bottom: `${t * 100}%` }} />
          ))}
          {data.by_day.map((d, i) => (
            <div
              key={d.day}
              data-testid="bar"
              className={`bar${i === last ? " current" : ""}`}
              style={{ ["--h" as string]: `${(d.tokens / max) * 100}%` }}
              title={`${fmtDay(d.day)}: ${fmtInt(d.tokens)} токенов, ${fmtInt(d.requests)} запросов`}
            />
          ))}
        </div>
        <div className="xaxis" aria-hidden="true">
          {data.by_day.map((d, i) => (
            <span key={d.day}>{i % every === 0 || i === last ? fmtDay(d.day) : ""}</span>
          ))}
        </div>
      </div>
    </div>
  );
}
