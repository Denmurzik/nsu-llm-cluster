import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { fixtures } from "../mocks/handlers";
import { renderApp } from "../test/render";

async function openLog() {
  const view = renderApp("/admin/audit", "admin");
  await screen.findByRole("row", { name: /Развёртывание создано/ });
  return view;
}

describe("audit log page", () => {
  it("is in the admin menu and for admins only", async () => {
    const { user } = renderApp("/admin/users", "admin");
    const nav = await screen.findByRole("navigation", { name: "Разделы" });
    await user.click(within(nav).getByRole("link", { name: "Журнал" }));
    expect(await screen.findByRole("heading", { name: "Журнал", level: 1 })).toBeInTheDocument();
  });

  it("refuses a regular user", async () => {
    renderApp("/admin/audit", "user");
    expect(await screen.findByText("Недостаточно прав")).toBeInTheDocument();
  });

  it("shows time, actor, action in Russian with its code, object and address", async () => {
    await openLog();
    const create = screen.getByRole("row", { name: /Развёртывание создано/ });
    expect(within(create).getByText("d.muruev@g.nsu.ru")).toBeInTheDocument();
    expect(within(create).getByText("deployment.create")).toBeInTheDocument();
    expect(within(create).getByText("gemma-4-e4b")).toBeInTheDocument();
    expect(within(create).getByText("развёртывание")).toBeInTheDocument();
    expect(within(create).getByText("192.168.3.15")).toBeInTheDocument();

    const wake = screen.getByRole("row", { name: /Нода разбужена/ });
    expect(within(wake).getByText("система")).toBeInTheDocument(); // no actor: a controller did it
    expect(within(wake).getByText("—")).toBeInTheDocument(); // no address
    expect(screen.getByText("Показано 1–3 из 3")).toBeInTheDocument();
  });

  it("unfolds the details of an entry, and only of an entry that has them", async () => {
    const { user } = await openLog();
    const toggle = screen.getByRole("button", { name: "Подробности: Развёртывание создано, gemma-4-e4b" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("term")).not.toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const details = screen.getByLabelText("Подробности записи 4");
    expect(within(details).getByText("Репозиторий")).toBeInTheDocument();
    expect(within(details).getByText("unsloth/gemma-4-E4B-it-GGUF")).toBeInTheDocument();
    expect(within(details).getByText("Режим")).toBeInTheDocument();

    await user.click(toggle);
    expect(screen.queryByLabelText("Подробности записи 4")).not.toBeInTheDocument();
    // «Развёртывание удалено» has no details: nothing to unfold
    expect(screen.queryByRole("button", { name: /Подробности: Развёртывание удалено/ })).not.toBeInTheDocument();
  });

  it("shows a user by e-mail when the entry carries it, and nested details as JSON", async () => {
    server.use(
      http.get("/api/admin/audit", () =>
        HttpResponse.json({
          items: [
            {
              id: 9,
              at: "2026-11-02T11:00:00Z",
              actor_id: null,
              actor_email: "d.muruev@g.nsu.ru",
              action: "user.limits",
              target_type: "user",
              target: "7c1f6a3e-5b0d-4a52-9d44-0e2b8c1f3a10",
              details: { email: "a.petrova@g.nsu.ru", changed: { rpm_limit: { old: null, new: 10 } } },
              ip: null,
            },
            {
              id: 8,
              at: "2026-11-02T10:00:00Z",
              actor_id: null,
              actor_email: null,
              action: "some.future_action",
              target_type: "thing",
              target: "x",
              details: null,
              ip: null,
            },
          ],
          total: 2,
        }),
      ),
    );
    const { user } = renderApp("/admin/audit", "admin");
    const row = await screen.findByRole("row", { name: /Лимиты пользователя изменены/ });
    expect(within(row).getByText("a.petrova@g.nsu.ru")).toBeInTheDocument();
    expect(within(row).getByText("пользователь")).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: /Подробности/ }));
    expect(screen.getByText('{"rpm_limit":{"old":null,"new":10}}')).toBeInTheDocument();
    // an action the table does not know is shown by its code
    expect(screen.getByRole("row", { name: /some\.future_action/ })).toBeInTheDocument();
  });

  it("filters by action group, actor, object and time and says when nothing matches", async () => {
    const calls: URLSearchParams[] = [];
    server.use(
      http.get("/api/admin/audit", ({ request }) => {
        const q = new URL(request.url).searchParams;
        calls.push(q);
        const prefix = q.get("action");
        const items = fixtures.audit.items.filter((e) => !prefix || e.action.startsWith(prefix));
        return HttpResponse.json({ items, total: items.length });
      }),
    );
    const { user } = await openLog();
    await user.selectOptions(screen.getByRole("combobox", { name: "Действие" }), "node.");
    await waitFor(() => expect(screen.queryByRole("row", { name: /Развёртывание создано/ })).not.toBeInTheDocument());
    expect(screen.getByRole("row", { name: /Нода разбужена/ })).toBeInTheDocument();
    expect(screen.getByText("Показано 1–1 из 1")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Исполнитель"), "muruev");
    await user.type(screen.getByLabelText("Объект"), "win");
    await waitFor(() => {
      const last = calls[calls.length - 1];
      expect(last.get("actor")).toBe("muruev");
      expect(last.get("target")).toBe("win");
      expect(last.get("action")).toBe("node.");
    });
    await user.selectOptions(screen.getByRole("combobox", { name: "Действие" }), "backup.");
    expect(await screen.findByText("По этим условиям записей нет")).toBeInTheDocument();
  });

  it("sends the time range as ISO times", async () => {
    const calls: URLSearchParams[] = [];
    server.use(
      http.get("/api/admin/audit", ({ request }) => {
        calls.push(new URL(request.url).searchParams);
        return HttpResponse.json(fixtures.audit);
      }),
    );
    await openLog();
    fireEvent.change(screen.getByLabelText("С"), { target: { value: "2026-11-01T00:00" } });
    await waitFor(() => expect(calls[calls.length - 1].get("since")).toBe(new Date("2026-11-01T00:00").toISOString()));
    expect(calls[calls.length - 1].get("until")).toBeNull();
  });

  it("offers the CSV of exactly what the filters show", async () => {
    const { user } = await openLog();
    const link = screen.getByRole("link", { name: "Скачать CSV" });
    expect(link).toHaveAttribute("href", "/api/admin/audit/export");
    expect(link).toHaveAttribute("download");
    await user.selectOptions(screen.getByRole("combobox", { name: "Действие" }), "user.");
    await waitFor(() => expect(link).toHaveAttribute("href", "/api/admin/audit/export?action=user."));
    await user.type(screen.getByLabelText("Исполнитель"), "a@b");
    await waitFor(() => expect(link).toHaveAttribute("href", "/api/admin/audit/export?action=user.&actor=a%40b"));
  });

  it("says in Russian what the details are, and keeps the key it does not know", async () => {
    server.use(
      http.get("/api/admin/audit", () =>
        HttpResponse.json({
          items: [
            {
              id: 3,
              at: "2026-11-02T11:00:00Z",
              actor_id: null,
              actor_email: "d.muruev@g.nsu.ru",
              action: "user.block",
              target_type: "user",
              target: "u1",
              details: { email: "a@g.nsu.ru", key_count: 2, reason: "спам", odd_field: 1 },
              ip: null,
            },
          ],
          total: 1,
        }),
      ),
    );
    const { user } = renderApp("/admin/audit", "admin");
    await user.click(await screen.findByRole("button", { name: /Подробности/ }));
    const details = screen.getByLabelText("Подробности записи 3");
    for (const label of ["Почта", "Ключей", "Причина", "odd_field"]) {
      expect(within(details).getByText(label)).toBeInTheDocument();
    }
    expect(within(details).queryByText("key_count")).not.toBeInTheDocument();
  });

  it("says when the CSV will be cut", async () => {
    server.use(http.get("/api/admin/audit", () => HttpResponse.json({ items: fixtures.audit.items, total: 60000 })));
    renderApp("/admin/audit", "admin");
    expect(await screen.findByText(/В файл CSV попадут только 50 000 самых новых записей/)).toBeInTheDocument();
  });

  it("shows an empty log and no other page", async () => {
    server.use(http.get("/api/admin/audit", () => HttpResponse.json({ items: [], total: 0 })));
    renderApp("/admin/audit", "admin");
    expect(await screen.findByText("В журнале пока пусто")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Дальше" })).toBeDisabled();
  });
});
