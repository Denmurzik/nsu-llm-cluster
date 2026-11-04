/** Maps FastAPI/pydantic 422 items ({loc, type, msg}) to Russian sentences. */

export interface ValidationItem {
  loc?: (string | number)[];
  type?: string;
  msg?: string;
}

const VALUE_ERROR_PREFIX = "Value error, ";

const FIELD: Record<string, string> = {
  alias: "название ключа",
  expires_days: "срок действия",
  name: "имя",
  repo: "репозиторий",
  file: "файл",
  engine: "движок",
  mode: "режим",
  accel: "ускоритель",
  group_size: "размер группы",
  ctx_per_request: "контекст на запрос",
  parallel: "параллельные запросы",
  nodes: "ноды",
  spare_nodes: "резервные ноды",
  regroup_grace_s: "пауза перед пересборкой",
  schedule: "расписание",
  start: "начало окна",
  end: "конец окна",
  maintenance: "обслуживание",
  model: "модель",
  messages: "сообщения",
  max_tokens: "максимум токенов",
  days: "число дней",
  lines: "число строк",
  deployment_id: "развёртывание",
};

function fieldName(loc: ValidationItem["loc"]): string {
  const name = [...(loc ?? [])].reverse().find((p) => typeof p === "string" && p !== "body" && p !== "query");
  if (typeof name !== "string") return "";
  return FIELD[name] ?? name;
}

export function validationMessage(item: ValidationItem): string {
  const field = fieldName(item.loc);
  const t = item.type ?? "";
  if (!field) return "Проверьте введённые данные";
  if (t === "value_error" && item.msg?.startsWith(VALUE_ERROR_PREFIX)) {
    const text = item.msg.slice(VALUE_ERROR_PREFIX.length);
    if (/[а-яё]/i.test(text)) return text;
  }
  if (t === "missing") return `Заполните поле «${field}»`;
  if (t === "string_too_short" || t === "too_short") return `Поле «${field}» слишком короткое`;
  if (t === "string_too_long" || t === "too_long") return `Поле «${field}» слишком длинное`;
  if (t === "string_pattern_mismatch") return `Поле «${field}» заполнено в неверном формате`;
  if (t.startsWith("int_") || t === "float_parsing") return `Поле «${field}» должно быть числом`;
  if (/^(greater|less)_than/.test(t)) return `Значение поля «${field}» вне допустимых пределов`;
  return `Проверьте поле «${field}»`;
}

export function validationDetail(items: ValidationItem[]): string {
  const seen = new Set<string>();
  for (const it of items) seen.add(validationMessage(it));
  return [...seen].join("; ");
}
