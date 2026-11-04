import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { fixtures, state } from "../mocks/handlers";
import { renderApp } from "../test/render";

const FULL_KEY = fixtures.createdKey.key;

describe("keys", () => {
  it("lists keys with a masked value and usage", async () => {
    renderApp("/keys");
    const row = await screen.findByRole("row", { name: /курсовая/ });
    expect(within(row).getByText("sk-…q7Zd")).toBeInTheDocument();
    expect(within(row).getByText("412")).toBeInTheDocument();
    expect(screen.queryByText(FULL_KEY)).not.toBeInTheDocument();
  });

  it("notes that usage is unavailable when the gateway degrades", async () => {
    server.use(http.get("/api/keys", () => HttpResponse.json(fixtures.keys, { headers: { "X-Usage-Unavailable": "1" } })));
    renderApp("/keys");
    expect(await screen.findByText("Статистика использования сейчас недоступна")).toBeInTheDocument();
  });

  it("creates a key with CSRF, shows it once and forgets it after closing", async () => {
    let seen: { csrf: string | null; body: unknown } | null = null;
    server.use(
      http.post("/api/keys", async ({ request }) => {
        seen = { csrf: request.headers.get("X-CSRF-Token"), body: await request.json() };
        return HttpResponse.json(fixtures.createdKey, { status: 201 });
      }),
    );
    const { user } = renderApp("/keys");
    await screen.findByRole("row", { name: /курсовая/ });

    await user.type(screen.getByLabelText("Название ключа"), "диплом");
    await user.selectOptions(screen.getByLabelText("Срок действия"), "90");
    await user.click(screen.getByRole("button", { name: "Создать ключ" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Ключ показывается один раз")).toBeInTheDocument();
    expect(within(dialog).getByText(FULL_KEY)).toBeInTheDocument();
    expect(seen).toEqual({ csrf: "csrf-test-token-user", body: { alias: "диплом", expires_days: 90 } });

    await user.click(within(dialog).getByRole("button", { name: "Копировать" }));
    expect(await navigator.clipboard.readText()).toBe(FULL_KEY);
    expect(await within(dialog).findByRole("button", { name: "Скопировано" })).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Я сохранил ключ" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByText(FULL_KEY)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(FULL_KEY);
    expect(screen.getByLabelText("Название ключа")).toHaveValue("");
  });

  async function openCreatedDialog() {
    server.use(http.post("/api/keys", () => HttpResponse.json(fixtures.createdKey, { status: 201 })));
    const view = renderApp("/keys");
    await screen.findByRole("row", { name: /курсовая/ });
    await view.user.type(screen.getByLabelText("Название ключа"), "диплом");
    await view.user.click(screen.getByRole("button", { name: "Создать ключ" }));
    return { ...view, dialog: await screen.findByRole("dialog") };
  }

  it("asks before Escape closes the one-time key dialog", async () => {
    const { user, dialog } = await openCreatedDialog();
    await user.keyboard("{Escape}");
    expect(await within(dialog).findByText("Ключ больше не будет показан. Закрыть?")).toBeInTheDocument();
    expect(within(dialog).getByText(FULL_KEY)).toBeInTheDocument(); // still on screen

    await user.keyboard("{Escape}"); // a second Escape does not dismiss it either
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Вернуться к ключу" }));
    expect(within(dialog).queryByText("Ключ больше не будет показан. Закрыть?")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Я сохранил ключ" })).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await user.click(await within(dialog).findByRole("button", { name: "Закрыть" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.body.textContent).not.toContain(FULL_KEY);
  });

  it("shows an error state when copying the key fails", async () => {
    const { user, dialog } = await openCreatedDialog();
    const original = navigator.clipboard.writeText;
    const exec = document.execCommand;
    navigator.clipboard.writeText = () => Promise.reject(new Error("denied"));
    document.execCommand = () => false;
    try {
      await user.click(within(dialog).getByRole("button", { name: "Копировать" }));
      expect(await within(dialog).findByRole("button", { name: "Не удалось скопировать" })).toBeInTheDocument();
      expect(within(dialog).queryByRole("button", { name: "Скопировано" })).not.toBeInTheDocument();
    } finally {
      navigator.clipboard.writeText = original;
      document.execCommand = exec;
    }
  });

  it("sends null expiry for a key without a term", async () => {
    let body: unknown = null;
    server.use(
      http.post("/api/keys", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(fixtures.createdKey, { status: 201 });
      }),
    );
    const { user } = renderApp("/keys");
    await screen.findByRole("row", { name: /курсовая/ });
    await user.type(screen.getByLabelText("Название ключа"), "бессрочный");
    await user.click(screen.getByRole("button", { name: "Создать ключ" }));
    await screen.findByRole("dialog");
    expect(body).toEqual({ alias: "бессрочный", expires_days: null });
  });

  it("shows the 409 detail from the server", async () => {
    const { user } = renderApp("/keys");
    await screen.findByRole("row", { name: /курсовая/ });
    await user.type(screen.getByLabelText("Название ключа"), "курсовая");
    await user.click(screen.getByRole("button", { name: "Создать ключ" }));
    expect(await screen.findByText("Ключ с таким названием уже есть")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows the gateway error when LiteLLM is down", async () => {
    server.use(http.post("/api/keys", () => HttpResponse.json({ detail: "Шлюз моделей недоступен" }, { status: 503 })));
    const { user } = renderApp("/keys");
    await screen.findByRole("row", { name: /курсовая/ });
    await user.type(screen.getByLabelText("Название ключа"), "новый");
    await user.click(screen.getByRole("button", { name: "Создать ключ" }));
    expect(await screen.findByText("Шлюз моделей недоступен")).toBeInTheDocument();
  });

  it("rejects an invalid alias before sending", async () => {
    const { user } = renderApp("/keys");
    await screen.findByRole("row", { name: /курсовая/ });
    await user.type(screen.getByLabelText("Название ключа"), "плохое/имя");
    expect(screen.getByRole("button", { name: "Создать ключ" })).toBeDisabled();
    expect(screen.getByText("Допустимы буквы, цифры, пробел, дефис и подчёркивание")).toBeInTheDocument();
  });

  it("rejects an alias that starts with two underscores and says why", async () => {
    const { user } = renderApp("/keys");
    await screen.findByRole("row", { name: /курсовая/ });
    await user.type(screen.getByLabelText("Название ключа"), "__служебный");
    expect(screen.getByRole("button", { name: "Создать ключ" })).toBeDisabled();
    expect(screen.getByText("Название не может начинаться с «__»")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Название ключа"));
    await user.type(screen.getByLabelText("Название ключа"), "_один");
    expect(screen.getByRole("button", { name: "Создать ключ" })).toBeEnabled();
  });

  it("deletes a key after confirmation", async () => {
    let deleted: { id: string; csrf: string | null } | null = null;
    server.use(
      http.delete("/api/keys/:id", ({ request, params }) => {
        deleted = { id: String(params.id), csrf: request.headers.get("X-CSRF-Token") };
        state.keys = state.keys.filter((k) => k.id !== String(params.id));
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { user } = renderApp("/keys");
    const row = await screen.findByRole("row", { name: /курсовая/ });
    await user.click(within(row).getByRole("button", { name: "Отозвать ключ курсовая" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Отозвать ключ «курсовая»/)).toBeInTheDocument();
    expect(within(dialog).getByText(/перестанут работать в течение ~15 с/)).toBeInTheDocument();
    expect(deleted).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Отозвать" }));

    await waitFor(() => expect(screen.queryByRole("row", { name: /курсовая/ })).not.toBeInTheDocument());
    expect(deleted).toEqual({ id: "3f6a9c1e-5b0d-4c7a-9e2f-8d1b7a4c6e12", csrf: "csrf-test-token-user" });
  });

  it("keeps the key when the confirmation is cancelled", async () => {
    const { user } = renderApp("/keys");
    const row = await screen.findByRole("row", { name: /курсовая/ });
    await user.click(within(row).getByRole("button", { name: "Отозвать ключ курсовая" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Отмена" }));
    expect(screen.getByRole("row", { name: /курсовая/ })).toBeInTheDocument();
  });
});
