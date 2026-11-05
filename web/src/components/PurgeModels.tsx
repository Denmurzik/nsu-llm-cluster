import { useState } from "react";
import { api, ApiError } from "../api";
import { fmtBytes } from "../lib/format";
import type { NodeInfo, PurgeResult } from "../types";
import { Confirm } from "./Modal";

/** what the confirmation says about the cleanup of the models volume (task 2-26) */
export const PURGE_NOTE =
  "Нода с развёртыванием сохранит то, что оно использует: файл модели, черновую модель, проектор и кеш тензоров своей группы split. Всё остальное будет удалено, и файлы других моделей придётся скачать заново. Нода без развёртывания очищается полностью. Недавно записанные файлы (идущая загрузка) не затрагиваются.";

/** the size the nodes hold, «38,2 ГБ»; nodes that have not reported one are left out, null when none did */
export function heldBytes(nodes: NodeInfo[]): string | null {
  const known = nodes.filter((n) => typeof n.models_bytes === "number");
  return known.length ? fmtBytes(known.reduce((sum, n) => sum + (n.models_bytes ?? 0), 0)) : null;
}

export function heldText(node: NodeInfo): string {
  return typeof node.models_bytes === "number" ? `занято ${fmtBytes(node.models_bytes)}` : "размер неизвестен";
}

/** one line for the result of a node: what was freed, or that the node has not answered yet */
export function purgeResultText(r: PurgeResult): string {
  if (r.status === "pending") return "Команда передана, нода ещё не ответила: размер обновится позже";
  return r.freed_bytes ? `Освобождено ${fmtBytes(r.freed_bytes)}` : "Удалять было нечего";
}

/** the part of a confirmation that tells how much the nodes hold and what stays */
export function PurgeNote({ nodes }: { nodes: NodeInfo[] }) {
  const total = heldBytes(nodes);
  return (
    <>
      {total && <p>Файлы моделей и кеш тензоров на этих нодах занимают {total}: освободится не больше этого.</p>}
      <p>{PURGE_NOTE}</p>
    </>
  );
}

/**
 * «Очистить модели» in the row of a node: one confirmation, one call, the result under the button. A node that is
 * down cannot do it (the cleanup runs on the node itself).
 */
export function NodePurge({ node, onDone }: { node: NodeInfo; onDone: () => void | Promise<void> }) {
  // the label never changes (a width change moves the right-aligned row); «Очищается…» goes to the reserved line
  const [asking, setAsking] = useState(false);
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  async function run() {
    setAsking(false);
    setPending(true);
    setResult(null);
    try {
      setResult({ ok: true, text: purgeResultText(await api.purgeNodeModels(node.name)) });
      await onDone();
    } catch (e) {
      setResult({ ok: false, text: e instanceof ApiError ? e.detail : "Не удалось очистить модели" });
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className="btn quiet sm"
        aria-label={`Очистить модели ${node.name}`}
        title={node.ready ? undefined : "Нода не в сети: очистку выполняет сама нода"}
        aria-busy={pending}
        disabled={!node.ready || pending}
        onClick={() => setAsking(true)}
      >
        Очистить модели
      </button>
      {/* the line is always there: the answer fills it without moving the rows below (DESIGN.md, stable layout) */}
      <div className="node-actions-note">
        {pending ? (
          <span className="muted small">Очищается…</span>
        ) : (
          result && (
            <span
              className={result.ok ? "muted small" : "small error-text"}
              role={result.ok ? "status" : "alert"}
              title={result.text}
            >
              {result.text}
            </span>
          )
        )}
      </div>
      {asking && (
        <Confirm
          title={`Очистить модели на ${node.name}`}
          confirmLabel="Очистить"
          onCancel={() => setAsking(false)}
          onConfirm={() => void run()}
        >
          <p>
            Нода <span className="mono">{node.name}</span>: {heldText(node)}.
          </p>
          <p>{PURGE_NOTE}</p>
        </Confirm>
      )}
    </>
  );
}
