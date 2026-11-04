import { ChevronDown, ChevronRight } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import { useDebounced } from "../lib/useResource";
import type { CommandPreview as Command } from "../types";
import { CopyButton } from "./CopyButton";
import { Ic } from "./ui";

/** The block «Команда llama-server»: the command the pods would run, built by the portal (keys masked).
 * `request` is asked again, after typing settles, whenever `requestKey` changes. The block is collapsed; the answer is
 * fetched anyway, so a 422 about the form is known (`onError`) before the block is opened. */
export function CommandPreview({
  requestKey,
  request,
  title = "Команда llama-server",
  onError,
}: {
  requestKey: string | null;
  request: () => Promise<Command>;
  title?: string;
  /** the portal's 422 text, or null when the form is accepted */
  onError?: (detail: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const settled = useDebounced(requestKey, 400);
  const [result, setResult] = useState<{ key: string | null; data: Command | null; error: string | null }>({
    key: null,
    data: null,
    error: null,
  });
  const requestRef = useRef(request);
  requestRef.current = request;
  const errorRef = useRef(onError);
  errorRef.current = onError;

  useEffect(() => {
    if (settled === null) {
      setResult({ key: null, data: null, error: null });
      errorRef.current?.(null);
      return;
    }
    let cancelled = false;
    requestRef
      .current()
      .then((data) => {
        if (cancelled) return;
        setResult({ key: settled, data, error: null });
        errorRef.current?.(null);
      })
      .catch((e) => {
        if (cancelled) return;
        const detail = e instanceof ApiError ? e.detail : "Не удалось получить команду";
        setResult({ key: settled, data: null, error: detail });
        errorRef.current?.(e instanceof ApiError && e.status === 422 ? detail : null);
      });
    return () => {
      cancelled = true;
    };
  }, [settled]);

  const stale = requestKey !== null && result.key !== requestKey;
  const { data, error } = result;
  return (
    <section className="card stack" aria-label={title}>
      <button
        type="button"
        className="btn quiet"
        style={{ justifyContent: "flex-start" }}
        aria-expanded={open}
        aria-controls="cmd-body"
        onClick={() => setOpen(!open)}
      >
        <Ic icon={open ? ChevronDown : ChevronRight} />
        {title}
      </button>
      {open && (
        <div id="cmd-body" className="section" aria-busy={stale}>
          {error && <p className="error-text">{error}</p>}
          {!data && !error && <p className="muted">Идёт расчёт команды</p>}
          {data && (
            <div className="section" style={{ opacity: stale ? 0.6 : 1 }}>
              <Block label={data.rpc ? "Лидер группы" : "Сервер"} text={data.leader} />
              {data.rpc && <Block label="Rpc-сервер каждой ноды" text={data.rpc} />}
              {data.notes.length > 0 && (
                <ul className="hint" style={{ margin: 0, paddingLeft: "var(--s5)" }}>
                  {data.notes.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function Block({ label, text }: { label: string; text: string }) {
  return (
    <div className="field">
      <span className="label">{label}</span>
      <div className="code wrap">
        <pre className="mono small" style={{ paddingRight: "var(--s4)" }}>
          {text}
        </pre>
        <div style={{ display: "flex", justifyContent: "flex-end", padding: "0 var(--s2) var(--s2)" }}>
          <CopyButton text={text} className="btn sm" ariaLabel={`Копировать: ${label}`} />
        </div>
      </div>
    </div>
  );
}
