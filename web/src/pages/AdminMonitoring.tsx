import { useEffect, useState } from "react";
import { api } from "../api";
import { LineChart, type ChartSeries } from "../components/LineChart";
import { ErrorNotice, Loading, Notice, PageHead } from "../components/ui";
import { fmtMib, fmtPercent } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { MetricPoint, MetricsHistory, MetricsLive } from "../types";
import "../styles/monitoring.css";

const NBSP = " ";

const RANGES = [
  { id: "1h", label: "Час", ms: 3600_000 },
  { id: "24h", label: "24 часа", ms: 24 * 3600_000 },
  { id: "7d", label: "7 дней", ms: 7 * 24 * 3600_000 },
] as const;
type RangeId = (typeof RANGES)[number]["id"];

const numFmt = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const fmtNum = (v: number) => numFmt.format(v);
const fmtTemp = (v: number) => `${Math.round(v)}${NBSP}°C`;
const fmtMs = (v: number) => (v >= 1000 ? `${numFmt.format(v / 1000)}${NBSP}с` : `${Math.round(v)}${NBSP}мс`);

const hourFmt = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const dayHourFmt = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

type Field = keyof Omit<MetricPoint, "t">;
const column = (points: MetricPoint[], key: Field): (number | null)[] =>
  points.map((p) => {
    const v = p[key];
    return typeof v === "number" ? v : null;
  });

interface ChartSpec {
  title: string;
  series: { label: string; field: Field; tone?: ChartSeries["tone"] }[];
  format: (v: number) => string;
  /** a fixed top of the axis: a number, or the field whose largest value is the top (the total of VRAM or RAM) */
  yMax?: number | Field;
  /** a GPU chart: the node has the card from the first paint if it has a GPU, and has no card at all if it has none */
  gpu?: boolean;
}

const NODE_CHARTS: ChartSpec[] = [
  { title: "GPU, загрузка", series: [{ label: "GPU", field: "gpu_util" }], format: fmtPercent, yMax: 100, gpu: true },
  { title: "VRAM", series: [{ label: "занято", field: "vram_used_mib" }], format: fmtMib, yMax: "vram_total_mib", gpu: true },
  { title: "Оперативная память", series: [{ label: "занято", field: "ram_used_mib" }], format: fmtMib, yMax: "ram_total_mib" },
  { title: "Температура GPU", series: [{ label: "GPU", field: "gpu_temp_c" }], format: fmtTemp, yMax: 100, gpu: true },
  { title: "CPU, загрузка", series: [{ label: "CPU", field: "cpu_pct" }], format: fmtPercent, yMax: 100 },
];

const MODEL_CHARTS: ChartSpec[] = [
  { title: "Запросы в минуту", series: [{ label: "запросы", field: "rpm" }], format: fmtNum },
  { title: "Генерация, токенов в секунду", series: [{ label: "токены", field: "tok_s_gen" }], format: fmtNum },
  { title: "Обработка запроса, токенов в секунду", series: [{ label: "токены", field: "tok_s_prompt" }], format: fmtNum },
  {
    title: "Время до первого токена",
    series: [
      { label: "медиана", field: "ttft_p50_ms" },
      { label: "95-й процентиль", field: "ttft_p95_ms", tone: "muted" },
    ],
    format: fmtMs,
  },
  {
    title: "Очередь и занятые слоты",
    series: [
      { label: "очередь", field: "queue" },
      { label: "слоты", field: "slots_busy", tone: "muted" },
    ],
    format: fmtNum,
  },
  {
    title: "Ошибки в минуту",
    series: [
      { label: "5xx", field: "e5xx", tone: "bad" },
      { label: "429", field: "e429", tone: "warn" },
    ],
    format: fmtNum,
  },
];

/** Whether a node has GPUs, as last seen: the card set of a node must not change once it is drawn. */
const gpuSeen = new Map<string, boolean>();

/** From what is known before the history arrives: the live sample of the node, else what an earlier visit saw, else yes
 *  (a GPU card without data says «Нет данных за период» and never moves the grid). */
function nodeHasGpu(name: string, live: MetricsLive | undefined): boolean {
  const node = live?.nodes.find((n) => n.name === name);
  if (node) return node.gpu_count > 0;
  return gpuSeen.get(name) ?? true;
}

/** `key` the page remounts this per kind/name, so the card set is decided once, from the live sample, at the first render. */
function Charts({ kind, name, range, live }: { kind: "node" | "model"; name: string; range: RangeId; live?: MetricsLive }) {
  const span = RANGES.find((r) => r.id === range)!.ms;
  const [hasGpu] = useState(() => kind === "node" && nodeHasGpu(name, live));
  const hist = useResource<{ range: RangeId; history: MetricsHistory }>(
    async () => {
      const to = new Date();
      return { range, history: await api.metricsHistory(kind, name, new Date(to.getTime() - span), to) };
    },
    [kind, name, range],
    { interval: 60_000 },
  );
  // the answer of another period (the tab has just moved) is not shown: the cards say «Загрузка» until the right one comes
  const data =
    hist.data && hist.data.range === range && hist.data.history.name === name && hist.data.history.kind === kind
      ? hist.data.history
      : undefined;
  const loading = !data && (hist.loading || !hist.error);
  useEffect(() => {
    if (kind === "node" && data && data.points.length > 0) {
      gpuSeen.set(name, data.points.some((p) => typeof p.gpu_util === "number" || typeof p.vram_total_mib === "number"));
    }
  }, [kind, name, data]);

  const to = data ? Date.parse(data.to) : Date.now();
  const from = data ? Date.parse(data.from) : to - span;
  const points = data?.points ?? [];
  const times = points.map((p) => Date.parse(p.t));
  const formatTime = (ms: number) => (range === "7d" ? dayHourFmt : hourFmt).format(new Date(ms));
  // two missed minutes (a restart, a node that was off) break the line
  const gapMs = Math.max(data?.step ?? 60, 60) * 2500;

  const specs = (kind === "node" ? NODE_CHARTS : MODEL_CHARTS).filter((c) => !c.gpu || hasGpu);
  const top = (c: ChartSpec): number | undefined => {
    if (typeof c.yMax === "number" || c.yMax === undefined) return c.yMax;
    const totals = column(points, c.yMax).filter((v): v is number => v !== null);
    return totals.length ? Math.max(...totals) : undefined;
  };

  return (
    <>
      <ErrorNotice error={hist.error && !data ? hist.error : null} />
      {hist.error && data && <Notice kind="warn">Не удалось обновить графики, показаны данные последней загрузки: {hist.error.message}</Notice>}
      <div className="chart-grid" aria-busy={loading}>
        {specs.map((c) => (
          <LineChart
            key={c.title}
            title={c.title}
            times={times}
            from={from}
            to={to}
            yMax={top(c)}
            series={c.series.map((s) => ({ label: s.label, tone: s.tone, values: column(points, s.field) }))}
            format={c.format}
            formatTime={formatTime}
            gapMs={gapMs}
            emptyText={loading ? "Загрузка" : undefined}
          />
        ))}
      </div>
    </>
  );
}

function Target({
  kind,
  label,
  names,
  range,
  live,
}: {
  kind: "node" | "model";
  label: string;
  names: string[];
  range: RangeId;
  live?: MetricsLive;
}) {
  const [chosen, setChosen] = useState<string>("");
  // the first name until the admin picks one; a name that has left the list falls back to the first
  const name = names.includes(chosen) ? chosen : (names[0] ?? "");
  return (
    <section className="section" aria-label={kind === "node" ? "Графики ноды" : "Графики развёртывания"}>
      <header>
        <h2>{kind === "node" ? "Ноды" : "Развёртывания"}</h2>
        {names.length > 0 && (
          <select className="select sm" aria-label={label} value={name} onChange={(e) => setChosen(e.target.value)}>
            {names.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        )}
      </header>
      {names.length === 0 ? (
        <Notice>
          {kind === "node"
            ? "Замеров нод пока нет: они появляются через минуту после подключения ноды"
            : "Замеров развёртываний пока нет: они появляются через минуту после запуска модели"}
        </Notice>
      ) : (
        <Charts key={`${kind}/${name}`} kind={kind} name={name} range={range} live={live} />
      )}
    </section>
  );
}

export function AdminMonitoring() {
  const names = useResource(() => api.metricsNames(), [], { interval: 60_000 });
  // read once, before the first chart: it decides which cards a node has (GPU or not); a failure just means «unknown»
  const live = useResource(() => api.metricsLive(), []);
  const [range, setRange] = useState<RangeId>("1h");

  return (
    <div className="page">
      <PageHead
        title="Мониторинг"
        lead="Нагрузка нод и работа моделей за час, сутки и неделю. Точка на графике это одна минута или среднее за несколько минут; для ошибок, очереди и температуры показана наибольшая."
        actions={
          <div className="tabs" role="tablist" aria-label="Период">
            {RANGES.map((r) => (
              <button
                key={r.id}
                type="button"
                role="tab"
                className="tab"
                aria-selected={range === r.id}
                onClick={() => setRange(r.id)}
              >
                {r.label}
              </button>
            ))}
          </div>
        }
      />
      <ErrorNotice error={names.error && !names.data ? names.error : null} />
      {((names.loading && !names.data) || live.loading) && !names.error && <Loading />}
      {names.data && !live.loading && (
        <>
          <Target kind="node" label="Нода" names={names.data.node} range={range} live={live.data} />
          <Target kind="model" label="Развёртывание" names={names.data.model} range={range} live={live.data} />
        </>
      )}
    </div>
  );
}
