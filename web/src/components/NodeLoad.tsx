import { fmtMib, fmtPercent } from "../lib/format";
import type { LiveNode, StatusNode } from "../types";
import { Notice } from "./ui";
import "../styles/monitoring.css";

const NBSP = " ";
/** a bar turns to the "bad" colour above this share (temperature: °C) */
const OVER = { gpu: 98, vram: 95, ram: 92, temp: 85 };

function Bar({ label, pct, text, over }: { label: string; pct: number | null; text: string; over: boolean }) {
  const width = pct === null ? 0 : Math.min(100, Math.max(0, pct));
  return (
    <div className="load-cell">
      <span
        className={`meter${over ? " over" : ""}`}
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct === null ? undefined : Math.round(width)}
        aria-valuetext={pct === null ? "нет данных" : text}
      >
        <i style={{ width: `${width}%` }} />
      </span>
      <span className="val">{text}</span>
    </div>
  );
}

const share = (used: number | null | undefined, total: number | null | undefined): number | null =>
  used === null || used === undefined || !total ? null : (used / total) * 100;

function age(seconds: number | null): string {
  if (seconds === null) return "нет данных";
  if (seconds < 60) return `${seconds} с назад`;
  return `${Math.floor(seconds / 60)} мин назад`;
}

function processes(n: LiveNode): string {
  const up = [n.llama_up && "llama-server", n.rpc_up && "rpc-сервер"].filter(Boolean);
  return up.length ? `${up.join(", ")} запущен` : "процессы модели не запущены";
}

function Cells({ node, label }: { node: LiveNode | undefined; label: string }) {
  const n = node;
  const gpu = n?.gpu_util ?? null;
  const vram = share(n?.vram_used_mib, n?.vram_total_mib);
  const ram = n ? share(n.ram_used_mib, n.ram_total_mib) : null;
  const temp = n?.gpu_temp_c ?? null;
  const noGpu = n !== undefined && n.gpu_count === 0;
  return (
    <>
      <td className="load">
        <Bar label={`GPU ${label}`} pct={gpu} text={gpu === null ? "—" : fmtPercent(gpu)} over={(gpu ?? 0) >= OVER.gpu} />
        <span className="load-sub">{noGpu ? "нет GPU" : n?.power_w != null ? `${Math.round(n.power_w)}${NBSP}Вт` : NBSP}</span>
      </td>
      <td className="load">
        <Bar label={`VRAM ${label}`} pct={vram} text={vram === null ? "—" : fmtPercent(vram)} over={(vram ?? 0) >= OVER.vram} />
        <span className="load-sub">
          {n && n.vram_total_mib ? `${fmtMib(n.vram_used_mib)} из ${fmtMib(n.vram_total_mib)}` : NBSP}
        </span>
      </td>
      <td className="load">
        <Bar label={`RAM ${label}`} pct={ram} text={ram === null ? "—" : fmtPercent(ram)} over={(ram ?? 0) >= OVER.ram} />
        <span className="load-sub">{n ? `${fmtMib(n.ram_used_mib)} из ${fmtMib(n.ram_total_mib)}` : NBSP}</span>
      </td>
      <td className="load">
        <Bar
          label={`Температура ${label}`}
          pct={temp}
          text={temp === null ? "—" : `${Math.round(temp)}${NBSP}°C`}
          over={(temp ?? 0) >= OVER.temp}
        />
        <span className="load-sub">{n ? `CPU ${fmtPercent(n.cpu_pct)}` : NBSP}</span>
      </td>
    </>
  );
}

/**
 * The live load of the nodes: one row per node of the status table, bars for GPU, VRAM, RAM and temperature. The rows
 * and the bars are there from the first paint (empty until the samples arrive), so the page does not move.
 */
export function NodeLoad({
  nodes,
  live,
  failed,
}: {
  nodes: StatusNode[];
  live: { nodes: LiveNode[]; available: boolean } | undefined;
  failed: boolean;
}) {
  const byName = new Map((live?.nodes ?? []).map((n) => [n.name, n]));
  const rows = nodes.filter((n) => !n.is_main);
  return (
    <>
      {failed && !live && <Notice kind="warn">Замеры нагрузки недоступны</Notice>}
      {live && !live.available && <Notice kind="warn">Хранилище замеров недоступно, нагрузка нод не показана</Notice>}
      <div className="table-wrap">
        <table className="data" aria-label="Нагрузка нод">
          <thead>
            <tr>
              <th>Нода</th>
              <th>GPU</th>
              <th>VRAM</th>
              <th>RAM</th>
              <th>Температура</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((n) => {
              const l = byName.get(n.name);
              return (
                <tr key={n.name}>
                  <td className="load">
                    <div className="stackcell">
                      <span className="mono">{n.name}</span>
                      <span className="muted small">{l ? `${processes(l)}, замер ${age(l.age_s)}` : live ? "нет замеров" : NBSP}</span>
                    </div>
                  </td>
                  <Cells node={l} label={n.name} />
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={5} className="empty-row">
                  Нод пока нет
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="hint">Ноды присылают замеры каждые 20 секунд, страница обновляет их каждые 10 секунд.</p>
    </>
  );
}
