import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useMe } from "../auth";

export function Forbidden({
  title = "Недостаточно прав",
  text = "Этот раздел доступен только администраторам",
}: {
  title?: string;
  text?: string;
}) {
  return (
    <div className="page">
      <div>
        <h1>{title}</h1>
        <p className="lead secondary" style={{ marginTop: 8 }}>
          {text}
        </p>
      </div>
      <p>
        <Link to="/models">К списку моделей</Link>
      </p>
    </div>
  );
}

export function RequireAdmin({ children }: { children: ReactNode }) {
  const me = useMe();
  return me.role === "admin" ? <>{children}</> : <Forbidden />;
}
