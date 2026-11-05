import { Check, Download } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api";
import { plural } from "../lib/labels";
import type { Deployment } from "../types";
import { Ic, Notice } from "./ui";

/** «Скачать заранее» for a deployment: the mirror downloads its weights (model, projector, draft model) at the pinned
 * commits, so that the nodes find them ready. The result is a line next to the button, the progress is on «Кэш моделей».
 * `compact` is the icon-only variant for a table row: the icon turns into a check mark for a few seconds, a hidden status
 * line tells screen readers; `onResult` gets the outcome (the row passes errors on to the page). */
export function PrefetchButton({
  dep,
  compact = false,
  onResult,
}: {
  dep: Pick<Deployment, "id" | "name">;
  compact?: boolean;
  onResult?: (text: string | null, error: string | null) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [queued, setQueued] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const queuedText = queued === null ? null : `Поставлено в очередь: ${queued} ${plural(queued, ["файл", "файла", "файлов"])}`;

  async function run() {
    setBusy(true);
    setQueued(null);
    setError(null);
    onResult?.(null, null);
    try {
      const r = await api.prefetchDeployment(dep.id);
      setQueued(r.jobs.length);
      onResult?.(`Поставлено в очередь: ${r.jobs.length} ${plural(r.jobs.length, ["файл", "файла", "файлов"])}`, null);
      if (compact) {
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setQueued(null), 3000);
      }
    } catch (e) {
      const text = e instanceof ApiError ? e.detail : "Не удалось поставить загрузку в очередь";
      setError(text);
      onResult?.(null, text);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className={compact ? "btn quiet sm" : "btn quiet"}
        disabled={busy}
        aria-label={compact ? `Скачать заранее файлы развёртывания ${dep.name}` : undefined}
        onClick={() => void run()}
      >
        <Ic icon={compact && queued !== null ? Check : Download} />
        {!compact && "Скачать заранее"}
      </button>
      {compact && queuedText && (
        <span className="visually-hidden" role="status">
          {queuedText}
        </span>
      )}
      {!compact && queued !== null && (
        <span className="secondary small" role="status">
          {queuedText}.{" "}
          <Link to="/admin/cache">Ход загрузки в «Кэше моделей»</Link>
        </span>
      )}
      {!compact && !onResult && error && <Notice kind="error">{error}</Notice>}
    </>
  );
}
