import { useState } from "react";
import { api, ApiError } from "../api";
import type { WakeState } from "../types";

interface WakeNode {
  name: string;
  ready: boolean;
  is_main: boolean;
  maintenance: boolean;
  wakeable: boolean;
  wake: WakeState | null;
}

/** The controller's Wake-on-LAN attempts for a down node: «будим (попытка 2)», or that it gave up. */
export function wakeText(node: WakeNode): string | null {
  if (node.ready || node.maintenance || !node.wake || node.wake.tries === 0) return null;
  const { tries, max_tries } = node.wake;
  return tries >= max_tries ? `не просыпается (попыток: ${tries})` : `будим (попытка ${tries})`;
}

/** The node's state line: the attempts of the controller and, for a down node that reported its network card, the
 * «Разбудить» button (one magic packet now). */
export function NodeWake({ node }: { node: WakeNode }) {
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const text = wakeText(node);
  const offer = !node.ready && !node.is_main && node.wakeable;
  if (!text && !offer) return null;

  async function wake() {
    setPending(true);
    setResult(null);
    try {
      const r = await api.wakeNode(node.name);
      const via = r.relay ? `, и через ноду ${r.relay}` : "";
      setResult({ ok: true, text: `Пакет пробуждения отправлен: ${r.targets.join(", ")}${via}` });
    } catch (e) {
      setResult({ ok: false, text: e instanceof ApiError ? e.detail : "Не удалось отправить пакет пробуждения" });
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="stackcell">
      {text && <span className="muted small">{text}</span>}
      {offer && (
        <button
          type="button"
          className="btn quiet sm"
          aria-label={`Разбудить ${node.name}`}
          disabled={pending}
          onClick={() => void wake()}
        >
          Разбудить
        </button>
      )}
      {result && (
        <span className={result.ok ? "muted small" : "small error-text"} role={result.ok ? "status" : "alert"}>
          {result.text}
        </span>
      )}
    </div>
  );
}
