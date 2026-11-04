import { MoreHorizontal } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { Ic } from "./ui";
import "../styles/row-actions.css";

const MENU_WIDTH = 200;

export interface RowMenuItem {
  key: string;
  label: string;
  icon: Parameters<typeof Ic>[0]["icon"];
  danger?: boolean;
  onSelect: () => void;
}

/**
 * The «⋯» button of a table row and its menu. The menu is a portal with fixed position (the table scrolls sideways and
 * would clip it), it closes on Escape, a click outside, Tab, a scroll and a resize; arrows, Home and End move between
 * the items, Escape gives the focus back to the button.
 */
export function RowMenu({ label, items, disabled }: { label: string; items: RowMenuItem[]; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!menu.current?.contains(target) && !button.current?.contains(target)) setOpen(false);
    };
    const close = () => setOpen(false);
    const onScroll = (e: Event) => !menu.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open]);

  function toggle() {
    if (disabled) return;
    if (!open) {
      const rect = button.current?.getBoundingClientRect();
      if (rect) {
        const left = Math.max(8, Math.min(rect.right - MENU_WIDTH, window.innerWidth - MENU_WIDTH - 8));
        setAt({ top: rect.bottom + 4, left });
      }
    }
    setOpen(!open);
  }

  function onKey(e: ReactKeyboardEvent) {
    const nodes = Array.from(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    const index = nodes.indexOf(document.activeElement as HTMLElement);
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      button.current?.focus();
    } else if (e.key === "Tab") {
      // the menu is a layer at the end of the page: the focus goes back to the button, Tab then moves on from there
      button.current?.focus();
      setOpen(false);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      nodes[(index + 1) % nodes.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      nodes[(index - 1 + nodes.length) % nodes.length]?.focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      nodes[0]?.focus();
    } else if (e.key === "End") {
      e.preventDefault();
      nodes[nodes.length - 1]?.focus();
    }
  }

  function pick(item: RowMenuItem) {
    setOpen(false);
    button.current?.focus();
    item.onSelect();
  }

  return (
    <>
      <button
        ref={button}
        type="button"
        className="btn quiet sm icon-only"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-disabled={disabled || undefined}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            toggle();
          }
        }}
      >
        <Ic icon={MoreHorizontal} />
      </button>
      {open &&
        createPortal(
          <div
            ref={menu}
            className="row-menu"
            role="menu"
            aria-label={label}
            style={{ top: at.top, left: at.left, width: MENU_WIDTH }}
            onKeyDown={onKey}
          >
            {items.map((item) => (
              <button
                key={item.key}
                type="button"
                role="menuitem"
                className={`row-menu-item${item.danger ? " danger" : ""}`}
                tabIndex={-1}
                onClick={() => pick(item)}
              >
                <Ic icon={item.icon} />
                {item.label}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}
