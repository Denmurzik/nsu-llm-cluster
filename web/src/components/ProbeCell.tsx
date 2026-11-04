import { Activity, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError } from "../api";
import { probeDeployment, type ProbeResult } from "../lib/adminActions";
import { fmtInt } from "../lib/format";
import { Ic } from "./ui";
import "../styles/tools.css";

const POPOVER_WIDTH = 320;

/** «первый токен 820 мс» and «62 ток/с», whichever of them the probe measured */
export function probeMetrics(r: Pick<ProbeResult, "ttft_ms" | "tokens_per_s">): string[] {
  const out: string[] = [];
  if (r.ttft_ms !== null) out.push(`первый токен ${fmtInt(r.ttft_ms)} мс`);
  if (r.tokens_per_s !== null) {
    const speed = r.tokens_per_s >= 10 ? fmtInt(Math.round(r.tokens_per_s)) : r.tokens_per_s.toFixed(1).replace(".", ",");
    out.push(`${speed} ток/с`);
  }
  return out;
}

type State = { kind: "idle" } | { kind: "running" } | { kind: "done"; result: ProbeResult } | { kind: "failed"; error: string };

/**
 * The «Проверить» button of a deployment row. The result opens in a popover over the page (not in the row), so the
 * table does not change height; it closes on Escape, a click outside or the close button.
 */
export function ProbeCell({ id, name, enabled }: { id: number; name: string; enabled: boolean }) {
  const [state, setState] = useState<State>({ kind: "idle" });
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
  const button = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => void (alive.current = false);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!pop.current?.contains(target) && !button.current?.contains(target)) setOpen(false);
    };
    const close = () => setOpen(false);
    // the answer box scrolls inside the popover: that is not the page moving
    const onScroll = (e: Event) => !pop.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open]);

  function place() {
    const rect = button.current?.getBoundingClientRect();
    if (!rect) return;
    const left = Math.max(8, Math.min(rect.right - POPOVER_WIDTH, window.innerWidth - POPOVER_WIDTH - 8));
    setAt({ top: rect.bottom + 4, left });
  }

  async function run() {
    if (state.kind === "running") return;
    setState({ kind: "running" });
    setOpen(false);
    try {
      const result = await probeDeployment(id);
      if (!alive.current) return;
      setState({ kind: "done", result });
    } catch (e) {
      if (!alive.current) return;
      setState({ kind: "failed", error: e instanceof ApiError ? e.detail : "Не удалось выполнить проверку" });
    }
    place();
    setOpen(true);
  }

  const running = state.kind === "running";
  const result = state.kind === "done" ? state.result : null;
  const metrics = result ? probeMetrics(result) : [];
  return (
    <>
      <button
        ref={button}
        type="button"
        className="btn quiet sm probe-btn"
        title={enabled ? undefined : "Развёртывание выключено"}
        disabled={!enabled || running}
        onClick={() => void run()}
      >
        <Ic icon={Activity} />
        {running ? "Проверка" : "Проверить"}
      </button>
      {open &&
        state.kind !== "idle" &&
        state.kind !== "running" &&
        createPortal(
          <div
            ref={pop}
            className="popover"
            role="status"
            aria-label={`Проверка ${name}`}
            style={{ top: at.top, left: at.left, width: POPOVER_WIDTH }}
          >
            <div className="popover-head">
              <span className="mono small">{name}</span>
              <button type="button" className="btn quiet sm" aria-label="Закрыть результат проверки" onClick={() => setOpen(false)}>
                <Ic icon={X} />
              </button>
            </div>
            {state.kind === "failed" && <p className="error-text">{state.error}</p>}
            {result && result.ok && (
              <>
                {result.answer ? (
                  <p className="probe-answer mono">{result.answer}</p>
                ) : (
                  <p className="secondary small">Модель рассуждает: 16 токенов ушли на размышление, ответа пока нет</p>
                )}
                {metrics.length > 0 && <p className="muted small">{metrics.join(" · ")}</p>}
              </>
            )}
            {result && !result.ok && (
              <>
                <p className="error-text">{result.error ?? "Модель не ответила"}</p>
                {result.answer && <p className="probe-answer mono muted">{result.answer}</p>}
                {metrics.length > 0 && <p className="muted small">{metrics.join(" · ")}</p>}
              </>
            )}
          </div>,
          document.body,
        )}
    </>
  );
}
