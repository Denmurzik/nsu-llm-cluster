import { useRef, useState, type PointerEvent } from "react";
import { PLOT_H, PLOT_W, linePath, nearest, niceMax } from "../lib/chart";
import "../styles/monitoring.css";

export interface ChartSeries {
  label: string;
  /** one value per time, null where there is none */
  values: (number | null)[];
  /** the accent for the main series, `muted` for the second one; warn and bad only where the series means exactly that */
  tone?: "accent" | "muted" | "warn" | "bad";
}

interface Props {
  title: string;
  /** ms since the epoch, one per point */
  times: number[];
  series: ChartSeries[];
  /** the period the x axis covers, ms */
  from: number;
  to: number;
  /** a fixed top of the y axis; otherwise the data's maximum rounded up (1, 2, 5, 10) */
  yMax?: number;
  format: (v: number) => string;
  formatTime: (ms: number) => string;
  /** the line breaks where two points are further apart than this, ms */
  gapMs: number;
  /** what the empty plot says: «Загрузка» while the period loads, the default when it has no samples */
  emptyText?: string;
}

/** A line chart drawn by hand: muted grid, one accent, a readout of the nearest point on hover. */
export function LineChart({ title, times, series, from, to, yMax, format, formatTime, gapMs, emptyText = "Нет данных за период" }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const data = series.flatMap((s) => s.values).filter((v): v is number => v !== null && Number.isFinite(v));
  const top = yMax ?? niceMax(data.length ? Math.max(...data) : 0);

  const last = (s: ChartSeries) => {
    for (let i = s.values.length - 1; i >= 0; i--) if (s.values[i] !== null) return s.values[i];
    return null;
  };
  const at = hover !== null && hover < times.length ? hover : null;
  const shown = (s: ChartSeries) => (at !== null ? s.values[at] : last(s));

  const onMove = (e: PointerEvent) => {
    const rect = box.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || times.length === 0) return;
    const t = from + ((e.clientX - rect.left) / rect.width) * (to - from);
    setHover(nearest(times, t));
  };

  return (
    <figure className="mchart" aria-label={title}>
      <figcaption className={`mchart-head${series.length > 1 ? " two" : ""}`}>
        <span className="mchart-title">{title}</span>
        <span className="mchart-values mono" aria-live="off">
          {series.map((s) => {
            const v = shown(s);
            return (
              <span key={s.label} className="mchart-value">
                <i className={`sw ${s.tone ?? "accent"}`} aria-hidden="true" />
                <span className="muted">{s.label}</span> {v === null ? "—" : format(v)}
              </span>
            );
          })}
        </span>
      </figcaption>
      <div className="mchart-body">
        <div className="mchart-y mono" aria-hidden="true">
          <span>{format(top)}</span>
          <span>{format(top / 2)}</span>
          <span>{format(0)}</span>
        </div>
        <div className="mchart-plot" ref={box} onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
          <svg viewBox={`0 0 ${PLOT_W} ${PLOT_H}`} preserveAspectRatio="none" aria-hidden="true">
            {[0, 0.5, 1].map((f) => (
              <line key={f} className="grid" x1="0" x2={PLOT_W} y1={f * PLOT_H} y2={f * PLOT_H} vectorEffect="non-scaling-stroke" />
            ))}
            {series.map((s) => (
              <path
                key={s.label}
                className={`line ${s.tone ?? "accent"}`}
                d={linePath(
                  times.map((t, i) => ({ t, v: s.values[i] ?? null })),
                  from,
                  to,
                  top,
                  gapMs,
                )}
                vectorEffect="non-scaling-stroke"
              />
            ))}
            {at !== null && (
              <line
                className="cursor"
                x1={((times[at] - from) / Math.max(to - from, 1)) * PLOT_W}
                x2={((times[at] - from) / Math.max(to - from, 1)) * PLOT_W}
                y1="0"
                y2={PLOT_H}
                vectorEffect="non-scaling-stroke"
              />
            )}
          </svg>
          {times.length === 0 && <span className="mchart-empty muted">{emptyText}</span>}
          {/* the time of the point under the cursor floats over the plot, away from the cursor: it takes no room in the card */}
          {at !== null && (
            <span className={`mchart-time mono muted ${(times[at] - from) / Math.max(to - from, 1) > 0.5 ? "left" : "right"}`} aria-hidden="true">
              {formatTime(times[at])}
            </span>
          )}
        </div>
        <div className="mchart-x mono" aria-hidden="true">
          <span>{formatTime(from)}</span>
          <span>{formatTime((from + to) / 2)}</span>
          <span>{formatTime(to)}</span>
        </div>
      </div>
    </figure>
  );
}
