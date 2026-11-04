import { Check, Copy, TriangleAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { copyText } from "../lib/clipboard";
import { Ic } from "./ui";

export function CopyButton({
  text,
  label = "Копировать",
  ariaLabel,
  className = "btn sm",
  autoFocus,
}: {
  text: string;
  label?: string;
  /** accessible name when it must differ from the visible label */
  ariaLabel?: string;
  className?: string;
  autoFocus?: boolean;
}) {
  const [state, setState] = useState<"idle" | "done" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  async function copy() {
    const ok = await copyText(text);
    setState(ok ? "done" : "failed");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), ok ? 1800 : 4000);
  }

  const done = state === "done";
  const failed = state === "failed";
  const shown = done ? "Скопировано" : failed ? "Не удалось скопировать" : label;

  return (
    <button
      type="button"
      className={className}
      onClick={copy}
      aria-label={done || failed ? shown : (ariaLabel ?? label)}
      data-autofocus={autoFocus ? "" : undefined}
      data-state={state}
    >
      <Ic icon={done ? Check : failed ? TriangleAlert : Copy} />
      {shown}
    </button>
  );
}
