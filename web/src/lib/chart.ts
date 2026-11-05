/** Geometry of the monitoring line charts (components/LineChart.tsx). No chart library: a path per series. */

export const PLOT_W = 600;
export const PLOT_H = 140;

/** 63 -> 100, 7 -> 10, 0.4 -> 1, 380 -> 500: the top of the y axis, 1, 2, 5 or 10 times a power of ten. */
export function niceMax(value: number): number {
  if (!(value > 0)) return 1;
  const exp = Math.floor(Math.log10(value));
  const base = 10 ** exp;
  const m = value / base;
  const nice = m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10;
  return nice * base;
}

export interface Point {
  t: number;
  v: number | null;
}

/**
 * SVG path data for one series. A value is a point of the line unless it is null; the line breaks at a null and
 * where two points are further apart than `gapMs` (the node was silent). A point with no neighbour is drawn as a dot
 * (a zero-length segment with round caps). Returns "" when nothing can be drawn.
 */
export function linePath(points: Point[], from: number, to: number, yMax: number, gapMs: number): string {
  const span = Math.max(to - from, 1);
  const top = yMax > 0 ? yMax : 1;
  const x = (t: number) => ((t - from) / span) * PLOT_W;
  const y = (v: number) => PLOT_H - (Math.min(Math.max(v, 0), top) / top) * PLOT_H;
  const parts: string[] = [];
  let run: [number, number][] = [];
  const flush = () => {
    if (run.length === 1) parts.push(`M${run[0][0].toFixed(1)} ${run[0][1].toFixed(1)}h0`);
    else if (run.length > 1) parts.push("M" + run.map(([px, py]) => `${px.toFixed(1)} ${py.toFixed(1)}`).join("L"));
    run = [];
  };
  let lastT: number | null = null;
  for (const p of points) {
    if (p.v === null || !Number.isFinite(p.v) || p.t < from || p.t > to) {
      flush();
      lastT = null;
      continue;
    }
    if (lastT !== null && p.t - lastT > gapMs) flush();
    run.push([x(p.t), y(p.v)]);
    lastT = p.t;
  }
  flush();
  return parts.join("");
}

/** The index of the point nearest in time to `t`, or -1 without points. */
export function nearest(times: number[], t: number): number {
  let best = -1;
  let dist = Infinity;
  times.forEach((x, i) => {
    const d = Math.abs(x - t);
    if (d < dist) {
      dist = d;
      best = i;
    }
  });
  return best;
}
