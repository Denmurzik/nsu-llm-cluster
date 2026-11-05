import { fmtDay, fmtInt } from "../lib/format";

export interface DayPoint {
  day: string;
  tokens: number;
  requests: number;
}

/** Round the top of the axis to 1, 2 or 5 times a power of ten. */
function niceMax(v: number): number {
  if (v <= 0) return 1;
  const pow = 10 ** Math.floor(Math.log10(v));
  const n = v / pow;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * pow;
}

/** Tokens per day as bars; the last day is the accent one. Shared by the user's and the admin «Использование». */
export function UsageChart({ days }: { days: DayPoint[] }) {
  const max = niceMax(Math.max(...days.map((d) => d.tokens)));
  const ticks = [1, 0.75, 0.5, 0.25];
  const every = Math.ceil(days.length / 10);
  const last = days.length - 1;
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
        <div className="plot" role="img" aria-label={`Токены по дням, всего ${fmtInt(days.reduce((s, d) => s + d.tokens, 0))}`}>
          {ticks.map((t) => (
            <i key={t} className="gridline" style={{ bottom: `${t * 100}%` }} />
          ))}
          {days.map((d, i) => (
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
          {days.map((d, i) => (
            <span key={d.day}>{i % every === 0 || i === last ? fmtDay(d.day) : ""}</span>
          ))}
        </div>
      </div>
    </div>
  );
}
