import { fmtBytes } from "../lib/format";
import type { NodeInfo } from "../types";
import { Confirm } from "./Modal";

/** Chosen nodes that another deployment holds right now: saving would take them over. */
export function takenNodes(nodes: NodeInfo[], selected: string[], own: string | null, keep: string[] = []) {
  return nodes
    .filter((n) => selected.includes(n.name) && !keep.includes(n.name) && n.deployment && n.deployment !== own)
    .map((n) => ({ node: n.name, from: n.deployment as string }));
}

export function NodePicker({
  nodes,
  selected,
  onChange,
  own = null,
}: {
  nodes: NodeInfo[];
  selected: string[];
  onChange: (names: string[]) => void;
  /** name of the deployment being edited: its own nodes are not marked as busy */
  own?: string | null;
}) {
  if (nodes.length === 0) return <p className="muted">Нод пока нет</p>;
  const toggle = (name: string) =>
    onChange(nodes.map((n) => n.name).filter((n) => (n === name ? !selected.includes(n) : selected.includes(n))));
  return (
    <div className="choices" role="group" aria-label="Ноды">
      {nodes.map((n) => (
        <label key={n.name} className="choice">
          <input
            type="checkbox"
            aria-label={n.name}
            checked={selected.includes(n.name)}
            onChange={() => toggle(n.name)}
          />
          <span className="grow mono">{n.name}</span>
          <span className="tag">{n.accel}</span>
          <span className="muted small nowrap">{n.capacity_bytes ? fmtBytes(n.capacity_bytes) : "—"}</span>
          {n.deployment && n.deployment !== own && <span className="muted small">занята: {n.deployment}</span>}
          {!n.ready && <span className="muted small">не в сети</span>}
        </label>
      ))}
    </div>
  );
}

export function TakeOverConfirm({
  taken,
  confirmLabel,
  busy,
  onConfirm,
  onCancel,
}: {
  taken: { node: string; from: string }[];
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Confirm title="Ноды уже заняты" confirmLabel={confirmLabel} busy={busy} onCancel={onCancel} onConfirm={onConfirm}>
      <p>Эти ноды сейчас обслуживают другие развёртывания и будут переданы этому:</p>
      <ul>
        {taken.map((t) => (
          <li key={t.node}>
            <span className="mono">{t.node}</span>, сейчас занята развёртыванием «{t.from}»
          </li>
        ))}
      </ul>
    </Confirm>
  );
}
