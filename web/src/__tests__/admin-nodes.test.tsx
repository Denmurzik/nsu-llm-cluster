import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { fixtures } from "../mocks/handlers";
import { renderApp } from "../test/render";

describe("admin nodes", () => {
  it("renders the nodes table with source, accelerator, memory and assignment", async () => {
    renderApp("/admin/nodes", "admin");
    const gpu = await screen.findByRole("row", { name: /aud-204-gpu01/ });
    expect(within(gpu).getByText("k8s")).toBeInTheDocument();
    expect(within(gpu).getByText("nvidia")).toBeInTheDocument();
    expect(within(gpu).getByText("12,0 ГБ")).toBeInTheDocument(); // VRAM
    expect(within(gpu).getByText("32,0 ГБ")).toBeInTheDocument(); // RAM
    expect(within(gpu).getByLabelText("Развёртывание для aud-204-gpu01")).toHaveValue("1");

    const laptop = screen.getByRole("row", { name: /win-laptop-07/ });
    expect(within(laptop).getByText("standalone")).toBeInTheDocument();
    expect(within(laptop).getByText("Не в сети")).toBeInTheDocument();
    expect(within(laptop).getByLabelText("Развёртывание для win-laptop-07")).toHaveValue("");

    const main = screen.getByRole("row", { name: /llm-main/ });
    expect(within(main).getByText("главная")).toBeInTheDocument();
    expect(within(main).queryByLabelText("Развёртывание для llm-main")).not.toBeInTheDocument();
  });

  it("PUTs the assignment with CSRF when the select changes", async () => {
    let put: { name: string; csrf: string | null; body: unknown } | null = null;
    server.use(
      http.put("/api/admin/nodes/:name/assignment", async ({ request, params }) => {
        put = { name: String(params.name), csrf: request.headers.get("X-CSRF-Token"), body: await request.json() };
        return HttpResponse.json({});
      }),
    );
    const { user } = renderApp("/admin/nodes", "admin");
    const select = await screen.findByLabelText("Развёртывание для aud-101-pc02");
    await user.selectOptions(select, "qwen3-30b-a3b");
    await waitFor(() => expect(put).not.toBeNull());
    expect(put).toEqual({ name: "aud-101-pc02", csrf: "csrf-test-token-admin", body: { deployment_id: 2 } });
  });

  it("unassigns a node with null", async () => {
    let body: unknown = null;
    server.use(
      http.put("/api/admin/nodes/:name/assignment", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({});
      }),
    );
    const { user } = renderApp("/admin/nodes", "admin");
    await user.selectOptions(await screen.findByLabelText("Развёртывание для aud-101-pc01"), "");
    await waitFor(() => expect(body).toEqual({ deployment_id: null }));
  });

  it("shows the assignment error from the server and restores the value", async () => {
    server.use(
      http.put("/api/admin/nodes/:name/assignment", () =>
        HttpResponse.json(
          { detail: "Standalone-нода поддерживает только replicas на llama.cpp" },
          { status: 422 },
        ),
      ),
    );
    const { user } = renderApp("/admin/nodes", "admin");
    const select = await screen.findByLabelText("Развёртывание для win-laptop-07");
    await user.selectOptions(select, "qwen3-30b-a3b");
    expect(await screen.findByText("Standalone-нода поддерживает только replicas на llama.cpp")).toBeInTheDocument();
    await waitFor(() => expect(select).toHaveValue(""));
  });

  it("shows the deployments error and still lists the nodes", async () => {
    server.use(http.get("/api/admin/deployments", () => HttpResponse.json({ detail: "Ошибка 500" }, { status: 500 })));
    renderApp("/admin/nodes", "admin");
    expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка 500");
    expect(await screen.findByRole("row", { name: /aud-204-gpu01/ })).toBeInTheDocument();
  });

  it("keeps an assignment to a deployment missing from the list visible and not selectable", async () => {
    server.use(http.get("/api/admin/deployments", () => HttpResponse.json([])));
    renderApp("/admin/nodes", "admin");
    const select = await screen.findByLabelText("Развёртывание для aud-204-gpu01");
    const option = within(select).getByRole("option", { name: /gemma-4-e4b/ });
    expect(option).toBeDisabled();
    expect(select).toHaveValue(option.getAttribute("value"));
  });

  it("shows nodes on maintenance and switches maintenance with CSRF", async () => {
    let put: { name: string; csrf: string | null; body: unknown } | null = null;
    server.use(
      http.put("/api/admin/nodes/:name/maintenance", async ({ request, params }) => {
        put = { name: String(params.name), csrf: request.headers.get("X-CSRF-Token"), body: await request.json() };
        return HttpResponse.json({});
      }),
    );
    const { user } = renderApp("/admin/nodes", "admin");
    const pc02 = await screen.findByRole("row", { name: /aud-101-pc02/ });
    expect(within(pc02).getByText("На обслуживании")).toBeInTheDocument();
    await user.click(within(pc02).getByRole("button", { name: "Вернуть в работу" }));
    await waitFor(() =>
      expect(put).toEqual({ name: "aud-101-pc02", csrf: "csrf-test-token-admin", body: { maintenance: false } }),
    );

    const pc01 = screen.getByRole("row", { name: /aud-101-pc01/ });
    expect(within(pc01).getByText("Готова")).toBeInTheDocument();
    await user.click(within(pc01).getByRole("button", { name: "На обслуживание" }));
    await waitFor(() => expect(put).toEqual({ name: "aud-101-pc01", csrf: "csrf-test-token-admin", body: { maintenance: true } }));

    const main = screen.getByRole("row", { name: /llm-main/ });
    expect(within(main).queryByRole("button", { name: "На обслуживание" })).not.toBeInTheDocument();
  });

  it("asks before taking a node of a split group off for maintenance", async () => {
    let put: unknown = null;
    server.use(
      http.get("/api/admin/nodes", () =>
        HttpResponse.json({
          ...fixtures.nodes,
          nodes: fixtures.nodes.nodes.map((n) => (n.name === "aud-204-gpu01" ? { ...n, deployment: "qwen3-30b-a3b" } : n)),
        }),
      ),
      http.put("/api/admin/nodes/:name/maintenance", async ({ request, params }) => {
        put = { name: String(params.name), body: await request.json() };
        return HttpResponse.json({});
      }),
    );
    const { user } = renderApp("/admin/nodes", "admin");
    const gpu = await screen.findByRole("row", { name: /aud-204-gpu01/ });
    await user.click(within(gpu).getByRole("button", { name: "На обслуживание" }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Группа split будет остановлена/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(put).toBeNull();

    await user.click(within(gpu).getByRole("button", { name: "На обслуживание" }));
    dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "На обслуживание" }));
    await waitFor(() => expect(put).toEqual({ name: "aud-204-gpu01", body: { maintenance: true } }));
  });

  it("shows the maintenance error from the server", async () => {
    server.use(
      http.put("/api/admin/nodes/:name/maintenance", () =>
        HttpResponse.json({ detail: "Кластер недоступен" }, { status: 503 }),
      ),
    );
    const { user } = renderApp("/admin/nodes", "admin");
    const pc01 = await screen.findByRole("row", { name: /aud-101-pc01/ });
    await user.click(within(pc01).getByRole("button", { name: "На обслуживание" }));
    expect(await screen.findByText("Кластер недоступен")).toBeInTheDocument();
  });

  it("shows join commands and copies them", async () => {
    const { user } = renderApp("/admin/nodes", "admin");
    const linux = await screen.findByTestId("join-linux");
    expect(linux).toHaveTextContent(fixtures.join.linux);
    expect(screen.getByTestId("join-linux_standalone")).toHaveTextContent("--mode standalone");
    expect(screen.getByTestId("join-windows")).toHaveTextContent(fixtures.join.windows);

    await user.click(screen.getByRole("button", { name: "Копировать команду Linux" }));
    expect(await navigator.clipboard.readText()).toBe(fixtures.join.linux);
    await user.click(screen.getByRole("button", { name: "Копировать команду Windows" }));
    expect(await navigator.clipboard.readText()).toBe(fixtures.join.windows);
  });

  it("notes that Kubernetes is unavailable", async () => {
    server.use(http.get("/api/admin/nodes", () => HttpResponse.json({ ...fixtures.nodes, k8s_available: false })));
    renderApp("/admin/nodes", "admin");
    expect(await screen.findByText("Kubernetes недоступен, показаны только standalone-ноды")).toBeInTheDocument();
  });
});

describe("role based navigation", () => {
  it("hides the admin section for a regular user", async () => {
    renderApp("/models", "user");
    const nav = await screen.findByRole("navigation", { name: "Разделы" });
    expect(within(nav).getByRole("link", { name: "Ключи" })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Развёртывания" })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Ноды" })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Пользователи" })).not.toBeInTheDocument();
  });

  it("shows the admin section for an admin", async () => {
    renderApp("/models", "admin");
    const nav = await screen.findByRole("navigation", { name: "Разделы" });
    expect(within(nav).getByRole("link", { name: "Развёртывания" })).toHaveAttribute("href", "/admin/deployments");
    expect(within(nav).getByRole("link", { name: "Ноды" })).toHaveAttribute("href", "/admin/nodes");
    expect(within(nav).getByRole("link", { name: "Пользователи" })).toHaveAttribute("href", "/admin/users");
  });

  it("refuses admin pages for a regular user", async () => {
    renderApp("/admin/nodes", "user");
    expect(await screen.findByText("Недостаточно прав")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows who is signed in and a logout link", async () => {
    renderApp("/models", "user");
    expect(await screen.findByText("Анна Петрова")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Выйти" })).toHaveAttribute("href", "/auth/logout");
  });
});

describe("admin users", () => {
  it("lists users with key counts and token usage", async () => {
    renderApp("/admin/users", "admin");
    const row = await screen.findByRole("row", { name: /a\.petrova@g\.nsu\.ru/ });
    expect(within(row).getByText("238 190")).toBeInTheDocument();
    const idle = screen.getByRole("row", { name: /i\.sidorov@g\.nsu\.ru/ });
    expect(within(idle).getAllByText("—").length).toBeGreaterThan(0);
  });
});
