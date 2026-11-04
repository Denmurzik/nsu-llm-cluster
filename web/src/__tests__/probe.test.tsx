import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { probeMetrics } from "../components/ProbeCell";
import { server } from "../mocks/server";
import { fixtures } from "../mocks/handlers";
import { renderApp } from "../test/render";

const ok = { ok: true, status: 200, answer: "Москва", ttft_ms: 820, tokens_per_s: 62.4, error: null };

async function openList() {
  const view = renderApp("/admin/deployments", "admin");
  await screen.findByRole("row", { name: /gemma-4-e4b/ });
  return view;
}

describe("probe metrics", () => {
  it("formats the first token and the speed", () => {
    expect(probeMetrics({ ttft_ms: 820, tokens_per_s: 62.4 })).toEqual(["первый токен 820 мс", "62 ток/с"]);
    expect(probeMetrics({ ttft_ms: 1234, tokens_per_s: 4.76 })).toEqual(["первый токен 1 234 мс", "4,8 ток/с"]);
  });

  it("leaves out what was not measured", () => {
    expect(probeMetrics({ ttft_ms: null, tokens_per_s: null })).toEqual([]);
    expect(probeMetrics({ ttft_ms: 90, tokens_per_s: null })).toEqual(["первый токен 90 мс"]);
  });
});

describe("«Проверить» in the deployment list", () => {
  it("has a button in every row", async () => {
    await openList();
    for (const d of fixtures.deployments) {
      expect(within(screen.getByRole("row", { name: new RegExp(d.name) })).getByRole("button", { name: "Проверить" })).toBeEnabled();
    }
  });

  it("posts with CSRF and shows the answer, the first token time and the speed without touching the table", async () => {
    let seen: { id: string; csrf: string | null } | null = null;
    server.use(
      http.post("/api/admin/deployments/:id/probe", ({ params, request }) => {
        seen = { id: String(params.id), csrf: request.headers.get("X-CSRF-Token") };
        return HttpResponse.json(ok);
      }),
    );
    const { user } = await openList();
    const rowsBefore = screen.getAllByRole("row").length;
    await user.click(within(screen.getByRole("row", { name: /gemma-4-e4b/ })).getByRole("button", { name: "Проверить" }));
    const result = await screen.findByRole("status", { name: "Проверка gemma-4-e4b" });
    expect(seen).toEqual({ id: "1", csrf: "csrf-test-token-admin" });
    expect(within(result).getByText("Москва")).toBeInTheDocument();
    expect(within(result).getByText("первый токен 820 мс · 62 ток/с")).toBeInTheDocument();
    // a popover: the table has the same rows, the result is not inside it
    expect(screen.getAllByRole("row").length).toBe(rowsBefore);
    expect(screen.getByRole("table", { name: "Развёртывания" })).not.toContainElement(result);
  });

  it("the button is disabled while the probe runs and the label keeps the place", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => void (release = r));
    server.use(
      http.post("/api/admin/deployments/:id/probe", async () => {
        await gate;
        return HttpResponse.json(ok);
      }),
    );
    const { user } = await openList();
    const row = screen.getByRole("row", { name: /gemma-4-e4b/ });
    await user.click(within(row).getByRole("button", { name: "Проверить" }));
    const running = await within(row).findByRole("button", { name: "Проверка" });
    expect(running).toBeDisabled();
    release();
    await screen.findByRole("status", { name: "Проверка gemma-4-e4b" });
    expect(within(row).getByRole("button", { name: "Проверить" })).toBeEnabled();
  });

  it("shows the error of a model that did not answer", async () => {
    server.use(
      http.post("/api/admin/deployments/:id/probe", () =>
        HttpResponse.json({ ok: false, status: 0, answer: "", ttft_ms: null, tokens_per_s: null, error: "Нет ответа за 60 с" }),
      ),
    );
    const { user } = await openList();
    await user.click(within(screen.getByRole("row", { name: /qwen3-30b-a3b/ })).getByRole("button", { name: "Проверить" }));
    const result = await screen.findByRole("status", { name: "Проверка qwen3-30b-a3b" });
    expect(within(result).getByText("Нет ответа за 60 с")).toBeInTheDocument();
    expect(within(result).queryByText(/ток\/с/)).not.toBeInTheDocument();
  });

  it("says so when a reasoning model spent its tokens on thinking", async () => {
    server.use(
      http.post("/api/admin/deployments/:id/probe", () =>
        HttpResponse.json({ ok: true, status: 200, answer: "", ttft_ms: 450, tokens_per_s: 31.2, error: null }),
      ),
    );
    const { user } = await openList();
    await user.click(within(screen.getByRole("row", { name: /gemma-4-e4b/ })).getByRole("button", { name: "Проверить" }));
    const result = await screen.findByRole("status", { name: "Проверка gemma-4-e4b" });
    expect(within(result).getByText(/Модель рассуждает/)).toBeInTheDocument();
    expect(within(result).getByText("первый токен 450 мс · 31 ток/с")).toBeInTheDocument();
  });

  it("shows an HTTP refusal of the portal as the error", async () => {
    server.use(
      http.post("/api/admin/deployments/:id/probe", () =>
        HttpResponse.json({ detail: "Развёртывание выключено" }, { status: 409 }),
      ),
    );
    const { user } = await openList();
    await user.click(within(screen.getByRole("row", { name: /gemma-4-e4b/ })).getByRole("button", { name: "Проверить" }));
    expect(await screen.findByText("Развёртывание выключено")).toBeInTheDocument();
  });

  it("the popover closes with Escape and with its own button", async () => {
    const { user } = await openList();
    const button = within(screen.getByRole("row", { name: /gemma-4-e4b/ })).getByRole("button", { name: "Проверить" });
    await user.click(button);
    await screen.findByRole("status", { name: "Проверка gemma-4-e4b" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("status", { name: "Проверка gemma-4-e4b" })).not.toBeInTheDocument());

    await user.click(button);
    await screen.findByRole("status", { name: "Проверка gemma-4-e4b" });
    await user.click(screen.getByRole("button", { name: "Закрыть результат проверки" }));
    expect(screen.queryByRole("status", { name: "Проверка gemma-4-e4b" })).not.toBeInTheDocument();
  });

  it("a switched-off deployment cannot be probed", async () => {
    server.use(
      http.get("/api/admin/deployments", () =>
        HttpResponse.json(fixtures.deployments.map((d) => (d.name === "llama-3-1-8b" ? { ...d, enabled: false } : d))),
      ),
    );
    await openList();
    expect(within(screen.getByRole("row", { name: /llama-3-1-8b/ })).getByRole("button", { name: "Проверить" })).toBeDisabled();
  });
});
