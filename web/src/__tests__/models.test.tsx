import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

describe("model catalog", () => {
  it("lists name, status, context per request and mode", async () => {
    renderApp("/models");
    expect(await screen.findByRole("columnheader", { name: "Контекст на запрос" })).toBeInTheDocument();

    const gemma = await screen.findByRole("row", { name: /gemma-4-e4b/ });
    expect(within(gemma).getByText("Готова")).toBeInTheDocument();
    expect(within(gemma).getByText("8 192")).toBeInTheDocument();
    expect(within(gemma).getByText("Реплики")).toBeInTheDocument();

    const qwen = screen.getByRole("row", { name: /qwen3-30b-a3b/ });
    expect(within(qwen).getByText("Запускается")).toBeInTheDocument();
    expect(within(qwen).getByText("16 384")).toBeInTheDocument();
    expect(within(qwen).getByText("Разделение на 2 ноды")).toBeInTheDocument();

    const llama = screen.getByRole("row", { name: /llama-3-1-8b/ });
    expect(within(llama).getByText("Недоступна")).toBeInTheDocument();
  });

  it("marks the unknown status when the gateway is unavailable", async () => {
    server.use(
      http.get("/api/models", () =>
        HttpResponse.json([
          {
            name: "gemma-4-e4b",
            engine: "llamacpp",
            mode: "replicas",
            group_size: 2,
            ctx_per_request: 8192,
            parallel: 2,
            ready_backends: null,
            status: "unknown",
            experimental: false,
            vision: false,
          },
        ]),
      ),
    );
    renderApp("/models");
    const row = await screen.findByRole("row", { name: /gemma-4-e4b/ });
    expect(within(row).getByText("Нет данных")).toBeInTheDocument();
  });

  it("explains an empty catalog", async () => {
    server.use(http.get("/api/models", () => HttpResponse.json([])));
    renderApp("/models");
    expect(await screen.findByText("Моделей пока нет")).toBeInTheDocument();
  });

  it("marks an experimental model for an admin and shows no mark on ordinary ones", async () => {
    const base = {
      engine: "llamacpp",
      mode: "replicas",
      group_size: 2,
      ctx_per_request: 8192,
      parallel: 2,
      ready_backends: 1,
      status: "ready",
      vision: false,
    };
    server.use(
      http.get("/api/models", () =>
        HttpResponse.json([
          { ...base, name: "main-model", experimental: false },
          { ...base, name: "try-model", experimental: true },
        ]),
      ),
    );
    renderApp("/models", "admin");
    const exp = await screen.findByRole("row", { name: /try-model/ });
    expect(within(exp).getByText("экспериментальная")).toBeInTheDocument();
    expect(within(screen.getByRole("row", { name: /main-model/ })).queryByText("экспериментальная")).toBeNull();
  });
});
