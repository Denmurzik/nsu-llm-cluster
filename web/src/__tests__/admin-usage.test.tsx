import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";
import type { AdminUsage, UsageItem } from "../types";

const item = (key: string, label: string, p: number, c: number, r: number, email?: string): UsageItem => ({
  key,
  label,
  email,
  prompt_tokens: p,
  completion_tokens: c,
  tokens: p + c,
  requests: r,
});

const USERS = [item("u1", "Иван Иванов", 90000, 30000, 70, "ivan@g.nsu.ru"), item("u2", "Пётр", 8000, 2000, 10, "petr@g.nsu.ru")];
const MODELS = [item("gemma", "gemma", 60000, 20000, 50), item("qwen", "qwen", 38000, 12000, 30)];

function report(from: string, to: string, group: "user" | "model" | "day"): AdminUsage {
  const days: string[] = [];
  for (let t = Date.parse(from); t <= Date.parse(to); t += 86_400_000) days.push(new Date(t).toISOString().slice(0, 10));
  const by_day = days.map((day, i) => ({ ...item(day, day, i === days.length - 1 ? 100000 : 0, 20000, i === days.length - 1 ? 80 : 0), day }));
  return {
    from,
    to,
    group,
    totals: { prompt_tokens: 98000, completion_tokens: 32000, tokens: 130000, requests: 80 },
    top_users: USERS,
    top_models: MODELS,
    by_day,
    items: group === "user" ? USERS : group === "model" ? MODELS : by_day,
  };
}

function serve() {
  const seen: URLSearchParams[] = [];
  server.use(
    http.get("/api/admin/usage", ({ request }) => {
      const q = new URL(request.url).searchParams;
      seen.push(q);
      return HttpResponse.json(report(q.get("from") ?? "", q.get("to") ?? "", (q.get("group") ?? "user") as "user"));
    }),
  );
  return seen;
}

describe("admin usage", () => {
  it("is for admins only", async () => {
    renderApp("/admin/usage", "user");
    expect(await screen.findByText("Недостаточно прав")).toBeInTheDocument();
  });

  it("is one click from the user's own usage, only for admins", async () => {
    serve();
    const { user, unmount } = renderApp("/usage", "admin");
    const tabs = await screen.findByRole("navigation", { name: "Чьё использование" });
    await user.click(within(tabs).getByRole("link", { name: "Все пользователи" }));
    expect(await screen.findByRole("table", { name: "Использование по группам" })).toBeInTheDocument();
    unmount();
    renderApp("/usage", "user");
    await screen.findByText("128 450");
    expect(screen.queryByRole("navigation", { name: "Чьё использование" })).not.toBeInTheDocument();
  });

  it("asks for the last seven days by user and shows totals, tops, a bar per day and the table", async () => {
    const seen = serve();
    renderApp("/admin/usage", "admin");
    expect(await screen.findByText("98 000")).toBeInTheDocument(); // prompt
    expect(screen.getByText("32 000")).toBeInTheDocument(); // completion
    expect(screen.getByText("80")).toBeInTheDocument(); // requests
    expect(seen[0].get("group")).toBe("user");
    const days = (Date.parse(seen[0].get("to")!) - Date.parse(seen[0].get("from")!)) / 86_400_000 + 1;
    expect(days).toBe(7);
    expect(within(screen.getByRole("img", { name: /Токены по дням/ })).getAllByTestId("bar")).toHaveLength(7);

    const users = screen.getByRole("list", { name: "Топ пользователей" });
    expect(within(users).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      expect.stringContaining("Иван Иванов"),
      expect.stringContaining("Пётр"),
    ]);
    expect(within(screen.getByRole("list", { name: "Топ моделей" })).getAllByRole("listitem")).toHaveLength(2);

    const table = screen.getByRole("table", { name: "Использование по группам" });
    const first = within(table).getAllByRole("row")[1];
    expect(within(first).getByText("ivan@g.nsu.ru")).toBeInTheDocument();
    expect(within(first).getByText("120 000")).toBeInTheDocument();
  });

  it("asks again for another period and another grouping", async () => {
    const seen = serve();
    const { user } = renderApp("/admin/usage", "admin");
    await screen.findByRole("table", { name: "Использование по группам" });
    await user.click(screen.getByRole("tab", { name: "30 дней" }));
    await waitFor(() => expect(seen.some((q) => (Date.parse(q.get("to")!) - Date.parse(q.get("from")!)) / 86_400_000 === 29)).toBe(true));
    await user.click(screen.getByRole("tab", { name: "По моделям" }));
    const table = await screen.findByRole("table", { name: "Использование по группам" });
    await waitFor(() => expect(within(table).getByRole("columnheader", { name: "Модель" })).toBeInTheDocument());
    expect(seen[seen.length - 1].get("group")).toBe("model");
    expect(within(table).getByText("gemma")).toBeInTheDocument();
  });

  it("does not ask for a period that ends before it starts", async () => {
    const seen = serve();
    const { user } = renderApp("/admin/usage", "admin");
    await screen.findByRole("table", { name: "Использование по группам" });
    const before = seen.length;
    const from = screen.getByLabelText("С");
    await user.clear(from);
    expect(await screen.findByText("Укажите начало и конец периода")).toBeInTheDocument();
    expect(seen.length).toBe(before);
    expect(screen.queryByRole("link", { name: /Скачать CSV/ })).not.toBeInTheDocument();
  });

  it("links the CSV of the shown period and grouping", async () => {
    serve();
    const { user } = renderApp("/admin/usage", "admin");
    const link = await screen.findByRole("link", { name: /Скачать CSV/ });
    expect(link.getAttribute("href")).toMatch(/^\/api\/admin\/usage\.csv\?from=\d{4}-\d{2}-\d{2}&to=\d{4}-\d{2}-\d{2}&group=user$/);
    await user.click(await screen.findByRole("tab", { name: "По дням" }));
    await waitFor(() => expect(screen.getByRole("link", { name: /Скачать CSV/ }).getAttribute("href")).toMatch(/group=day$/));
  });

  it("explains an empty period", async () => {
    server.use(
      http.get("/api/admin/usage", ({ request }) => {
        const q = new URL(request.url).searchParams;
        return HttpResponse.json({
          ...report(q.get("from")!, q.get("to")!, "user"),
          totals: { prompt_tokens: 0, completion_tokens: 0, tokens: 0, requests: 0 },
          top_users: [],
          top_models: [],
          items: [],
        });
      }),
    );
    renderApp("/admin/usage", "admin");
    expect(await screen.findByText("За выбранный период запросов не было")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Использование по группам" })).not.toBeInTheDocument();
  });

  it("shows that the gateway is down", async () => {
    server.use(http.get("/api/admin/usage", () => HttpResponse.json({ detail: "Шлюз моделей недоступен" }, { status: 503 })));
    renderApp("/admin/usage", "admin");
    expect(await screen.findByText("Шлюз моделей недоступен")).toBeInTheDocument();
  });
});
