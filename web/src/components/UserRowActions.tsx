import { Ban, Gauge, KeyRound, ListRestart, LockOpen, ShieldCheck, ShieldOff } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { AdminUserFull, UserPatch } from "../types";
import { Confirm, Modal } from "./Modal";
import { RowMenu, type RowMenuItem } from "./RowMenu";
import { ErrorNotice } from "./ui";
import { UserKeysDialog } from "./UserKeysDialog";
import "../styles/row-actions.css";
import "../styles/admin-users.css";

type Asking = "to-admin" | "to-user" | "to-list" | "block" | "limits" | "keys" | null;

const nameOf = (u: AdminUserFull) => u.full_name || u.email;

/**
 * The «⋯» menu of a user row: the role (by hand, or back to the ADMIN_EMAILS list), blocking, limits and the keys.
 * The role changes ask first; «Разблокировать» is immediate. Oneself cannot be blocked or demoted, so those items are
 * not offered to the signed-in admin. A failure goes to the page through `onError`.
 */
export function UserRowActions({
  user,
  myId,
  onChanged,
  onError,
}: {
  user: AdminUserFull;
  myId: string;
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
  const myself = user.id === myId;

  /** Sends a PATCH; true when it went through. A failure is shown by the caller (a dialog) or the page. */
  async function patch(body: UserPatch, failure: string, toPage = true): Promise<string | null> {
    setBusy(true);
    if (toPage) onError(null);
    try {
      await api.patchUser(user.id, body);
      onChanged();
      if (alive.current) setAsking(null);
      return null;
    } catch (err) {
      const text = err instanceof ApiError ? err.detail : failure;
      if (toPage) {
        onError(text);
        if (alive.current) setAsking(null);
      }
      return text;
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  const items: RowMenuItem[] = [];
  if (user.role === "admin") {
    if (!myself) items.push({ key: "to-user", label: "Сделать пользователем", icon: ShieldOff, onSelect: () => setAsking("to-user") });
  } else {
    items.push({ key: "to-admin", label: "Сделать администратором", icon: ShieldCheck, onSelect: () => setAsking("to-admin") });
  }
  if (user.role_source === "manual" && !myself) {
    items.push({ key: "to-list", label: "Вернуть роль по списку ADMIN_EMAILS", icon: ListRestart, onSelect: () => setAsking("to-list") });
  }
  if (!myself) {
    items.push(
      user.blocked
        ? {
            key: "unblock",
            label: "Разблокировать",
            icon: LockOpen,
            onSelect: () => void patch({ blocked: false }, "Не удалось разблокировать"),
          }
        : { key: "block", label: "Заблокировать…", icon: Ban, danger: true, onSelect: () => setAsking("block") },
    );
  }
  items.push({ key: "limits", label: "Лимиты…", icon: Gauge, onSelect: () => setAsking("limits") });
  items.push({ key: "keys", label: "Ключи", icon: KeyRound, onSelect: () => setAsking("keys") });

  return (
    <>
      <RowMenu label={`Действия: ${user.email}`} items={items} disabled={busy} />
      {asking === "to-admin" && (
        <Confirm
          title="Назначение администратором"
          confirmLabel="Сделать администратором"
          busy={busy}
          onCancel={() => setAsking(null)}
          onConfirm={() => void patch({ role: "admin" }, "Не удалось изменить роль")}
        >
          <p>Сделать {nameOf(user)} администратором?</p>
          <p className="secondary">
            Он сможет управлять моделями, нодами и пользователями и увидит экспериментальные модели. Роль останется, пока вы не вернёте её по
            списку ADMIN_EMAILS.
          </p>
        </Confirm>
      )}
      {asking === "to-user" && (
        <Confirm
          title="Снятие роли администратора"
          confirmLabel="Сделать пользователем"
          busy={busy}
          onCancel={() => setAsking(null)}
          onConfirm={() => void patch({ role: "user" }, "Не удалось изменить роль")}
        >
          <p>Сделать {nameOf(user)} обычным пользователем?</p>
          <p className="secondary">Управление и экспериментальные модели станут недоступны, ключи потеряют доступ к экспериментальным моделям сразу.</p>
        </Confirm>
      )}
      {asking === "to-list" && (
        <Confirm
          title="Роль по списку"
          confirmLabel="Вернуть по списку"
          busy={busy}
          onCancel={() => setAsking(null)}
          onConfirm={() => void patch({ role_source: "auto" }, "Не удалось изменить роль")}
        >
          <p>Роль {nameOf(user)} снова определяется списком ADMIN_EMAILS.</p>
          <p className="secondary">Если почты нет в списке, он станет обычным пользователем.</p>
        </Confirm>
      )}
      {asking === "block" && (
        <BlockDialog
          user={user}
          busy={busy}
          onCancel={() => setAsking(null)}
          onSubmit={(reason) => patch({ blocked: true, blocked_reason: reason }, "Не удалось заблокировать", false)}
        />
      )}
      {asking === "limits" && (
        <LimitsDialog
          user={user}
          busy={busy}
          onCancel={() => setAsking(null)}
          onSubmit={(limits) => patch(limits, "Не удалось сохранить лимиты", false)}
        />
      )}
      {asking === "keys" && <UserKeysDialog user={user} onClose={() => setAsking(null)} onChanged={onChanged} />}
    </>
  );
}

function BlockDialog({
  user,
  busy,
  onCancel,
  onSubmit,
}: {
  user: AdminUserFull;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (reason: string | null) => Promise<string | null>;
}) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal
      title="Блокировка пользователя"
      onClose={onCancel}
      hideClose
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel}>
            Отмена
          </button>
          <button
            type="button"
            className="btn danger"
            disabled={busy}
            onClick={async () => setError(await onSubmit(reason.trim() || null))}
          >
            Заблокировать
          </button>
        </>
      }
    >
      <p>Заблокировать {nameOf(user)}?</p>
      <p className="secondary">
        Он сразу выйдет из системы, а его ключи API перестанут работать. После разблокировки те же ключи заработают снова.
      </p>
      <div className="field">
        <label htmlFor="block-reason">Причина</label>
        <textarea
          id="block-reason"
          className="textarea"
          rows={3}
          maxLength={500}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          data-autofocus
        />
        <span className="hint">Необязательно. Её увидит пользователь при попытке войти.</span>
      </div>
      <ErrorNotice error={error} />
    </Modal>
  );
}

const LIMIT_FIELDS = [
  { key: "rpm_limit", label: "Запросов в минуту на ключ", hint: "Общий лимит из настроек, если поле пусто" },
  { key: "tpm_limit", label: "Токенов в минуту на ключ", hint: "Общий лимит из настроек, если поле пусто" },
  { key: "max_keys", label: "Сколько ключей можно создать", hint: "Общее число из настроек, если поле пусто" },
] as const;

type LimitKey = (typeof LIMIT_FIELDS)[number]["key"];

function LimitsDialog({
  user,
  busy,
  onCancel,
  onSubmit,
}: {
  user: AdminUserFull;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (limits: Pick<UserPatch, LimitKey>) => Promise<string | null>;
}) {
  const [values, setValues] = useState<Record<LimitKey, string>>({
    rpm_limit: user.rpm_limit === null ? "" : String(user.rpm_limit),
    tpm_limit: user.tpm_limit === null ? "" : String(user.tpm_limit),
    max_keys: user.max_keys === null ? "" : String(user.max_keys),
  });
  const [error, setError] = useState<string | null>(null);

  const bad = LIMIT_FIELDS.find((f) => values[f.key].trim() !== "" && !/^[1-9]\d*$/.test(values[f.key].trim()));

  async function save() {
    if (bad) {
      setError(`«${bad.label}»: нужно целое число больше нуля или пустое поле`);
      return;
    }
    const num = (k: LimitKey) => (values[k].trim() === "" ? null : Number(values[k].trim()));
    setError(await onSubmit({ rpm_limit: num("rpm_limit"), tpm_limit: num("tpm_limit"), max_keys: num("max_keys") }));
  }

  return (
    <Modal
      title="Лимиты пользователя"
      onClose={onCancel}
      hideClose
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel}>
            Отмена
          </button>
          <button type="button" className="btn primary" disabled={busy} onClick={() => void save()}>
            Сохранить
          </button>
        </>
      }
    >
      <p>{nameOf(user)}. Лимиты применяются к уже созданным ключам и к новым.</p>
      {LIMIT_FIELDS.map((f, i) => (
        <div className="field" key={f.key}>
          <label htmlFor={`limit-${f.key}`}>{f.label}</label>
          <input
            id={`limit-${f.key}`}
            className="input"
            inputMode="numeric"
            placeholder="общий"
            value={values[f.key]}
            onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
            data-autofocus={i === 0 ? true : undefined}
          />
          <span className="hint">{f.hint}</span>
        </div>
      ))}
      <ErrorNotice error={error} />
    </Modal>
  );
}
