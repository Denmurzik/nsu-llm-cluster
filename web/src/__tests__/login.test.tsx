import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

describe("login screen", () => {
  it("offers the login with return_to when /api/me answers 401", async () => {
    renderApp("/models", null);
    const link = await screen.findByRole("link", { name: "Войти через SSO" });
    expect(link).toHaveAttribute("href", "/auth/login?return_to=%2Fmodels");
  });

  it("names the provider from LOGIN_PROVIDER_NAME on the button and in the text", async () => {
    server.use(
      http.get("/api/public/status", () => HttpResponse.json({ login_provider: "NSU ID", models: [] })),
    );
    renderApp("/models", null);
    expect(await screen.findByRole("link", { name: "Войти через NSU ID" })).toBeInTheDocument();
    expect(screen.getByText(/Войдите через NSU ID, выпустите ключ/)).toBeInTheDocument();
    expect(screen.queryByText(/НГУ/)).not.toBeInTheDocument();
  });

  it("keeps the query string in return_to", async () => {
    renderApp("/playground?model=gemma-4-e4b", null);
    const link = await screen.findByRole("link", { name: "Войти через SSO" });
    expect(link).toHaveAttribute("href", "/auth/login?return_to=%2Fplayground%3Fmodel%3Dgemma-4-e4b");
  });

  it("shows live model status from /api/public/status", async () => {
    renderApp("/", null);
    const list = await screen.findByRole("list", { name: "Статус моделей" });
    const ready = await within(list).findByRole("listitem", { name: /gemma-4-e4b/ });
    expect(ready).toHaveTextContent("Готова");
    expect(ready).toHaveTextContent("3");
    expect(within(list).getByRole("listitem", { name: /qwen3-30b-a3b/ })).toHaveTextContent("Запускается");
    expect(within(list).getByRole("listitem", { name: /llama-3-1-8b/ })).toHaveTextContent("Недоступна");
  });

  it("still renders the login when the public status is unavailable", async () => {
    server.use(http.get("/api/public/status", () => HttpResponse.json({ detail: "x" }, { status: 500 })));
    renderApp("/", null);
    expect(await screen.findByRole("link", { name: "Войти через SSO" })).toBeInTheDocument();
    expect(await screen.findByText("Статус моделей сейчас недоступен")).toBeInTheDocument();
  });

  it("does not show the login for a signed in user", async () => {
    renderApp("/models", "user");
    expect(await screen.findByRole("heading", { name: "Модели" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Войти через SSO" })).not.toBeInTheDocument();
  });
});
