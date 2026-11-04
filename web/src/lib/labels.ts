import type { Accel, DeployMode, Engine, LoadProgress, ModelStatus } from "../types";
import { fmtPercent } from "./format";

export const STATUS_LABEL: Record<ModelStatus, string> = {
  ready: "Готова",
  starting: "Запускается",
  down: "Недоступна",
  unknown: "Нет данных",
};

const ROLE_LABEL: Record<string, string> = {
  inference: "Инференс",
  rpc: "RPC-сервер",
  "split-leader": "Лидер группы",
};

const PHASE_LABEL: Record<string, string> = {
  Pending: "Ожидает",
  Running: "Работает",
  Succeeded: "Завершён",
  Failed: "Ошибка",
  Unknown: "Неизвестно",
};

/** Role of a pod (`llm.nsu/role`); an unknown value is shown as it is. */
export const roleLabel = (role: string): string => ROLE_LABEL[role] ?? role;
/** Kubernetes pod phase; an unknown value is shown as it is. */
export const phaseLabel = (phase: string): string => PHASE_LABEL[phase] ?? phase;

/** «Скачивание 42,5 %», «Загрузка весов», «Готов». */
export function loadLabel(progress: LoadProgress): string {
  switch (progress.stage) {
    case "downloading":
      return progress.percent === null ? "Скачивание" : `Скачивание ${fmtPercent(progress.percent)}`;
    case "loading":
      return "Загрузка весов";
    case "ready":
      return "Готов";
    default:
      return "Запуск";
  }
}

export const ENGINE_LABEL: Record<Engine, string> = {
  llamacpp: "llama.cpp",
  vllm: "vLLM",
};

export function modeLabel(mode: DeployMode, groupSize: number): string {
  return mode === "split" ? `Разделение на ${groupSize} ${plural(groupSize, ["ноду", "ноды", "нод"])}` : "Реплики";
}

export const ACCEL_LABEL: Record<Accel, string> = {
  any: "любой",
  cpu: "cpu",
  nvidia: "nvidia",
};

/** Russian plural form: [1 штука, 2 штуки, 5 штук]. */
export function plural(n: number, forms: [string, string, string]): string {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return forms[2];
  if (b > 1 && b < 5) return forms[1];
  if (b === 1) return forms[0];
  return forms[2];
}
