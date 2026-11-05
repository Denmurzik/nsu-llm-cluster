import { Download } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { UsageChart } from "../components/UsageChart";
import { UsageOwner } from "../components/UsageOwner";
import { ErrorNotice, Ic, Loading, PageHead } from "../components/ui";
import { fmtInt } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { AdminUsage as AdminUsageData, UsageGroup, UsageItem } from "../types";
import "../styles/admin-ops.css";

const PRESETS = [
  { days: 7, label: "7 дней" },
  { days: 30, label: "30 дней" },
  { days: 90, label: "90 дней" },
];
const GROUPS: { id: UsageGroup; label: string; head: string }[] = [
  { id: "user", label: "По пользователям", head: "Пользователь" },
  { id: "model", label: "По моделям", head: "Модель" },
  { id: "day", label: "По дням", head: "День" },
];
const MAX_DAYS = 366;
const DAY_MS = 86_400_000;

/** LiteLLM keeps its days in UTC, so the period is counted in UTC days as well. */
const isoDay = (d: Date) => d.toISOString().slice(0, 10);
function lastDays(days: number): { from: string; to: string } {
  const to = new Date();
  return { from: isoDay(new Date(to.getTime() - (days - 1) * DAY_MS)), to: isoDay(to) };
}
const spanDays = (from: string, to: string) => Math.round((Date.parse(to) - Date.parse(from)) / DAY_MS) + 1;

function periodError(from: string, to: string): string | null {
  if (!from || !to) return "Укажите начало и конец периода";
  if (from > to) return "Начало периода позже его конца";
  if (spanDays(from, to) > MAX_DAYS) return `Период не длиннее ${MAX_DAYS} дней`;
  return null;
}

export function AdminUsage() {
  const [period, setPeriod] = useState(() => lastDays(7));
  const [group, setGroup] = useState<UsageGroup>("user");
  const invalid = periodError(period.from, period.to);
  const usage = useResource(
    () => (invalid ? Promise.resolve(undefined) : api.adminUsage({ ...period, group })),
    [period.from, period.to, group],
  );
  const data = usage.data;
  const csv = `/api/admin/usage.csv?${new URLSearchParams({ ...period, group })}`;
  const today = isoDay(new Date());

  return (
    <div className="page">
      <PageHead
        title="Использование"
        lead="Токены и запросы всех пользователей по данным шлюза. Дни считаются по UTC."
        actions={
          invalid ? undefined : (
            <a className="btn" href={csv} download>
              <Ic icon={Download} />
              Скачать CSV
            </a>
          )
        }
      />
      <UsageOwner />

      <div className="range">
        <div className="tabs" role="tablist" aria-label="Период">
          {PRESETS.map((p) => (
            <button
              key={p.days}
              type="button"
              role="tab"
              className="tab"
              aria-selected={period.to === today && spanDays(period.from, period.to) === p.days}
              onClick={() => setPeriod(lastDays(p.days))}
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="field">
          <label htmlFor="usage-from">С</label>
          <input
            id="usage-from"
            className="input"
            type="date"
            value={period.from}
            max={period.to || undefined}
            onChange={(e) => setPeriod({ ...period, from: e.target.value })}
          />
        </div>
        <div className="field">
          <label htmlFor="usage-to">По</label>
          <input
            id="usage-to"
            className="input"
            type="date"
            value={period.to}
            min={period.from || undefined}
            onChange={(e) => setPeriod({ ...period, to: e.target.value })}
          />
        </div>
      </div>
      {invalid && <p className="error-text">{invalid}</p>}

      <ErrorNotice error={usage.error} />
      {!invalid && usage.loading && !data && <Loading />}
      {!invalid && data && <Report data={data} group={group} onGroup={setGroup} />}
    </div>
  );
}

function Report({ data, group, onGroup }: { data: AdminUsageData; group: UsageGroup; onGroup: (g: UsageGroup) => void }) {
  const t = data.totals;
  const head = GROUPS.find((g) => g.id === group)?.head ?? "";
  return (
    <>
      <div className="stats">
        <div className="stat">
          <span className="muted">Токены запроса</span>
          <span className="figure">{fmtInt(t.prompt_tokens)}</span>
        </div>
        <div className="stat">
          <span className="muted">Токены ответа</span>
          <span className="figure">{fmtInt(t.completion_tokens)}</span>
        </div>
        <div className="stat">
          <span className="muted">Запросы</span>
          <span className="figure">{fmtInt(t.requests)}</span>
        </div>
      </div>

      {t.requests === 0 ? (
        <p className="muted">За выбранный период запросов не было</p>
      ) : (
        <>
          <section className="section">
            <h2>Токены по дням</h2>
            <UsageChart days={data.by_day} />
          </section>

          <div className="tops">
            <TopList title="Топ пользователей" items={data.top_users} />
            <TopList title="Топ моделей" items={data.top_models} />
          </div>

          <section className="section">
            <header>
              <h2>Подробно</h2>
              <div className="tabs" role="tablist" aria-label="Группировка">
                {GROUPS.map((g) => (
                  <button
                    key={g.id}
                    type="button"
                    role="tab"
                    className="tab"
                    aria-selected={group === g.id}
                    onClick={() => onGroup(g.id)}
                  >
                    {g.label}
                  </button>
                ))}
              </div>
            </header>
            <div className="table-wrap">
              <table className="data" aria-label="Использование по группам">
                <thead>
                  <tr>
                    <th>{head}</th>
                    <th className="num">Токены запроса</th>
                    <th className="num">Токены ответа</th>
                    <th className="num">Всего токенов</th>
                    <th className="num">Запросы</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((i) => (
                    <tr key={i.key}>
                      <td>
                        <div className="stackcell">
                          <span className={group === "user" ? undefined : "mono"}>{i.label}</span>
                          {group === "user" && i.email && i.email !== i.label && (
                            <span className="muted small">{i.email}</span>
                          )}
                        </div>
                      </td>
                      <td className="num">{fmtInt(i.prompt_tokens)}</td>
                      <td className="num">{fmtInt(i.completion_tokens)}</td>
                      <td className="num">{fmtInt(i.tokens)}</td>
                      <td className="num">{fmtInt(i.requests)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </>
  );
}

function TopList({ title, items }: { title: string; items: UsageItem[] }) {
  const max = Math.max(1, ...items.map((i) => i.tokens));
  return (
    <section className="section">
      <h2>{title}</h2>
      <div className="card">
        <ol className="top-list" aria-label={title}>
          {items.map((i) => (
            <li key={i.key}>
              <span className="name" title={i.email && i.email !== i.label ? `${i.label}, ${i.email}` : i.label}>
                {i.label}
              </span>
              <span className="mono">{fmtInt(i.tokens)}</span>
              <span className="meter" aria-hidden="true">
                <i style={{ width: `${(i.tokens / max) * 100}%` }} />
              </span>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
