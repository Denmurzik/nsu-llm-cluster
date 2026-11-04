import { fmtBytes, fmtInt, fmtPercent, suggestName, NAME_RE, ALIAS_RE } from "../lib/format";
import { loadLabel } from "../lib/labels";
import type { LoadStage } from "../types";

const norm = (s: string) => s.replace(/\s/g, " ");

describe("format", () => {
  it("formats bytes with a decimal comma and Russian units", () => {
    expect(norm(fmtBytes(5046586573))).toBe("4,7 ГБ");
    expect(norm(fmtBytes(512 * 2 ** 20))).toBe("512 МБ");
    expect(norm(fmtBytes(1536))).toBe("2 КБ");
    expect(norm(fmtBytes(12))).toBe("12 Б");
  });

  it("groups thousands with a space", () => {
    expect(norm(fmtInt(131072))).toBe("131 072");
  });

  it("suggests a valid deployment name from a repo", () => {
    expect(suggestName("unsloth/gemma-4-E4B-it-GGUF")).toBe("gemma-4-e4b-it");
    expect(NAME_RE.test(suggestName("ggml-org/gemma-3-270m-it-GGUF"))).toBe(true);
    expect(suggestName("a/" + "x".repeat(80)).length).toBeLessThanOrEqual(40);
  });

  it("validates key aliases like the server", () => {
    expect(ALIAS_RE.test("курсовая работа-2")).toBe(true);
    expect(ALIAS_RE.test("плохое/имя")).toBe(false);
    expect(ALIAS_RE.test("")).toBe(false);
    expect(ALIAS_RE.test("a".repeat(41))).toBe(false);
  });
});

describe("load labels", () => {
  it("names each stage and formats the share", () => {
    const p = (stage: LoadStage, percent: number | null = null) => ({ stage, percent, done_bytes: null, total_bytes: null });
    expect(norm(fmtPercent(42.5))).toBe("42,5 %");
    expect(norm(fmtPercent(7))).toBe("7 %");
    expect(norm(loadLabel(p("downloading", 42.5)))).toBe("Скачивание 42,5 %");
    expect(loadLabel(p("downloading"))).toBe("Скачивание");
    expect(loadLabel(p("loading"))).toBe("Загрузка весов");
    expect(loadLabel(p("starting"))).toBe("Запуск");
    expect(loadLabel(p("ready"))).toBe("Готов");
  });
});
