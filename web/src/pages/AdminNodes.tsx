import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import "../styles/tools.css";
import { BulkNodeActions, SPLIT_WARNING } from "../components/BulkNodeActions";
import { CopyButton } from "../components/CopyButton";
import { Confirm } from "../components/Modal";
import { NodeWake } from "../components/NodeWake";
import { ErrorNotice, Loading, Notice, PageHead } from "../components/ui";
import { fmtBytes, fmtMib } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { JoinInfo } from "../types";

const JOIN_ROWS: { key: keyof JoinInfo & ("linux" | "linux_standalone" | "windows"); title: string; copy: string }[] = [
  { key: "linux", title: "Linux", copy: "Копировать команду Linux" },
  { key: "linux_standalone", title: "Linux, режим standalone", copy: "Копировать команду Linux standalone" },
  { key: "windows", title: "Windows", copy: "Копировать команду Windows" },
];

/** value of the option that stands for a deployment the list does not know (yet or any more) */
const UNKNOWN = "?";

export function AdminNodes() {
  const nodes = useResource(() => api.nodes(), [], { interval: 10000 });
  const deployments = useResource(() => api.deployments(), [], { interval: 10000 });
  const join = useResource(() => api.join(), []);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  /** a node of a split deployment waiting for the admin to confirm its maintenance */
  const [confirmSplit, setConfirmSplit] = useState<string | null>(null);
  /** names ticked for the bulk actions; the main node cannot be ticked */
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const allBox = useRef<HTMLInputElement>(null);

  const deps = deployments.data ?? [];
  const idByName = new Map(deps.map((d) => [d.name, d.id]));
  const splitNames = new Set(deps.filter((d) => d.mode === "split").map((d) => d.name));

  const selectable = (nodes.data?.nodes ?? []).filter((n) => !n.is_main);
  // a node that left the table drops out of the selection
  const picked = selectable.filter((n) => selected.has(n.name));
  const allPicked = selectable.length > 0 && picked.length === selectable.length;
  useEffect(() => {
    if (allBox.current) allBox.current.indeterminate = picked.length > 0 && !allPicked;
  }, [picked.length, allPicked]);

  const toggle = (name: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (!next.delete(name)) next.add(name);
      return next;
    });

  async function assign(node: string, value: string) {
    setError(null);
    setPending(node);
    try {
      await api.assignNode(node, value === "" ? null : Number(value));
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Не удалось назначить ноду");
    } finally {
      setPending(null);
      await nodes.reload();
    }
  }

  async function setMaintenance(node: string, on: boolean) {
    setError(null);
    setPending(node);
    try {
      await api.setMaintenance(node, on);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Не удалось изменить режим обслуживания");
    } finally {
      setPending(null);
      await nodes.reload();
    }
  }

  return (
    <div className="page">
      <PageHead
        title="Ноды"
        lead="Машины, на которых работают модели. Назначение ноды на развёртывание меняется прямо в таблице. Нода на обслуживании ничего не запускает и сохраняет назначение до возвращения в работу. Выключенный ПК класса портал будит по сети сам, кнопка «Разбудить» делает это сразу."
      />
      {nodes.data && !nodes.data.k8s_available && (
        <Notice kind="warn">Kubernetes недоступен, показаны только standalone-ноды</Notice>
      )}
      {error && <ErrorNotice error={error} />}
      <ErrorNotice error={nodes.error && !nodes.data ? nodes.error : null} />
      <ErrorNotice error={deployments.error} />
      {(nodes.loading && !nodes.data) || deployments.loading ? <Loading /> : null}

      {nodes.data && !deployments.loading && (
        <div className="tools-block">
          <BulkNodeActions
            selected={picked}
            deployments={deps}
            splitNames={splitNames}
            onFinished={async (failed) => {
              setSelected(new Set(failed));
              await nodes.reload();
            }}
          />
          <div className="table-wrap">
            <table className="data dense" aria-label="Ноды">
              <thead>
                <tr>
                  <th className="pick">
                    <input
                      ref={allBox}
                      type="checkbox"
                      aria-label="Выбрать все ноды"
                      checked={allPicked}
                      disabled={selectable.length === 0}
                      onChange={() => setSelected(allPicked ? new Set() : new Set(selectable.map((n) => n.name)))}
                    />
                  </th>
                  <th>Нода</th>
                  <th>Ускоритель</th>
                  <th className="num">VRAM</th>
                  <th className="num">RAM</th>
                  <th className="num">Ёмкость</th>
                  <th>Состояние</th>
                  <th className="num">Экземпляры</th>
                  <th>Развёртывание</th>
                  <th>Обслуживание</th>
                </tr>
              </thead>
              <tbody>
                {nodes.data.nodes.map((n) => (
                  <tr key={n.name}>
                    <td className="pick">
                      {!n.is_main && (
                        <input
                          type="checkbox"
                          aria-label={`Выбрать ${n.name}`}
                          checked={selected.has(n.name)}
                          onChange={() => toggle(n.name)}
                        />
                      )}
                    </td>
                    <td>
                      <div className="stackcell">
                        <span className="mono nowrap">{n.name}</span>
                        <span className="muted small nowrap">
                          <span>{n.is_main ? "главная" : n.source}</span>
                          {n.ip ? ` · ${n.ip}` : ""}
                        </span>
                      </div>
                    </td>
                    <td>{n.accel}</td>
                    <td className="num">{fmtMib(n.vram_mib)}</td>
                    <td className="num">{fmtMib(n.mem_mib)}</td>
                    <td className="num">{n.capacity_bytes ? fmtBytes(n.capacity_bytes) : "—"}</td>
                    <td>
                      <span className="status">
                        <i
                          className={`dot ${n.maintenance ? "starting" : n.ready ? "ready" : "down"}`}
                          aria-hidden="true"
                        />
                        <span>{n.maintenance ? "На обслуживании" : n.ready ? "Готова" : "Не в сети"}</span>
                      </span>
                      <NodeWake node={n} />
                    </td>
                    <td className="num">{n.backends}</td>
                    <td>
                      {n.is_main ? (
                        <span className="muted">Не для моделей</span>
                      ) : (
                        <select
                          className="select sm cell-select"
                          aria-label={`Развёртывание для ${n.name}`}
                          value={n.deployment ? String(idByName.get(n.deployment) ?? UNKNOWN) : ""}
                          disabled={pending === n.name}
                          onChange={(e) => void assign(n.name, e.target.value)}
                        >
                          <option value="">Не назначено</option>
                          {n.deployment && !idByName.has(n.deployment) && (
                            <option value={UNKNOWN} disabled>
                              {n.deployment}
                            </option>
                          )}
                          {deps.map((d) => (
                            <option key={d.id} value={String(d.id)}>
                              {d.name}
                            </option>
                          ))}
                        </select>
                      )}
                    </td>
                    <td className="actions">
                      {!n.is_main && (
                        <button
                          type="button"
                          className="btn quiet sm"
                          disabled={pending === n.name}
                          onClick={() =>
                            !n.maintenance && n.deployment && splitNames.has(n.deployment)
                              ? setConfirmSplit(n.name)
                              : void setMaintenance(n.name, !n.maintenance)
                          }
                        >
                          {n.maintenance ? "Вернуть в работу" : "На обслуживание"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {nodes.data.nodes.length === 0 && (
                  <tr>
                    <td colSpan={10} className="empty-row">
                      Нод пока нет
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {nodes.data && !deployments.loading && <p className="hint">Ёмкость: сколько памяти ноды остаётся под модель</p>}

      {confirmSplit && (
        <Confirm
          title={`Перевести ${confirmSplit} на обслуживание`}
          confirmLabel="На обслуживание"
          onCancel={() => setConfirmSplit(null)}
          onConfirm={() => {
            const node = confirmSplit;
            setConfirmSplit(null);
            void setMaintenance(node, true);
          }}
        >
          <p>{SPLIT_WARNING}</p>
        </Confirm>
      )}

      {/* Shown together with the table: appearing first, it would be pushed down when the table arrives. */}
      {((nodes.data && !deployments.loading) || nodes.error) && (
        <section className="section">
          <h2>Подключение ноды</h2>
          <p className="secondary">
            Выполните команду на машине, которую нужно добавить. Токен даёт право присоединять ноды к кластеру, не публикуйте его.
          </p>
          <ErrorNotice error={join.error} />
          {join.loading && !join.data && <Loading />}
          {join.data &&
            JOIN_ROWS.map((r) => (
              <div key={r.key} className="field">
                <span className="label">{r.title}</span>
                <div className="code wrap">
                  <pre data-testid={`join-${r.key}`}>
                    <code>{join.data![r.key]}</code>
                  </pre>
                  <span className="copy">
                    <CopyButton text={join.data![r.key]} ariaLabel={r.copy} />
                  </span>
                </div>
              </div>
            ))}
        </section>
      )}
    </div>
  );
}
