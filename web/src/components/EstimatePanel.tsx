import { Check, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError, type EstimateParams } from "../api";
import { fmtBytes } from "../lib/format";
import { useDebounced } from "../lib/useResource";
import type { Estimate, Placement } from "../types";
import { Ic, Notice } from "./ui";

const OVERHEAD = 512 * 2 ** 20;

export interface EstimateState {
  data: Estimate | null;
  error: ApiError | null;
  /** inputs changed and the answer for them has not arrived yet */
  pending: boolean;
}

/** Live estimate: waits for typing to settle, then asks the portal. */
export function useEstimate(params: EstimateParams | null, delay = 300): EstimateState {
  const key = params ? JSON.stringify(params) : null;
  const settledKey = useDebounced(key, delay);
  const [result, setResult] = useState<{ key: string | null; data: Estimate | null; error: ApiError | null }>({
    key: null,
    data: null,
    error: null,
  });

  useEffect(() => {
    if (!settledKey) {
      setResult({ key: null, data: null, error: null });
      return;
    }
    let cancelled = false;
    api
      .hfEstimate(JSON.parse(settledKey) as EstimateParams)
      .then((data) => !cancelled && setResult({ key: settledKey, data, error: null }))
      .catch((e) => !cancelled && setResult({ key: settledKey, data: null, error: e as ApiError }));
    return () => {
      cancelled = true;
    };
  }, [settledKey]);

  return {
    data: result.data,
    error: result.error,
    pending: key !== null && result.key !== key,
  };
}

/** Moment of the admin screens: weights, KV cache and the fit per node. */
export function EstimatePanel({
  state,
  groupSize,
  split = false,
  onApplyGroup,
  canApplyGroup,
}: {
  state: EstimateState;
  groupSize: number;
  /** split mode: the placement of the first group is shown */
  split?: boolean;
  onApplyGroup?: (size: number) => void;
  canApplyGroup: boolean;
}) {
  const { data, error, pending } = state;
  return (
    <section className={`card estimate${pending && data ? " stale" : ""}`} aria-label="Оценка памяти" aria-busy={pending}>
      <header>
        <h2>Оценка памяти</h2>
        <p className="hint">
          Веса плюс KV-кэш на весь контекст всех слотов, оценка сверху. При разделении draft-модель и проектор лежат на
          лидере
        </p>
      </header>

      {error && <p className="error-text">{error.detail}</p>}
      {!data && !error && <p className="muted">Идёт оценка памяти</p>}

      {data && (
        <Body data={data} groupSize={groupSize} split={split} onApplyGroup={onApplyGroup} canApplyGroup={canApplyGroup} />
      )}
    </section>
  );
}

function Body({
  data,
  groupSize,
  split,
  onApplyGroup,
  canApplyGroup,
}: {
  data: Estimate;
  groupSize: number;
  split: boolean;
  onApplyGroup?: (size: number) => void;
  canApplyGroup: boolean;
}) {
  const { weights_bytes, kv_bytes, total_bytes, per_node_bytes, device_bytes, host_bytes } = data.estimate;
  // parts besides the model: the draft model (weights and its own KV cache) and the image projector
  const draft = (data.estimate.draft_weights_bytes ?? 0) + (data.estimate.draft_kv_bytes ?? 0);
  const mmproj = data.estimate.mmproj_bytes ?? 0;
  const sum = weights_bytes + kv_bytes + draft + mmproj + OVERHEAD;
  const pct = (n: number) => `${(n / sum) * 100}%`;
  const rec = data.recommended_group_size;

  return (
    <>
      <div className="memstack" aria-hidden="true">
        <i className="w" style={{ width: pct(weights_bytes) }} />
        <i className="kv" style={{ width: pct(kv_bytes) }} />
        {draft > 0 && <i style={{ width: pct(draft), background: "var(--text-2)" }} />}
        {mmproj > 0 && <i style={{ width: pct(mmproj), background: "var(--line-strong)" }} />}
        <i className="ov" style={{ width: pct(OVERHEAD) }} />
      </div>
      <div className="legend">
        <div>
          <span className="k">
            <i className="sw" style={{ background: "var(--text-3)" }} />
            Веса
          </span>
          <span className="v">{fmtBytes(weights_bytes)}</span>
        </div>
        <div>
          <span className="k">
            <i className="sw" style={{ background: "var(--accent)" }} />
            KV-кэш
          </span>
          <span className="v">{fmtBytes(kv_bytes)}</span>
        </div>
        {draft > 0 && (
          <div>
            <span className="k">
              <i className="sw" style={{ background: "var(--text-2)" }} />
              Draft с KV-кэшем
            </span>
            <span className="v">{fmtBytes(draft)}</span>
          </div>
        )}
        {mmproj > 0 && (
          <div>
            <span className="k">
              <i className="sw" style={{ background: "var(--line-strong)" }} />
              Проектор изображений
            </span>
            <span className="v">{fmtBytes(mmproj)}</span>
          </div>
        )}
        <div>
          <span className="k">Всего</span>
          <span className="v">{fmtBytes(total_bytes)}</span>
        </div>
        <div>
          <span className="k">На одну ноду при группе {groupSize}</span>
          <span className="v">{fmtBytes(per_node_bytes)}</span>
        </div>
        {!split && device_bytes != null && host_bytes != null && (
          <div>
            <span className="k">На GPU-ноде: видеопамять, RAM (входные эмбеддинги)</span>
            <span className="v">
              {fmtBytes(device_bytes)}, {fmtBytes(host_bytes)}
            </span>
          </div>
        )}
      </div>

      {rec !== null ? (
        <p>
          <span>Рекомендуемый размер группы: {rec}</span>
          {canApplyGroup && rec !== groupSize && onApplyGroup && (
            <>
              {" "}
              <button type="button" className="btn quiet sm" onClick={() => onApplyGroup(Math.max(rec, 2))}>
                Применить
              </button>
            </>
          )}
        </p>
      ) : (
        <p className="error-text">Ни один набор нод не вмещает модель с такими параметрами</p>
      )}

      {data.nodes.length > 0 ? (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Нода</th>
                <th className="num">Ёмкость</th>
                <th aria-label="Заполнение" />
                <th>Результат</th>
              </tr>
            </thead>
            <tbody>
              {data.nodes.map((n) => {
                const ratio = n.capacity_bytes ? Math.min(1, (n.need_bytes ?? per_node_bytes) / n.capacity_bytes) : 1;
                return (
                  <tr key={n.name}>
                    <td className="mono nowrap">{n.name}</td>
                    <td className="num">{n.capacity_bytes ? fmtBytes(n.capacity_bytes) : "—"}</td>
                    <td>
                      <span className={`meter${n.fits ? "" : " over"}`} aria-hidden="true">
                        <i style={{ width: `${ratio * 100}%` }} />
                      </span>
                    </td>
                    <td>
                      <span className={`fits nowrap ${n.fits ? "yes" : "no"}`}>
                        <Ic icon={n.fits ? Check : X} style={{ color: n.fits ? "var(--ok)" : "var(--bad)" }} />
                        <span>{n.fits ? "Влезает" : "Не влезает"}</span>
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">Нод для сравнения пока нет</p>
      )}

      {data.placement ? (
        <PlacementTable placement={data.placement} />
      ) : (
        split && (
          <p className="muted">Размещение неизвестно: у нод нет меток памяти или их меньше, чем размер группы</p>
        )
      )}
    </>
  );
}

function used(bytes: number, budget: number | null) {
  return budget === null ? "—" : `${fmtBytes(bytes)} из ${fmtBytes(budget)}`;
}

/** Split: how the layers of the first group lie on the GPU and in the RAM of each node. */
function PlacementTable({ placement }: { placement: Placement }) {
  return (
    <div className="stack" aria-label="Размещение по нодам">
      <h3>Размещение первой группы</h3>
      <p className="hint">Оценка по текущим нодам; контроллер может собрать группы иначе</p>
      {!placement.fits && (
        <p className="error-text">Модель не помещается в память группы: увеличьте группу или долю RAM</p>
      )}
      {placement.speed_warning && (
        <Notice kind="warn">
          Часть слоёв ляжет в RAM нод ({fmtBytes(placement.ram_bytes)}). Их считает процессор, поэтому генерация будет
          в несколько раз медленнее, чем на одних видеокартах
        </Notice>
      )}
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Нода</th>
              <th className="num">Видеопамять</th>
              <th className="num">RAM</th>
              <th className="num">Слои GPU / RAM</th>
            </tr>
          </thead>
          <tbody>
            {placement.nodes.map((n) => (
              <tr key={n.name}>
                <td className="mono nowrap">
                  {n.name}
                  {n.leader && <span className="tag">лидер</span>}
                </td>
                <td className="num nowrap">{used(n.vram_bytes, n.vram_budget_bytes)}</td>
                <td className="num nowrap">{used(n.ram_bytes, n.ram_budget_bytes)}</td>
                <td className="num">
                  {n.vram_budget_bytes === null ? "—" : n.gpu_layers} / {n.ram_budget_bytes === null ? "—" : n.cpu_layers}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
