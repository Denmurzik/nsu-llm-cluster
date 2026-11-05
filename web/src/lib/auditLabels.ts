import type { AuditEntry } from "../types";

/** What the log says in Russian; an action the table does not know is shown by its code. */
const ACTION_LABEL: Record<string, string> = {
  "deployment.create": "Развёртывание создано",
  "deployment.update": "Развёртывание изменено",
  "deployment.delete": "Развёртывание удалено",
  "deployment.refresh_revision": "Ревизия модели обновлена",
  "node.assign": "Нода назначена",
  "node.maintenance": "Обслуживание ноды",
  "node.wake": "Нода разбужена",
  "node.update": "Нода обновлена",
  "node.delete": "Нода удалена",
  "node.purge_models": "Очистка весов на ноде",
  "user.role": "Роль пользователя изменена",
  "user.block": "Пользователь заблокирован",
  "user.unblock": "Пользователь разблокирован",
  "user.limits": "Лимиты пользователя изменены",
  "key.revoke": "Ключ отозван",
  "setting.update": "Настройка изменена",
  "cache.delete": "Кэш модели удалён",
  "cache.prefetch": "Кэш модели загружен заранее",
  "backup.run": "Резервная копия",
  "local_model.upload": "Своя модель загружена",
  "local_model.delete": "Своя модель удалена",
};

const TARGET_LABEL: Record<string, string> = {
  deployment: "развёртывание",
  node: "нода",
  user: "пользователь",
  key: "ключ",
  setting: "настройка",
  cache: "кэш",
  backup: "копия",
  local_model: "своя модель",
};

export const actionLabel = (action: string): string => ACTION_LABEL[action] ?? action;
export const targetTypeLabel = (type: string): string => TARGET_LABEL[type] ?? type;

/** A user is logged by id; the e-mail in the details reads better. */
export function targetText(e: AuditEntry): string {
  const email = e.details?.email;
  return e.target_type === "user" && typeof email === "string" ? email : e.target;
}

/** The groups of the filter: the prefix the portal matches. */
export const ACTION_GROUPS: { prefix: string; label: string }[] = [
  { prefix: "", label: "Все действия" },
  { prefix: "deployment.", label: "Развёртывания" },
  { prefix: "node.", label: "Ноды" },
  { prefix: "user.", label: "Пользователи" },
  { prefix: "key.", label: "Ключи" },
  { prefix: "setting.", label: "Настройки" },
  { prefix: "local_model.", label: "Свои модели" },
  { prefix: "cache.", label: "Кэш моделей" },
  { prefix: "backup.", label: "Резервные копии" },
];

/** The keys of the details in Russian; a key the table does not know is shown as it is. */
const DETAIL_LABEL: Record<string, string> = {
  email: "Почта",
  owner: "Владелец",
  alias: "Название",
  masked: "Ключ",
  by: "Кто",
  old: "Было",
  new: "Стало",
  changed: "Изменено",
  changes: "Изменения",
  reason: "Причина",
  reason_changed: "Причина изменена",
  key_count: "Ключей",
  role_source: "Источник роли",
  repo: "Репозиторий",
  file: "Файл",
  engine: "Движок",
  mode: "Режим",
  replicas: "Реплик",
  revision: "Ревизия",
  draft_revision: "Ревизия черновика",
  node: "Нода",
  source: "Источник",
  manual: "Вручную",
  maintenance: "Обслуживание",
  reset: "Сброс к умолчанию",
  size_bytes: "Размер, байт",
  deployment_id: "Развёртывание",
  rpm_limit: "Запросов в минуту",
  tpm_limit: "Токенов в минуту",
  max_keys: "Лимит ключей",
};

export const detailLabel = (key: string): string => DETAIL_LABEL[key] ?? key;

/** One detail value as a short string: nested lists and objects as JSON. */
export function detailText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "да" : "нет";
  if (typeof value === "number") return String(value);
  return JSON.stringify(value);
}
