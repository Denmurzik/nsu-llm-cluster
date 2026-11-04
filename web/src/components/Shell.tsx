import {
  Activity,
  Boxes,
  ChartColumn,
  Cpu,
  HardDrive,
  KeyRound,
  Layers,
  LogOut,
  Menu,
  MessageSquare,
  SquareTerminal,
  Users,
  X,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useMe } from "../auth";
import { Logo } from "./Logo";
import { Ic } from "./ui";

const USER_LINKS = [
  { to: "/models", label: "Модели", icon: Boxes },
  { to: "/keys", label: "Ключи", icon: KeyRound },
  { to: "/quickstart", label: "Быстрый старт", icon: SquareTerminal },
  { to: "/playground", label: "Playground", icon: MessageSquare },
  { to: "/usage", label: "Использование", icon: ChartColumn },
];

const ADMIN_LINKS = [
  { to: "/admin/deployments", label: "Развёртывания", icon: Layers },
  { to: "/admin/local-models", label: "Мои модели", icon: HardDrive },
  { to: "/admin/nodes", label: "Ноды", icon: Cpu },
  { to: "/admin/status", label: "Состояние", icon: Activity },
  { to: "/admin/users", label: "Пользователи", icon: Users },
];

export function Shell({ children }: { children: ReactNode }) {
  const me = useMe();
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  useEffect(() => setOpen(false), [pathname]);

  return (
    <div className={`shell${open ? " nav-open" : ""}`}>
      <header className="topbar">
        <span className="brand">
          <Logo />
        </span>
        <button
          type="button"
          className="btn quiet sm"
          aria-label={open ? "Закрыть меню" : "Открыть меню"}
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <Ic icon={open ? X : Menu} />
        </button>
      </header>
      <div className="scrim" onClick={() => setOpen(false)} aria-hidden="true" />

      <aside className="sidebar">
        <NavLink to="/models" className="brand" aria-label="NSU LLM, на главную">
          <Logo />
        </NavLink>

        <nav className="nav" aria-label="Разделы">
          <div className="nav-label" id="nav-main">
            Работа
          </div>
          {USER_LINKS.map((l) => (
            <NavLink key={l.to} to={l.to} className="nav-link">
              <Ic icon={l.icon} />
              <span>{l.label}</span>
            </NavLink>
          ))}
          {me.role === "admin" && (
            <>
              <div className="nav-label" id="nav-admin">
                Администрирование
              </div>
              {ADMIN_LINKS.map((l) => (
                <NavLink key={l.to} to={l.to} className="nav-link">
                  <Ic icon={l.icon} />
                  <span>{l.label}</span>
                </NavLink>
              ))}
            </>
          )}
        </nav>

        <div className="userbox">
          <div className="who">
            <span>{me.full_name || me.username}</span>
            <span className="muted small">{me.email}</span>
          </div>
          <a className="nav-link" href="/auth/logout">
            <Ic icon={LogOut} />
            <span>Выйти</span>
          </a>
        </div>
      </aside>

      <main className="main">{children}</main>
    </div>
  );
}
