import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import "../styles/tools.css";
import { APPROVE_NOTE, BulkNodeActions, SPLIT_WARNING } from "../components/BulkNodeActions";
import { CopyButton } from "../components/CopyButton";
import { Confirm } from "../components/Modal";
import { NodeDiskWarning } from "../components/NodeDiskWarning";
import { NodeWake } from "../components/NodeWake";
import { BulkUpdateNodes, NodeUpdateButton, NodeVersionTag, useNodeVersions } from "../components/NodeVersions";
import { NodeDeletedDialog, NodeDeleteMenu } from "../components/NodeDelete";
import { NodePurge } from "../components/PurgeModels";
import { ErrorNotice, Loading, Notice, PageHead } from "../components/ui";
import { fmtBytes, fmtMib } from "../lib/format";
import { versionText } from "../lib/nodeVersions";
import { useResource } from "../lib/useResource";
import { hasClaims, waitsForApproval, type JoinInfo, type NodeDeleteResult, type NodeInfo } from "../types";
import { NodeClaimsDialog } from "../components/NodeClaims";

const JOIN_ROWS: { key: keyof JoinInfo & ("linux" | "linux_standalone" | "windows"); title: string; copy: string }[] = [
  { key: "linux", title: "Linux", copy: "Копировать команду Linux" },
  { key: "linux_standalone", title: "Linux, режим standalone", copy: "Копировать команду Linux standalone" },
  { key: "windows", title: "Windows", copy: "Копировать команду Windows" },
];

/** value of the option that stands for a deployment the list does not know (yet or any more) */
const UNKNOWN = "?";

export function AdminNodes() {
  const nodes = useResource(() => api.nodes(true), [], { interval: 10000 });
  /** the last samples of the nodes: the free space of the PC's disk */
  const live = useResource(() => api.metricsLive(), [], { interval: 10000 });
  const deployments = useResource(() => api.deployments(), [], { interval: 10000 });
  const join = useResource(() => api.join(), []);
  /** the image each node runs and whether it is outdated (portal task 2-30); the table does not wait for it */
  const versions = useNodeVersions();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  /** a node waiting for «Подтвердить»: the admin compares its name and address first */
  const [confirmApprove, setConfirmApprove] = useState<NodeInfo | null>(null);
  /** a known name somebody else registered: the admin compares the claims' addresses and picks one */
  const [claimsOf, setClaimsOf] = useState<NodeInfo | null>(null);
  /** a node of a split deployment waiting for the admin to confirm its maintenance */
  const [confirmSplit, setConfirmSplit] = useState<string | null>(null);
  /** the answer of «Удалить из кластера…»: the row is gone, the dialog with it stays */
  const [deleted, setDeleted] = useState<NodeDeleteResult | null>(null);
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

  async function approve(node: string) {
    setError(null);
    setPending(node);
    try {
      await api.approveNode(node);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Не удалось подтвердить ноду");
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
        lead="Машины, на которых работают модели. Новая нода ждёт подтверждения: до кнопки «Подтвердить» она не получает модель и ключи. Назначение ноды на развёртывание меняется прямо в таблице. Нода на обслуживании ничего не запускает и сохраняет назначение до возвращения в работу. Выключенный ПК класса портал будит по сети сам, кнопка «Разбудить» делает это сразу."
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
          <BulkUpdateNodes
            selected={picked.map((n) => n.name)}
            byName={versions.byName}
            onFinished={async (failed) => {
              setSelected(new Set(failed));
              await versions.reload();
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
                  <th className="num">Модели на диске</th>
                  <th>Состояние</th>
                  <th className="num">Экземпляры</th>
                  <th>Развёртывание</th>
                  <th>Версия</th>
                  <th>Обслуживание</th>
                  {/* no visible title; a .visually-hidden span here is positioned against the page and widens it */}
                  <th className="menu-cell" aria-label="Меню действий" />
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
                          <span>{n.is_main ? "главная" : n.source === "enrolled" ? "ещё не в кластере" : n.source}</span>
                          {n.ip ? ` · ${n.ip}` : ""}
                        </span>
                      </div>
                    </td>
                    <td>{n.accel}</td>
                    <td className="num">{fmtMib(n.vram_mib)}</td>
                    <td className="num">{fmtMib(n.mem_mib)}</td>
                    <td className="num">{n.capacity_bytes ? fmtBytes(n.capacity_bytes) : "—"}</td>
                    <td className="num muted">{typeof n.models_bytes === "number" ? fmtBytes(n.models_bytes) : "—"}</td>
                    <td>
                      {hasClaims(n) && (
                        <div className="stackcell">
                          <span className="tag warn small">
                            {n.approved === false && !waitsForApproval(n)
                              ? "Ждёт выбора заявки"
                              : "Попытка перерегистрации известного имени"}
                          </span>
                          <button type="button" className="btn sm" onClick={() => setClaimsOf(n)}>
                            Заявки: {n.claims!.length}
                          </button>
                        </div>
                      )}
                      {waitsForApproval(n) ? (
                        <div className="stackcell">
                          <span className="status">
                            <i className="dot starting" aria-hidden="true" />
                            <span>Ожидает подтверждения</span>
                          </span>
                          {n.claim_ip && <span className="muted small nowrap">с адреса {n.claim_ip}</span>}
                          <button
                            type="button"
                            className="btn sm"
                            disabled={pending === n.name}
                            onClick={() => setConfirmApprove(n)}
                          >
                            Подтвердить
                          </button>
                        </div>
                      ) : n.approved === false ? (
                        hasClaims(n) ? null : (
                          <span
                            className="status"
                            title="Контейнер ноды регистрируется сам при старте. Если этого не происходит, перезапустите ноду на ПК и посмотрите журнал контейнера"
                          >
                            <i className="dot starting" aria-hidden="true" />
                            <span>Ждёт регистрации ноды</span>
                          </span>
                        )
                      ) : (
                        <span className="status">
                          <i
                            className={`dot ${n.maintenance ? "starting" : n.ready ? "ready" : "down"}`}
                            aria-hidden="true"
                          />
                          <span>{n.maintenance ? "На обслуживании" : n.ready ? "Готова" : "Не в сети"}</span>
                        </span>
                      )}
                      {n.legacy && (
                        <span className="tag warn small" title="Старый образ ноды: обновите ноду, чтобы она получила свой ключ">
                          без ключа (старый образ)
                        </span>
                      )}
                      <NodeWake node={n} />
                      {!n.is_main && (
                        <NodeDiskWarning freeBytes={live.data?.nodes.find((l) => l.name === n.name)?.disk_free_bytes} />
                      )}
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
                    <td>
                      {n.is_main ? (
                        <span className="tag">{versions.data ? versionText(versions.data.main) : "—"}</span>
                      ) : (
                        <div className="stackcell">
                          <NodeVersionTag info={versions.byName[n.name]} />
                          <NodeUpdateButton node={n.name} info={versions.byName[n.name]} onDone={versions.reload} />
                        </div>
                      )}
                    </td>
                    <td className="actions">
                      {!n.is_main && (
                        <div className="node-actions">
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
                          <NodePurge node={n} onDone={() => nodes.reload()} />
                        </div>
                      )}
                    </td>
                    {/* the «⋯» menu has a column of its own: next to the two buttons it would add a line to every row */}
                    <td className="menu-cell">
                      {!n.is_main && (
                        <NodeDeleteMenu
                          node={n}
                          deployments={deps}
                          onReset={() => nodes.reload()}
                          onDeleted={async (result) => {
                            setDeleted(result);
                            setSelected((prev) => {
                              const next = new Set(prev);
                              next.delete(n.name);
                              return next;
                            });
                            await nodes.reload();
                          }}
                        />
                      )}
                    </td>
                  </tr>
                ))}
                {nodes.data.nodes.length === 0 && (
                  <tr>
                    <td colSpan={12} className="empty-row">
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
      {nodes.data && !deployments.loading && (
        <p className="hint">
          Модели на диске: файлы моделей и кеш тензоров на диске ноды. «Очистить модели» удаляет всё, что не нужно её развёртыванию.
          Нода без развёртывания очищается сама спустя несколько минут, после смены модели развёртывания лишнее удаляется сразу.
        </p>
      )}

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

      {confirmApprove && (
        <Confirm
          title={`Подтвердить ${confirmApprove.name}`}
          confirmLabel="Подтвердить"
          onCancel={() => setConfirmApprove(null)}
          onConfirm={() => {
            const node = confirmApprove.name;
            setConfirmApprove(null);
            void approve(node);
          }}
        >
          <p>
            Нода <span className="mono">{confirmApprove.name}</span> зарегистрировалась с адреса{" "}
            <span className="mono">{confirmApprove.claim_ip ?? "неизвестно"}</span>.
          </p>
          <p>{APPROVE_NOTE}</p>
        </Confirm>
      )}

      {claimsOf && (
        <NodeClaimsDialog
          node={(nodes.data?.nodes ?? []).find((x) => x.name === claimsOf.name) ?? claimsOf}
          onClose={() => setClaimsOf(null)}
          onDone={() => nodes.reload()}
        />
      )}

      {deleted && <NodeDeletedDialog result={deleted} onClose={() => setDeleted(null)} />}

      {/* Shown together with the table: appearing first, it would be pushed down when the table arrives. */}
      {((nodes.data && !deployments.loading) || nodes.error) && (
        <section className="section">
          <h2>Подключение ноды</h2>
          <p className="secondary">
            Выполните команду на машине, которую нужно добавить. Токен даёт право присоединять новые ноды к кластеру, не публикуйте
            его. Нода с новым именем появится в таблице со статусом «Ожидает подтверждения»: сверьте имя и адрес и нажмите
            «Подтвердить».
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
