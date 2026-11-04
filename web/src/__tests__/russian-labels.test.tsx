import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { ApiError, api } from "../api";
import { phaseLabel, roleLabel } from "../lib/labels";
import { validationMessage } from "../lib/validation";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

describe("pod labels", () => {
  it("translates roles and phases and keeps unknown values", () => {
    expect([roleLabel("inference"), roleLabel("rpc"), roleLabel("split-leader")]).toEqual([
      "Инференс",
      "RPC-сервер",
      "Лидер группы",
    ]);
    expect(phaseLabel("Running")).toBe("Работает");
    expect(phaseLabel("Pending")).toBe("Ожидает");
    expect(phaseLabel("Failed")).toBe("Ошибка");
    expect(roleLabel("custom")).toBe("custom");
    expect(phaseLabel("Terminating")).toBe("Terminating");
  });

  it("shows them in the status table", async () => {
    renderApp("/admin/deployments/1", "admin");
    const table = await screen.findByRole("table", { name: "Поды развёртывания" });
    const row = await within(table).findByRole("row", { name: /llm-gemma-4-e4b-cpu-m4d9q/ });
    expect(within(row).getByText("Инференс")).toBeInTheDocument();
    expect(within(row).getByText("Работает")).toBeInTheDocument();
    expect(within(row).queryByText("inference")).not.toBeInTheDocument();
  });
});

describe("validation messages", () => {
  it("maps pydantic items to Russian", () => {
    expect(validationMessage({ loc: ["body", "alias"], type: "missing", msg: "Field required" })).toBe(
      "Заполните поле «название ключа»",
    );
    expect(validationMessage({ loc: ["body", "alias"], type: "string_too_long" })).toBe(
      "Поле «название ключа» слишком длинное",
    );
    expect(validationMessage({ loc: ["query", "days"], type: "int_parsing" })).toBe("Поле «число дней» должно быть числом");
    expect(validationMessage({ loc: ["body", "parallel"], type: "less_than_equal" })).toBe(
      "Значение поля «параллельные запросы» вне допустимых пределов",
    );
  });

  it("falls back to a generic sentence naming the field", () => {
    expect(validationMessage({ loc: ["body", "weird_field"], type: "something_new", msg: "English" })).toBe(
      "Проверьте поле «weird_field»",
    );
    expect(validationMessage({ type: "x" })).toBe("Проверьте введённые данные");
  });

  it("an API 422 with an item list reaches the caller in Russian", async () => {
    server.use(
      http.post("/api/keys", () =>
        HttpResponse.json(
          {
            detail: [
              { type: "string_too_long", loc: ["body", "alias"], msg: "String should have at most 40 characters" },
              { type: "missing", loc: ["body", "alias"], msg: "Field required" },
              { type: "weird", loc: ["body", "expires_days"], msg: "Input should be a valid integer" },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    const err = (await api.createKey("x", null).catch((e) => e)) as ApiError;
    expect(err.status).toBe(422);
    expect(err.detail).toBe(
      "Поле «название ключа» слишком длинное; Заполните поле «название ключа»; Проверьте поле «срок действия»",
    );
    expect(err.detail).not.toMatch(/[A-Za-z]{4,}/);
  });

  it("a Russian string detail is passed through", async () => {
    server.use(http.post("/api/keys", () => HttpResponse.json({ detail: "Репозиторий указывается как владелец/название" }, { status: 422 })));
    const err = (await api.createKey("x", null).catch((e) => e)) as ApiError;
    expect(err.detail).toBe("Репозиторий указывается как владелец/название");
  });
});

describe("pydantic value errors", () => {
  it("shows a Russian value_error text without the English prefix", () => {
    expect(
      validationMessage({
        loc: ["body", "alias"],
        type: "value_error",
        msg: "Value error, Название не может начинаться с «__»",
      }),
    ).toBe("Название не может начинаться с «__»");
  });

  it("keeps the generic sentence for an English value_error", () => {
    expect(validationMessage({ loc: ["body", "alias"], type: "value_error", msg: "Value error, bad" })).toBe(
      "Проверьте поле «название ключа»",
    );
  });
});
