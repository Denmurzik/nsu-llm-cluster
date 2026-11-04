const NBSP = " ";
const GB = 2 ** 30;
const MB = 2 ** 20;
const KB = 2 ** 10;

const intFmt = new Intl.NumberFormat("ru-RU");

export function fmtInt(n: number): string {
  return intFmt.format(n);
}

/** 5 046 586 573 -> «4,7 ГБ»: one decimal for gigabytes, whole megabytes. */
export function fmtBytes(bytes: number): string {
  if (bytes >= GB) return `${(bytes / GB).toFixed(1).replace(".", ",")}${NBSP}ГБ`;
  if (bytes >= MB) return `${Math.round(bytes / MB)}${NBSP}МБ`;
  if (bytes >= KB) return `${Math.round(bytes / KB)}${NBSP}КБ`;
  return `${bytes}${NBSP}Б`;
}

const percentFmt = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });

/** 42.5 -> «42,5 %». */
export function fmtPercent(percent: number): string {
  return `${percentFmt.format(percent)}${NBSP}%`;
}

export function fmtMib(mib: number | null | undefined): string {
  if (mib === null || mib === undefined) return "—";
  return fmtBytes(mib * MB);
}

const dateFmt = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
const dateTimeFmt = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});
const dayFmt = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit" });

export function fmtDate(iso: string | null | undefined): string {
  return iso ? dateFmt.format(new Date(iso)) : "—";
}

export function fmtDateTime(iso: string | null | undefined): string {
  return iso ? dateTimeFmt.format(new Date(iso)) : "—";
}

/** "2026-11-02" -> "02.11" */
export function fmtDay(day: string): string {
  return dayFmt.format(new Date(`${day}T12:00:00`));
}

export const NAME_RE = /^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$/;
export const ALIAS_RE = /^[\p{L}\p{N}_ -]{1,40}$/u;
/** Aliases starting with two underscores are reserved for service keys (the portal rejects them). */
export const ALIAS_RESERVED_PREFIX = "__";

/** unsloth/gemma-4-E4B-it-GGUF -> gemma-4-e4b-it */
export function suggestName(repo: string): string {
  const base = repo.split("/").pop() ?? "";
  return base
    .toLowerCase()
    .replace(/-gguf$/, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40)
    .replace(/-+$/g, "");
}
