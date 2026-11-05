import { NavLink } from "react-router-dom";
import { useMe } from "../auth";
import "../styles/admin-ops.css";

/** For admins: «Моё» and «Все пользователи» beside each other; the page of the other kind is one click away. */
export function UsageOwner() {
  const me = useMe();
  if (me.role !== "admin") return null;
  return (
    <nav className="tabs link-tabs" aria-label="Чьё использование">
      <NavLink to="/usage" end className="tab">
        Моё
      </NavLink>
      <NavLink to="/admin/usage" end className="tab">
        Все пользователи
      </NavLink>
    </nav>
  );
}
