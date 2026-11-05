import { Check, CircleAlert } from "lucide-react";
import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { hasClaims, waitsForApproval, type Deployment, type NodeInfo } from "../types";
import { Confirm, Modal } from "./Modal";
import { DeleteNote, deleteResultText, heldBy } from "./NodeDelete";
import { heldText, PurgeNote, purgeResultText } from "./PurgeModels";
import { Ic } from "./ui";

/** what approving a node means, in the confirmation of «Подтвердить» */
export const APPROVE_NOTE =
  "Подтверждённая нода получит развёртывание, свой ключ бэкенда и попадёт в шлюз. Подтверждайте только машины, которые вы подключали сами: сверьте имя и адрес.";

/** the text of the confirmation when a node of a split group leaves its work */
export const SPLIT_WARNING =
  "Группа split будет остановлена: модель, разделённая между нодами группы, перестанет отвечать, пока группа не соберётся заново из резервных или оставшихся нод.";

type Action =
  | { kind: "assign"; deployment: Deployment | null }
  | { kind: "maintenance"; on: boolean }
  | { kind: "wake" }
  | { kind: "purge" }
  | { kind: "approve" }
  | { kind: "delete" };

type Outcome = "waiting" | "running" | "ok" | "error";
interface Item {
  node: string;
  outcome: Outcome;
  text: string;
}

export function nodesWord(n: number): string {
  const rest = n % 100;
  if (rest >= 11 && rest <= 14) return "нод";
  switch (n % 10) {
    case 1:
      return "нода";
    case 2:
    case 3:
    case 4:
      return "ноды";
    default:
      return "нод";
  }
}

const NONE = "none";

/** the deployments the nodes work for, each once, with its mode */
function heldUnion(nodes: NodeInfo[], deployments: Deployment[]) {
  const byName = new Map(nodes.flatMap((n) => heldBy(n, deployments, null)).map((d) => [d.name, d]));
  return [...byName.values()];
}

function titleOf(a: Action): string {
  if (a.kind === "assign") return a.deployment ? `Назначить развёртывание ${a.deployment.name}` : "Снять назначение";
  if (a.kind === "maintenance") return a.on ? "Перевести на обслуживание" : "Вернуть в работу";
  if (a.kind === "purge") return "Очистить модели";
  if (a.kind === "approve") return "Подтвердить ноды";
  if (a.kind === "delete") return "Удалить из кластера";
  return "Разбудить ноды";
}

const confirmLabelOf = (a: Action) =>
  a.kind === "assign"
    ? a.deployment
      ? "Назначить"
      : "Снять назначение"
    : a.kind === "wake"
      ? "Разбудить"
      : a.kind === "purge"
        ? "Очистить"
        : a.kind === "approve"
          ? "Подтвердить"
          : a.kind === "delete"
          ? "Удалить"
          : a.on
          ? "На обслуживание"
          : "Вернуть в работу";

/** the nodes an action is meaningful for: already settled ones are left out */
function targetsOf(a: Action, selected: NodeInfo[]): NodeInfo[] {
  if (a.kind === "maintenance") return selected.filter((n) => n.maintenance !== a.on);
  if (a.kind === "wake") return selected.filter((n) => !n.ready);
  if (a.kind === "purge") return selected.filter((n) => n.ready); // a node that is down cannot clean itself
  if (a.kind === "approve") return selected.filter((n) => waitsForApproval(n) && !hasClaims(n)); // claims: look at the row
  return selected;
}

function errorText(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.detail : fallback;
}

async function perform(a: Action, node: string, held: boolean): Promise<string> {
  if (a.kind === "delete") return deleteResultText(await api.deleteNode(node, held));
  if (a.kind === "assign") {
    await api.assignNode(node, a.deployment ? a.deployment.id : null);
    return a.deployment ? "Назначено" : "Назначение снято";
  }
  if (a.kind === "maintenance") {
    await api.setMaintenance(node, a.on);
    return a.on ? "На обслуживании" : "Возвращена в работу";
  }
  if (a.kind === "purge") return purgeResultText(await api.purgeNodeModels(node));
  if (a.kind === "approve") {
    await api.approveNode(node);
    return "Подтверждена";
  }
  const r = await api.wakeNode(node);
  return r.relay ? `Пакет отправлен, в том числе через ноду ${r.relay}` : "Пакет отправлен";
}

interface Props {
  /** the selected nodes (a node that is gone from the table is not here) */
  selected: NodeInfo[];
  deployments: Deployment[];
  splitNames: Set<string>;
  /** called once at the end of a run with the names that failed, to refresh the table and keep them selected */
  onFinished: (failed: string[]) => void | Promise<void>;
}

/**
 * The toolbar of the nodes table. Its space is reserved (the table does not move when the selection appears), the
 * actions share one confirmation and one result list; the calls go node by node through the single-node endpoints.
 */
export function BulkNodeActions({ selected, deployments, splitNames, onFinished }: Props) {
  const [choice, setChoice] = useState("");
  const [confirm, setConfirm] = useState<Action | null>(null);
  const [run, setRun] = useState<{ action: Action; items: Item[]; done: boolean } | null>(null);
  const running = useRef(false);

  const count = selected.length;
  const noneApplicable = (a: Action) => targetsOf(a, selected).length === 0;

  async function start(action: Action) {
    if (running.current) return;
    running.current = true;
    const picked = targetsOf(action, selected);
    const targets = picked.map((n) => n.name);
    setConfirm(null);
    setRun({ action, items: targets.map((node) => ({ node, outcome: "waiting", text: "Ожидает" })), done: false });
    const patch = (node: string, outcome: Outcome, text: string) =>
      setRun((r) => r && { ...r, items: r.items.map((i) => (i.node === node ? { ...i, outcome, text } : i)) });
    const failed: string[] = [];
    for (const node of targets) {
      patch(node, "running", "Выполняется");
      try {
        // a node with a deployment is removed with `force`: the confirmation named the deployment
        patch(node, "ok", await perform(action, node, !!picked.find((n) => n.name === node)?.deployment));
      } catch (e) {
        failed.push(node);
        patch(node, "error", errorText(e, "Не удалось выполнить действие"));
      }
    }
    setRun((r) => r && { ...r, done: true });
    running.current = false;
    await onFinished(failed);
  }

  const askAssign = () => {
    if (!choice) return;
    setConfirm({ kind: "assign", deployment: choice === NONE ? null : (deployments.find((d) => String(d.id) === choice) ?? null) });
  };

  const targets = confirm ? targetsOf(confirm, selected) : [];
  const splitInvolved =
    !!confirm &&
    confirm.kind !== "wake" &&
    confirm.kind !== "purge" &&
    confirm.kind !== "approve" &&
    confirm.kind !== "delete" && // «Удалить из кластера» says it per node (DeleteNote)
    !(confirm.kind === "maintenance" && !confirm.on) &&
    targets.some((n) => n.deployment && splitNames.has(n.deployment));

  return (
    <div className="bulkbar" role="toolbar" aria-label="Действия над выбранными нодами">
      {count === 0 ? (
        <span className="muted small">Отметьте ноды, чтобы подтвердить, назначить развёртывание, перевести на обслуживание, разбудить, очистить модели или удалить из кластера</span>
      ) : (
        <>
          <span className="bulk-count">
            Выбрано: {count} {nodesWord(count)}
          </span>
          <select
            className="select sm"
            aria-label="Назначить развёртывание выбранным"
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
          >
            <option value="" disabled>
              Назначить развёртывание…
            </option>
            <option value={NONE}>Снять назначение</option>
            {deployments.map((d) => (
              <option key={d.id} value={String(d.id)}>
                {d.name}
              </option>
            ))}
          </select>
          <button type="button" className="btn sm" disabled={!choice} onClick={askAssign}>
            Применить
          </button>
          <button
            type="button"
            className="btn quiet sm"
            disabled={noneApplicable({ kind: "maintenance", on: true })}
            onClick={() => setConfirm({ kind: "maintenance", on: true })}
          >
            На обслуживание
          </button>
          <button
            type="button"
            className="btn quiet sm"
            disabled={noneApplicable({ kind: "maintenance", on: false })}
            onClick={() => setConfirm({ kind: "maintenance", on: false })}
          >
            Вернуть в работу
          </button>
          <button
            type="button"
            className="btn quiet sm"
            disabled={noneApplicable({ kind: "wake" })}
            onClick={() => setConfirm({ kind: "wake" })}
          >
            Разбудить
          </button>
          <button
            type="button"
            className="btn quiet sm"
            disabled={noneApplicable({ kind: "purge" })}
            onClick={() => setConfirm({ kind: "purge" })}
          >
            Очистить модели
          </button>
          <button
            type="button"
            className="btn quiet sm"
            disabled={noneApplicable({ kind: "approve" })}
            onClick={() => setConfirm({ kind: "approve" })}
          >
            Подтвердить
          </button>
          <button
            type="button"
            className="btn quiet sm"
            disabled={noneApplicable({ kind: "delete" })}
            onClick={() => setConfirm({ kind: "delete" })}
          >
            Удалить из кластера…
          </button>
        </>
      )}

      {confirm && (
        <Confirm
          title={titleOf(confirm)}
          confirmLabel={confirmLabelOf(confirm)}
          onCancel={() => setConfirm(null)}
          onConfirm={() => void start(confirm)}
        >
          <p>
            {targets.length} {nodesWord(targets.length)}:
          </p>
          <ul className="node-list" aria-label="Ноды действия">
            {targets.map((n) => (
              <li key={n.name} className="mono">
                {n.name}
                {confirm.kind === "purge" && <span className="muted"> — {heldText(n)}</span>}
                {confirm.kind === "delete" && n.deployment && <span className="muted"> — развёртывание {n.deployment}</span>}
                {confirm.kind === "approve" && n.claim_ip && <span className="muted"> — с адреса {n.claim_ip}</span>}
              </li>
            ))}
          </ul>
          {confirm.kind === "purge" && <PurgeNote nodes={targets} />}
          {confirm.kind === "approve" && <p>{APPROVE_NOTE}</p>}
          {confirm.kind === "delete" && <DeleteNote nodes={targets} held={heldUnion(targets, deployments)} />}
          {splitInvolved && <p>{SPLIT_WARNING}</p>}
        </Confirm>
      )}

      {run && (
        <Modal
          title={`Результат: ${titleOf(run.action)}`}
          onClose={() => run.done && setRun(null)}
          hideClose
          footer={
            <button type="button" className="btn primary" disabled={!run.done} onClick={() => setRun(null)} data-autofocus>
              Закрыть
            </button>
          }
        >
          <ul className="node-results" aria-label="Результат по нодам" aria-live="polite">
            {run.items.map((i) => (
              <li key={i.node} className={`node-result ${i.outcome}`}>
                <span className="mark" aria-hidden="true">
                  {i.outcome === "ok" ? <Ic icon={Check} /> : i.outcome === "error" ? <Ic icon={CircleAlert} /> : null}
                </span>
                <span className="mono">{i.node}</span>
                <span className={i.outcome === "error" ? "error-text" : "muted"}>{i.text}</span>
              </li>
            ))}
          </ul>
        </Modal>
      )}
    </div>
  );
}
