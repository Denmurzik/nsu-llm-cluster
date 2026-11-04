import { Power, PowerOff, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { Deployment } from "../types";
import { Confirm } from "./Modal";
import { ProbeCell } from "./ProbeCell";
import { RowMenu, type RowMenuItem } from "./RowMenu";
import "../styles/row-actions.css";

type Asking = "delete" | "disable" | null;

/**
 * The actions of a row in «Развёртывания»: «Проверить» stays in view, «Выключить» / «Включить» and «Удалить» sit in the
 * «⋯» menu. Switching off and on is immediate (a PATCH of `enabled`); a split group stops as a whole and starts again
 * from scratch, so switching it off asks first. Deleting always asks. A failure goes to the page through `onError`.
 */
export function DeploymentRowActions({
  dep,
  onChanged,
  onError,
}: {
  dep: Deployment;
  onChanged: () => void;
  onError: (message: string | null) => void;
}) {
  const [asking, setAsking] = useState<Asking>(null);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => void (alive.current = false);
  }, []);

  async function run(action: () => Promise<unknown>, failure: string) {
    setBusy(true);
    onError(null);
    try {
      await action();
      onChanged();
    } catch (err) {
      onError(err instanceof ApiError ? err.detail : failure);
    } finally {
      if (alive.current) {
        setBusy(false);
        setAsking(null);
      }
    }
  }

  const setEnabled = (enabled: boolean) =>
    run(() => api.updateDeployment(dep.id, { enabled }), enabled ? "Не удалось включить" : "Не удалось выключить");
  const remove = () => run(() => api.deleteDeployment(dep.id), "Не удалось удалить");

  const items: RowMenuItem[] = [
    dep.enabled
      ? {
          key: "disable",
          label: "Выключить",
          icon: PowerOff,
          onSelect: () => (dep.mode === "split" ? setAsking("disable") : void setEnabled(false)),
        }
      : { key: "enable", label: "Включить", icon: Power, onSelect: () => void setEnabled(true) },
    { key: "delete", label: "Удалить", icon: Trash2, danger: true, onSelect: () => setAsking("delete") },
  ];

  return (
    <div className="row-actions">
      <ProbeCell id={dep.id} name={dep.name} enabled={dep.enabled} />
      <RowMenu label={`Действия: ${dep.name}`} items={items} disabled={busy} />
      {asking === "delete" && (
        <Confirm
          title="Удаление развёртывания"
          confirmLabel="Удалить"
          busy={busy}
          onCancel={() => setAsking(null)}
          onConfirm={() => void remove()}
        >
          <p>Удалить развёртывание «{dep.name}»? Это действие нельзя отменить.</p>
          <p className="secondary">Поды остановятся, модель пропадёт из каталога, ноды освободятся.</p>
        </Confirm>
      )}
      {asking === "disable" && (
        <Confirm
          title="Выключение развёртывания"
          confirmLabel="Выключить"
          busy={busy}
          onCancel={() => setAsking(null)}
          onConfirm={() => void setEnabled(false)}
        >
          <p>Выключить развёртывание «{dep.name}»? Группа нод остановится целиком, включение запустит её заново.</p>
          <p className="secondary">Поды остановятся, модель пропадёт из каталога, ноды освободятся.</p>
        </Confirm>
      )}
    </div>
  );
}
