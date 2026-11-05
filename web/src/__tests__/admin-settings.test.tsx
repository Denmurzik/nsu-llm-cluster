import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { fmtSetting, groupSettings, parseNumber } from "../lib/settings";
import { fixtures, state } from "../mocks/handlers";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";
import type { SystemSetting } from "../types";

const items = fixtures.settings as unknown as SystemSetting[];
const spec = (key: string) => items.find((s) => s.key === key)!;
const stored = (key: string) => state.settings.find((s) => s.key === key)!;

const row = async (key: string) => within(await screen.findByRole("group", { name: key }));

describe("the rules of the fields are the portal rules", () => {
  it.each([
    ["key_rpm_limit", "120", 120],
    ["key_rpm_limit", " 7 ", 7],
    ["wol_delay_s", "2,5", 2.5],
    ["wol_delay_s", "0", 0],
  ])("%s accepts %s", (key, text, value) => {
    expect(parseNumber(spec(key), text)).toEqual({ ok: true, value });
  });

  it.each([
    ["key_rpm_limit", "", "«key_rpm_limit»: нужно число"],
    ["key_rpm_limit", "много", "«key_rpm_limit»: нужно число"],
    ["key_rpm_limit", "1e3", "«key_rpm_limit»: нужно число"],
    ["key_rpm_limit", "2.5", "«key_rpm_limit»: нужно целое число"],
    ["key_rpm_limit", "0", "«key_rpm_limit»: не меньше 1"],
    ["key_rpm_limit", "1000001", "«key_rpm_limit»: не больше 1000000"],
    ["wol_delay_s", "-1", "«wol_delay_s»: не меньше 0"],
    ["wol_max_tries", "101", "«wol_max_tries»: не больше 100"],
    ["model_cache_idle_s", "59", "«model_cache_idle_s»: не меньше 60"],
    ["metrics_retention_days", "366", "«metrics_retention_days»: не больше 365"],
  ])("%s refuses «%s»", (key, text, error) => {
    expect(parseNumber(spec(key), text)).toEqual({ ok: false, error });
  });

  it("prints values in the house format and keeps the order of the sections", () => {
    expect(fmtSetting(100000)).toBe("100 000");
    expect(fmtSetting(true)).toBe("включено");
    expect(fmtSetting(false)).toBe("выключено");
    expect(groupSettings(items).map((g) => g.group)).toEqual([
      "Ключи и лимиты",
      "Ноды и Wake-on-LAN",
      "Модели и кэш",
      "Мониторинг",
    ]);
  });
});

describe("the settings page", () => {
  it("is in the admin menu and for admins only", async () => {
    const { user } = renderApp("/admin/nodes", "admin");
    await user.click(await screen.findByRole("link", { name: "Настройки" }));
    expect(await screen.findByRole("heading", { name: "Настройки", level: 1 })).toBeInTheDocument();
  });

  it("refuses a regular user", async () => {
    renderApp("/admin/settings", "user");
    expect(await screen.findByText("Недостаточно прав")).toBeInTheDocument();
  });

  it("groups the settings, with the Russian description, the key and the .env default", async () => {
    renderApp("/admin/settings", "admin");
    for (const name of ["Ключи и лимиты", "Ноды и Wake-on-LAN", "Модели и кэш", "Мониторинг"]) {
      expect(await screen.findByRole("region", { name })).toBeInTheDocument();
    }
    const keys = within(screen.getByRole("region", { name: "Ключи и лимиты" }));
    expect(keys.getAllByRole("group").map((g) => g.getAttribute("aria-label"))).toEqual([
      "key_rpm_limit",
      "key_tpm_limit",
      "max_keys_per_user",
    ]);
    const rpm = await row("key_rpm_limit");
    expect(rpm.getByText(/Запросов в минуту на один ключ/)).toBeInTheDocument();
    expect(rpm.getByText("Из .env")).toBeInTheDocument();
    expect(rpm.getByText("По умолчанию из .env: 60")).toBeInTheDocument();
    expect(rpm.getByLabelText(/Запросов в минуту/)).toHaveValue("60");
    expect((await row("wol_enabled")).getByRole("checkbox")).toBeChecked();
  });

  it("saves one field, shows the stored state and undoes the save", async () => {
    const { user } = renderApp("/admin/settings", "admin");
    const rpm = await row("key_rpm_limit");
    const input = rpm.getByLabelText(/Запросов в минуту/);
    expect(rpm.getByRole("button", { name: "Сохранить" })).toBeDisabled();
    await user.clear(input);
    await user.type(input, "120");
    await user.click(rpm.getByRole("button", { name: "Сохранить" }));
    expect(await rpm.findByText("Сохранено")).toBeInTheDocument();
    expect(rpm.getByText("Изменено в панели")).toBeInTheDocument();
    expect(stored("key_rpm_limit").value).toBe(120);
    // the other fields are untouched
    expect((await row("key_tpm_limit")).getByText("Из .env")).toBeInTheDocument();

    await user.click(rpm.getByRole("button", { name: "Вернуть прежнее" }));
    expect(await rpm.findByText("Возвращено прежнее значение")).toBeInTheDocument();
    expect(rpm.getByText("Из .env")).toBeInTheDocument();
    expect(input).toHaveValue("60");
    expect(stored("key_rpm_limit").overridden).toBe(false);
  });

  it("checks a value like the server does and keeps the save button off", async () => {
    const { user } = renderApp("/admin/settings", "admin");
    const rpm = await row("key_rpm_limit");
    const input = rpm.getByLabelText(/Запросов в минуту/);
    await user.clear(input);
    await user.type(input, "0");
    expect(rpm.getByText("«key_rpm_limit»: не меньше 1")).toBeInTheDocument();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(rpm.getByRole("button", { name: "Сохранить" })).toBeDisabled();
    await user.clear(input);
    await user.type(input, "2.5");
    expect(rpm.getByText("«key_rpm_limit»: нужно целое число")).toBeInTheDocument();
    expect(stored("key_rpm_limit").overridden).toBe(false);
  });

  it("drops an edit that is not saved", async () => {
    const { user } = renderApp("/admin/settings", "admin");
    const tries = await row("wol_max_tries");
    const input = tries.getByLabelText(/Сколько раз автоматически будить/);
    await user.clear(input);
    await user.type(input, "9");
    await user.click(tries.getByRole("button", { name: "Отменить" }));
    expect(input).toHaveValue("6");
    expect(tries.queryByRole("button", { name: "Отменить" })).not.toBeInTheDocument();
  });

  it("saves with Enter and drops the edit with Escape", async () => {
    const { user } = renderApp("/admin/settings", "admin");
    const retention = await row("metrics_retention_days");
    const input = retention.getByLabelText(/Сколько дней хранить замеры/);
    await user.clear(input);
    await user.type(input, "14");
    await user.keyboard("{Escape}");
    expect(input).toHaveValue("7");
    await user.type(input, "{Backspace}30{Enter}");
    expect(await retention.findByText("Сохранено")).toBeInTheDocument();
    expect(stored("metrics_retention_days").value).toBe(30);
  });

  it("switches a yes/no setting and resets it to the .env value", async () => {
    const { user } = renderApp("/admin/settings", "admin");
    const wol = await row("wol_enabled");
    await user.click(wol.getByRole("checkbox"));
    expect(wol.getByText("Выключено")).toBeInTheDocument();
    await user.click(wol.getByRole("button", { name: "Сохранить" }));
    expect(await wol.findByText("Изменено в панели")).toBeInTheDocument();
    expect(wol.getByRole("checkbox")).not.toBeChecked();

    await user.click(wol.getByRole("button", { name: "Сбросить к .env" }));
    expect(await wol.findByText("Сброшено к значению из .env")).toBeInTheDocument();
    expect(wol.getByText("Из .env")).toBeInTheDocument();
    expect(wol.getByRole("checkbox")).toBeChecked();
    // a reset can be undone too
    await user.click(wol.getByRole("button", { name: "Вернуть прежнее" }));
    expect(await wol.findByText("Возвращено прежнее значение")).toBeInTheDocument();
    expect(wol.getByRole("checkbox")).not.toBeChecked();
    expect(wol.getByText("Изменено в панели")).toBeInTheDocument();
  });

  it("shows the refusal of the server as it is", async () => {
    server.use(
      http.put("/api/admin/settings/:key", () =>
        HttpResponse.json({ detail: "«key_rpm_limit»: не меньше 1" }, { status: 422 }),
      ),
    );
    const { user } = renderApp("/admin/settings", "admin");
    const rpm = await row("key_rpm_limit");
    const input = rpm.getByLabelText(/Запросов в минуту/);
    await user.clear(input);
    await user.type(input, "5");
    await user.click(rpm.getByRole("button", { name: "Сохранить" }));
    expect(await rpm.findByRole("alert")).toHaveTextContent("«key_rpm_limit»: не меньше 1");
    expect(input).toHaveValue("5");
  });

  it("marks the mirror limit as waiting for a restart and restarts the mirror after a confirmation", async () => {
    const { user } = renderApp("/admin/settings", "admin");
    const mirror = await row("hf_mirror_max_gib");
    expect(mirror.getByText("Зеркало читает значение при запуске")).toBeInTheDocument();
    expect(mirror.queryByRole("button", { name: "Перезапустить зеркало" })).not.toBeInTheDocument();
    const input = mirror.getByLabelText(/Предел кэша зеркала/);
    await user.clear(input);
    await user.type(input, "800");
    await user.click(mirror.getByRole("button", { name: "Сохранить" }));
    expect(await mirror.findByText("Применится после перезапуска зеркала")).toBeInTheDocument();

    await user.click(mirror.getByRole("button", { name: "Перезапустить зеркало" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Перезапуск зеркала" }));
    await user.click(dialog.getByRole("button", { name: "Отмена" }));
    expect(state.mirrorApplied).toBe(500);

    await user.click(mirror.getByRole("button", { name: "Перезапустить зеркало" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Перезапустить" }));
    await waitFor(() => expect(state.mirrorApplied).toBe(800));
    expect(await screen.findByText(/Зеркало перезапускается/)).toBeInTheDocument();
    await waitFor(() => expect(mirror.queryByText("Применится после перезапуска зеркала")).not.toBeInTheDocument());
    expect(mirror.getByText("Зеркало читает значение при запуске")).toBeInTheDocument();
  });

  it("says why a restart failed and keeps the marker", async () => {
    server.use(
      http.post("/api/admin/hf-mirror/restart", () => HttpResponse.json({ detail: "Кластер недоступен" }, { status: 503 })),
    );
    const { user } = renderApp("/admin/settings", "admin");
    const mirror = await row("hf_mirror_max_gib");
    const input = mirror.getByLabelText(/Предел кэша зеркала/);
    await user.clear(input);
    await user.type(input, "900");
    await user.click(mirror.getByRole("button", { name: "Сохранить" }));
    await user.click(await mirror.findByRole("button", { name: "Перезапустить зеркало" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Перезапустить" }));
    expect(await screen.findByText("Кластер недоступен")).toBeInTheDocument();
    expect(mirror.getByText("Применится после перезапуска зеркала")).toBeInTheDocument();
  });

  it("applies the key limits to the existing keys as a background job and shows its progress", async () => {
    const { user } = renderApp("/admin/settings", "admin");
    const section = within(await screen.findByRole("region", { name: "Ключи и лимиты" }));
    await user.click(section.getByRole("button", { name: "Применить к существующим ключам" }));
    expect(await section.findByText(/Обновлено 0 из 4/)).toBeInTheDocument();
    expect(section.getByRole("button", { name: "Применить к существующим ключам" })).toBeDisabled();
    expect(
      await section.findByText(/Готово: обновлено 4, не удалось 0\. Лимиты: 60 запросов и 100\s000 токенов в минуту/, undefined, {
        timeout: 6000,
      }),
    ).toBeInTheDocument();
    expect(section.getByRole("button", { name: "Применить к существующим ключам" })).toBeEnabled();
  });

  it("shows a refused start", async () => {
    server.use(
      http.post("/api/admin/key-limits/apply", () =>
        HttpResponse.json({ detail: "Обновление ключей уже идёт" }, { status: 409 }),
      ),
    );
    const { user } = renderApp("/admin/settings", "admin");
    const section = within(await screen.findByRole("region", { name: "Ключи и лимиты" }));
    await user.click(section.getByRole("button", { name: "Применить к существующим ключам" }));
    expect(await section.findByText("Обновление ключей уже идёт")).toBeInTheDocument();
  });

  it("shows the last job when the page opens", async () => {
    state.keyJob = { state: "failed", total: 10, done: 3, failed: 0, error: "Шлюз моделей недоступен" };
    renderApp("/admin/settings", "admin");
    expect(await screen.findByText("Остановлено: Шлюз моделей недоступен. Обновлено 3")).toBeInTheDocument();
  });

  it("says when the list cannot be loaded", async () => {
    server.use(http.get("/api/admin/settings", () => HttpResponse.json({ detail: "Ошибка 500" }, { status: 500 })));
    renderApp("/admin/settings", "admin");
    expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка 500");
  });
});
