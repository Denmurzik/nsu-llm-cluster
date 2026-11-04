import { STATUS_LABEL, plural } from "../lib/labels";
import type { ModelStatus } from "../types";

const MAX_CELLS = 8;

/** Signature detail: one cell per ready backend, a hollow cell when none is up. */
export function Cells({ ready, status }: { ready: number | null; status: ModelStatus }) {
  const known = ready !== null && status !== "unknown";
  const filled = known ? Math.min(ready, MAX_CELLS) : 0;
  const extra = known && ready > MAX_CELLS ? ready - MAX_CELLS : 0;
  return (
    <span className="cells" aria-hidden="true">
      {filled > 0 ? (
        Array.from({ length: filled }, (_, i) => <i key={i} className="cell" />)
      ) : (
        <i className="cell empty" />
      )}
      {extra > 0 && <span className="more">+{extra}</span>}
    </span>
  );
}

export function StatusLabel({ status, off }: { status: ModelStatus; off?: boolean }) {
  const cls = off ? "off" : status;
  return (
    <span className="status">
      <i className={`dot ${cls}`} aria-hidden="true" />
      <span>{off ? "Выключено" : STATUS_LABEL[status]}</span>
    </span>
  );
}

export function backendsText(ready: number | null): string {
  if (ready === null) return "нет данных";
  return `${ready} ${plural(ready, ["экземпляр", "экземпляра", "экземпляров"])}`;
}
