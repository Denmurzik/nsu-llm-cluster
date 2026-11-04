import { describe, expect, it } from "vitest";
import { checkOperation } from "../lib/rules";

const ok = { spare: "0", grace: "", schedule: null };

describe("checkOperation", () => {
  it("accepts the defaults and an end of the day", () => {
    expect(checkOperation(ok)).toEqual({});
    expect(checkOperation({ ...ok, schedule: { days: [6, 7], start: "00:00", end: "24:00" } })).toEqual({});
  });

  it("uses the portal messages", () => {
    expect(checkOperation({ ...ok, spare: "101" }).spare).toBe("Резервных нод от 0 до 100");
    expect(checkOperation({ ...ok, grace: "-1" }).grace).toBe("Пауза перед пересборкой от 0 до 86400 секунд");
    expect(checkOperation({ ...ok, schedule: { days: [], start: "08:00", end: "20:00" } }).schedule).toBe(
      "Выберите хотя бы один день расписания",
    );
    expect(checkOperation({ ...ok, schedule: { days: [1], start: "24:00", end: "20:00" } }).schedule).toBe(
      "Время расписания указывается как ЧЧ:ММ",
    );
    expect(checkOperation({ ...ok, schedule: { days: [1], start: "08:00", end: "08:00" } }).schedule).toBe(
      "Начало и конец окна расписания совпадают",
    );
  });
});
