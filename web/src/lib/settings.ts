import type { SystemSetting } from "../types";
import { fmtInt } from "./format";

export type Parsed = { ok: true; value: number | boolean } | { ok: false; error: string };

const NUMBER_RE = /^-?\d+([.,]\d+)?$/;

const num = (n: number) => String(n);

/**
 * The text of a number field as a value of the setting, with the rules and the Russian messages of the portal
 * (app/settings_store.py, SettingSpec.coerce): a number, whole for `int`, within min..max.
 */
export function parseNumber(s: SystemSetting, text: string): Parsed {
  const t = text.trim();
  if (!NUMBER_RE.test(t)) return { ok: false, error: `«${s.key}»: нужно число` };
  const value = Number(t.replace(",", "."));
  if (!Number.isFinite(value)) return { ok: false, error: `«${s.key}»: нужно конечное число` };
  if (s.type === "int" && !Number.isInteger(value)) return { ok: false, error: `«${s.key}»: нужно целое число` };
  if (s.min !== null && value < s.min) return { ok: false, error: `«${s.key}»: не меньше ${num(s.min)}` };
  if (s.max !== null && value > s.max) return { ok: false, error: `«${s.key}»: не больше ${num(s.max)}` };
  return { ok: true, value };
}

/** A value as the page prints it: «100 000», «60», «включено» */
export function fmtSetting(value: number | boolean): string {
  if (typeof value === "boolean") return value ? "включено" : "выключено";
  return fmtInt(value);
}

/** The text a number field starts with: plain digits, no grouping */
export function fieldText(value: number | boolean): string {
  return typeof value === "boolean" ? "" : String(value);
}

/** The sections of the page in the order the portal sends them */
export function groupSettings(items: SystemSetting[]): { group: string; items: SystemSetting[] }[] {
  const out: { group: string; items: SystemSetting[] }[] = [];
  for (const item of items) {
    const last = out.find((g) => g.group === item.group);
    if (last) last.items.push(item);
    else out.push({ group: item.group, items: [item] });
  }
  return out;
}
