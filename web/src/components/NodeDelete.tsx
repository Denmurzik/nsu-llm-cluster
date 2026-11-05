import { KeyRound, Trash2 } from "lucide-react";
import { useState } from "react";
import { api, ApiError } from "../api";
import type { BusyDeployment, Deployment, NodeDeleteResult, NodeInfo } from "../types";
import { CopyButton } from "./CopyButton";
import { Confirm, Modal } from "./Modal";
import { RowMenu } from "./RowMenu";

/** what a removal does on main: the confirmation names it (task 2-31) */
const REMOVED = [
  "запись ноды в портале и объект ноды в Kubernetes;",
  "назначение на развёртывание;",
  "адрес сетевой карты для Wake-on-LAN: ПК больше не будится;",
  "сведения о занятом моделями месте, отчёты о версии и обновлении, текущие замеры.",
];

const KEPT = "История замеров и журнал действий остаются. Файлы моделей и сама нода на ПК не удаляются: их убирает команда, которую покажет следующее окно.";
const STILL_ONLINE = "ПК сейчас в сети: пока на нём установлена нода, она зарегистрируется снова. Её нужно удалить и на самом ПК.";

const modeSuffix = (d: BusyDeployment) => (d.mode === "split" ? " (split)" : "");

/** the deployments a node works for: what the server said about a refused removal, else what the table shows */
export function heldBy(node: NodeInfo, deployments: Deployment[], fromServer: BusyDeployment[] | null): BusyDeployment[] {
  if (fromServer && fromServer.length > 0) return fromServer;
  if (!node.deployment) return [];
  const known = deployments.find((d) => d.name === node.deployment);
  return [{ name: node.deployment, mode: known?.mode === "split" ? "split" : "replicas" }];
}

/** the 409 answer of a refused removal: the deployments it lists, or null when the answer is something else */
function busyOf(e: unknown): BusyDeployment[] | null {
  if (!(e instanceof ApiError) || e.status !== 409) return null;
  const list = e.data.deployments;
  if (!Array.isArray(list)) return null;
  const items = list.filter((d): d is BusyDeployment => !!d && typeof d === "object" && typeof (d as BusyDeployment).name === "string");
  return items.length > 0 ? items : null;
}

/** the part of a confirmation that says what is removed, what stays and, for a node with a deployment, what it costs */
export function DeleteNote({ nodes, held }: { nodes: NodeInfo[]; held: BusyDeployment[] }) {
  const split = held.some((d) => d.mode === "split");
  return (
    <>
      <p>Будет удалено на главном сервере:</p>
      <ul className="delete-list">
        {REMOVED.map((t) => (
          <li key={t}>{t}</li>
        ))}
      </ul>
      {held.length > 0 && (
        <p>
          <strong>Нода работает на развёртываниях: {held.map((d) => `${d.name}${modeSuffix(d)}`).join(", ")}.</strong> Они потеряют её и
          перепланируются на оставшихся нодах.
          {split && " Группа split будет остановлена и соберётся заново из резервных или оставшихся нод."}
        </p>
      )}
      {nodes.some((n) => n.ready) && <p>{STILL_ONLINE}</p>}
      <p>{KEPT}</p>
    </>
  );
}

/** one line for the result of a node in the bulk list */
export function deleteResultText(r: NodeDeleteResult): string {
  const parts = ["Удалена из кластера"];
  if (r.k8s === "unavailable") parts.push("объект Kubernetes не удалён: кластер не ответил, повторите позже");
  if (r.online) parts.push("ПК в сети: удалите ноду и на нём (install-node.ps1 -Uninstall или run-node.sh --uninstall)");
  return parts.join("; ");
}

/**
 * «Удалить из кластера…» in the «⋯» menu of a node row: one confirmation (the one for a node with a deployment names it
 * and asks again), one call. The page shows the result (`onDeleted`): the row is gone by then, and so would be a dialog
 * this component owned.
 */
export function NodeDeleteMenu({
  node,
  deployments,
  onDeleted,
  onReset,
}: {
  node: NodeInfo;
  deployments: Deployment[];
  onDeleted: (result: NodeDeleteResult) => void | Promise<void>;
  /** after «Сбросить ключ ноды»: refresh the table */
  onReset?: () => void | Promise<void>;
}) {
  const [asking, setAsking] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resetDone, setResetDone] = useState(false);
  const [refused, setRefused] = useState<BusyDeployment[] | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const held = heldBy(node, deployments, refused);

  function close() {
    setAsking(false);
    setResetting(false);
    setResetDone(false);
    setRefused(null);
    setError(null);
  }

  async function reset() {
    setPending(true);
    setError(null);
    try {
      await api.resetNodeKey(node.name);
      setResetDone(true);
      await onReset?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Не удалось сбросить ключ ноды");
    } finally {
      setPending(false);
    }
  }

  async function run() {
    setPending(true);
    setError(null);
    try {
      const result = await api.deleteNode(node.name, held.length > 0);
      close();
      await onDeleted(result);
    } catch (e) {
      const busy = busyOf(e);
      if (busy) setRefused(busy); // the server knows a deployment the table did not: ask again with it named
      else setError(e instanceof ApiError ? e.detail : "Не удалось удалить ноду");
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      <RowMenu
        label={`Действия: ${node.name}`}
        items={[
          ...(node.has_key || node.legacy || node.claims?.length
            ? [{ key: "reset", label: "Сбросить ключ ноды…", icon: KeyRound, onSelect: () => setResetting(true) }]
            : []),
          { key: "delete", label: "Удалить из кластера…", icon: Trash2, danger: true, onSelect: () => setAsking(true) },
        ]}
      />
      {resetting && !resetDone && (
        <Confirm
          title={`Сбросить ключ ${node.name}`}
          confirmLabel="Сбросить ключ"
          busy={pending}
          onCancel={close}
          onConfirm={() => void reset()}
        >
          <p>
            Ключ ноды перестанет действовать, нода отключится от шлюза и снимется с развёртывания. Она зарегистрируется заново
            агентским токеном и будет ждать подтверждения на этой странице. Сбрасывайте ключ, если машину переустановили или
            ключ мог утечь.
          </p>
          {error && (
            <p className="error-text" role="alert">
              {error}
            </p>
          )}
        </Confirm>
      )}
      {resetDone && (
        <Confirm title={`Ключ ${node.name} сброшен`} confirmLabel="Закрыть" onCancel={close} onConfirm={close}>
          <p>Когда нода зарегистрируется снова, подтвердите её кнопкой «Подтвердить» в строке ноды.</p>
        </Confirm>
      )}
      {asking && (
        <Confirm
          title={`Удалить ${node.name} из кластера`}
          confirmLabel={held.length > 0 ? "Всё равно удалить" : "Удалить"}
          busy={pending}
          onCancel={close}
          onConfirm={() => void run()}
        >
          <DeleteNote nodes={[node]} held={held} />
          {error && (
            <p className="error-text" role="alert">
              {error}
            </p>
          )}
        </Confirm>
      )}
    </>
  );
}

/** the result of a removal: what happened to the Kubernetes node, and, for a PC that may register again, how to uninstall it */
export function NodeDeletedDialog({ result, onClose }: { result: NodeDeleteResult; onClose: () => void }) {
  const commands: { key: "windows" | "linux"; title: string; copy: string }[] = [
    { key: "windows", title: "Windows (PowerShell, из каталога node)", copy: "Копировать команду удаления для Windows" },
    { key: "linux", title: "Linux", copy: "Копировать команду удаления для Linux" },
  ];
  return (
    <Modal
      title={`Нода ${result.node} удалена из кластера`}
      onClose={onClose}
      hideClose
      footer={
        <button type="button" className="btn primary" onClick={onClose} data-autofocus>
          Закрыть
        </button>
      }
    >
      {result.k8s === "unavailable" && (
        <p className="error-text" role="alert">
          Объект ноды в Kubernetes не удалён: кластер не ответил. Повторите удаление, когда он вернётся.
        </p>
      )}
      {result.unassigned.length > 0 && <p>Снята с развёртываний: {result.unassigned.join(", ")}.</p>}
      <p>
        {result.online ? (
          <>
            <strong>ПК ещё в сети.</strong> Установленная на нём нода зарегистрируется снова со следующим heartbeat.
          </>
        ) : (
          <>
            <strong>ПК не в сети.</strong> Если на нём осталась установленная нода, она зарегистрируется снова после включения.
          </>
        )}{" "}
        Чтобы убрать её совсем, выполните на ПК (токен не нужен):
      </p>
      {commands.map((c) => (
        <div key={c.key} className="field">
          <span className="label">{c.title}</span>
          <div className="code wrap">
            <pre data-testid={`uninstall-${c.key}`}>
              <code>{result.uninstall[c.key]}</code>
            </pre>
            <span className="copy">
              <CopyButton text={result.uninstall[c.key]} ariaLabel={c.copy} />
            </span>
          </div>
        </div>
      ))}
    </Modal>
  );
}
