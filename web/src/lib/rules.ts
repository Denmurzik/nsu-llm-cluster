import { fmtInt } from "./format";
import type { Accel, DeployMode } from "../types";

export interface RuleInput {
  ctx: number;
  parallel: number;
  mode: DeployMode;
  group: number;
  accel: Accel;
  maxCtx?: number;
  /** split on nvidia or any: the GPU nodes' RAM is used too (two devices per GPU node) */
  useNodeRam?: boolean;
  ramPct?: number;
}

export interface FieldErrors {
  ctx?: string;
  parallel?: string;
  group?: string;
  accel?: string;
  ramPct?: string;
}

/** devices of one split group: llama.cpp is built with 64 backends, one of them the leader's CPU */
export const MAX_SPLIT_DEVICES = 63;
export const RAM_PCT_MIN = 10;
export const RAM_PCT_MAX = 90;

/** Same limits as the portal validates, so the form explains them before the request. */
export function checkRules(i: RuleInput): FieldErrors {
  const e: FieldErrors = {};
  if (!Number.isInteger(i.ctx) || i.ctx < 256) e.ctx = "Контекст на запрос не меньше 256";
  else if (i.maxCtx && i.ctx > i.maxCtx) e.ctx = `Контекст на запрос больше максимума модели: ${fmtInt(i.maxCtx)}`;
  if (!Number.isInteger(i.parallel) || i.parallel < 1 || i.parallel > 16) e.parallel = "Параллельность: от 1 до 16";
  if (i.mode === "split") {
    // accel any: GPU and CPU nodes in one group, any member may be a GPU node with two devices
    const perNode = i.accel !== "cpu" && i.useNodeRam !== false ? 2 : 1;
    if (!Number.isInteger(i.group) || i.group < 2) e.group = "Размер группы не меньше 2";
    else if (i.group * perNode > MAX_SPLIT_DEVICES)
      e.group =
        `В группе не больше ${MAX_SPLIT_DEVICES} устройств: не больше ${Math.floor(MAX_SPLIT_DEVICES / perNode)} нод` +
        (perNode > 1 ? " при использовании RAM нод" : "");
  }
  if (i.ramPct !== undefined && (!Number.isInteger(i.ramPct) || i.ramPct < RAM_PCT_MIN || i.ramPct > RAM_PCT_MAX))
    e.ramPct = `Доля RAM ноды для модели от ${RAM_PCT_MIN} до ${RAM_PCT_MAX} %`;
  return e;
}

export const hasErrors = (e: FieldErrors | OperationErrors) => Object.keys(e).length > 0;

export interface OperationInput {
  spare: string;
  grace: string;
  /** null: around the clock */
  schedule: { days: number[]; start: string; end: string } | null;
}

export interface OperationErrors {
  spare?: string;
  grace?: string;
  schedule?: string;
}

const TIME = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Same limits as the portal: spare nodes, regroup pause (empty: the default of the mode) and the working window. */
export function checkOperation(i: OperationInput): OperationErrors {
  const e: OperationErrors = {};
  const spare = Number(i.spare);
  if (i.spare.trim() === "" || !Number.isInteger(spare) || spare < 0 || spare > 100) e.spare = "Резервных нод от 0 до 100";
  const grace = Number(i.grace);
  if (i.grace.trim() !== "" && (!Number.isInteger(grace) || grace < 0 || grace > 86400))
    e.grace = "Пауза перед пересборкой от 0 до 86400 секунд";
  if (i.schedule) {
    if (i.schedule.days.length === 0) e.schedule = "Выберите хотя бы один день расписания";
    else if (!TIME.test(i.schedule.start) || !(TIME.test(i.schedule.end) || i.schedule.end === "24:00"))
      e.schedule = "Время расписания указывается как ЧЧ:ММ";
    else if (i.schedule.start === i.schedule.end) e.schedule = "Начало и конец окна расписания совпадают";
  }
  return e;
}
