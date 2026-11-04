import { RAM_PCT_MAX, RAM_PCT_MIN } from "../lib/rules";
import type { Accel } from "../types";

/** Split: whether the GPU nodes' RAM takes layers too (nvidia and any; cpu nodes always use it) and which share of it. */
export function NodeRamFields({
  idPrefix,
  accel,
  useNodeRam,
  onUseNodeRam,
  ramPct,
  onRamPct,
  error,
}: {
  idPrefix: string;
  accel: Accel;
  useNodeRam: boolean;
  onUseNodeRam: (v: boolean) => void;
  ramPct: string;
  onRamPct: (v: string) => void;
  error?: string;
}) {
  const ramUsed = accel !== "nvidia" || useNodeRam;
  return (
    <>
      {accel !== "cpu" && (
        <label className="check">
          <input type="checkbox" checked={useNodeRam} onChange={(e) => onUseNodeRam(e.target.checked)} />
          <span>Использовать RAM нод, когда не хватает видеопамяти</span>
        </label>
      )}
      {ramUsed && (
        <div className="field">
          <label htmlFor={`${idPrefix}-ram-pct`}>Доля RAM ноды, %</label>
          <input
            id={`${idPrefix}-ram-pct`}
            className="input"
            type="number"
            min={RAM_PCT_MIN}
            max={RAM_PCT_MAX}
            value={ramPct}
            aria-invalid={!!error}
            onChange={(e) => onRamPct(e.target.value)}
          />
          {error ? (
            <span className="error-text">{error}</span>
          ) : (
            <span className="hint">Сколько памяти ноды может занять модель, остальное остаётся системе</span>
          )}
        </div>
      )}
    </>
  );
}
