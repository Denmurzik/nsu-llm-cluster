import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { fixtures, state } from "../mocks/handlers";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

// «Удалить из кластера…» (task 2-31): the «⋯» menu of a node row, the confirmation, the force variant, the result and the
// bulk action. Fixture nodes: aud-101-pc01, aud-204-gpu01 ready with gemma-4-e4b; aud-101-pc02 on maintenance with it;
// win-laptop-07 down, no deployment; llm-main the main one. Deployments: gemma-4-e4b (replicas), qwen3-30b-a3b (split).

const ANSWER = {
  node: "win-laptop-07",
  online: false,
  k8s: "absent",
  unassigned: [],
  uninstall: {
    windows: ".\\install-node.ps1 -Uninstall -Name win-laptop-07",
    linux: "sudo ./run-node.sh --uninstall --main 192.168.3.21 --name win-laptop-07",
  },
};

async function openNodes() {
  const view = renderApp("/admin/nodes", "admin");
  await screen.findByRole("row", { name: /win-laptop-07/ });
  return view;
}

const rowOf = (name: RegExp) => screen.getByRole("row", { name });

async function pickDelete(user: ReturnType<typeof renderApp>["user"], node: string) {
  await user.click(screen.getByRole("button", { name: `Действия: ${node}` }));
  await user.click(await screen.findByRole("menuitem", { name: "Удалить из кластера…" }));
}

describe("the «⋯» menu of a node row", () => {
  it("is offered for every node but the main one and keeps the cleanup button next to it", async () => {
    await openNodes();
    expect(within(rowOf(/llm-main/)).queryByRole("button", { name: /Действия:/ })).not.toBeInTheDocument();
    const row = rowOf(/aud-101-pc01/);
    expect(within(row).getByRole("button", { name: "Действия: aud-101-pc01" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Очистить модели aud-101-pc01" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "На обслуживание" })).toBeInTheDocument();
  });

  it("holds the key reset and the removal, the removal dangerous, and calls nothing by itself", async () => {
    let calls = 0;
    server.use(http.delete("/api/admin/nodes/:name", () => (calls++, HttpResponse.json(ANSWER))));
    const { user } = await openNodes();
    await user.click(screen.getByRole("button", { name: "Действия: win-laptop-07" }));
    const menu = await screen.findByRole("menu", { name: "Действия: win-laptop-07" });
    const items = within(menu).getAllByRole("menuitem");
    expect(items.map((i) => i.textContent)).toEqual(["Сбросить ключ ноды…", "Удалить из кластера…"]);
    expect(items[1]).toHaveClass("danger");
    expect(items[0]).not.toHaveClass("danger");
    expect(calls).toBe(0);
  });
});

describe("the confirmation", () => {
  it("names the node and what is removed; Cancel calls nothing", async () => {
    let calls = 0;
    server.use(http.delete("/api/admin/nodes/:name", () => (calls++, HttpResponse.json(ANSWER))));
    const { user } = await openNodes();
    await pickDelete(user, "win-laptop-07");
    const dialog = await screen.findByRole("dialog", { name: "Удалить win-laptop-07 из кластера" });
    expect(within(dialog).getByText("Будет удалено на главном сервере:")).toBeInTheDocument();
    expect(within(dialog).getByText(/запись ноды в портале и объект ноды в Kubernetes/)).toBeInTheDocument();
    expect(within(dialog).getByText(/адрес сетевой карты для Wake-on-LAN/)).toBeInTheDocument();
    expect(within(dialog).getByText(/История замеров и журнал действий остаются/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/Нода работает на развёртываниях/)).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Удалить" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(calls).toBe(0);
  });

  it("says a PC that is in the network will register again, and a PC that is down does not", async () => {
    const { user } = await openNodes();
    await pickDelete(user, "win-laptop-07");
    expect(within(await screen.findByRole("dialog")).queryByText(/ПК сейчас в сети/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Отмена" }));
    await pickDelete(user, "aud-101-pc02");
    // aud-101-pc02 is ready in the fixture: the warning is there
    expect(within(await screen.findByRole("dialog")).getByText(/ПК сейчас в сети/)).toBeInTheDocument();
  });

  it("DELETEs with CSRF and without force, refreshes the table and shows the result", async () => {
    const sent: { name: string; csrf: string | null; search: string }[] = [];
    let listed = 0;
    server.use(
      http.delete("/api/admin/nodes/:name", ({ request, params }) => {
        sent.push({ name: String(params.name), csrf: request.headers.get("X-CSRF-Token"), search: new URL(request.url).search });
        return HttpResponse.json(ANSWER);
      }),
      http.get("/api/admin/nodes", () => (listed++, HttpResponse.json(fixtures.nodes))),
    );
    const { user } = await openNodes();
    const before = listed;
    await pickDelete(user, "win-laptop-07");
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Удалить" }));
    const result = await screen.findByRole("dialog", { name: "Нода win-laptop-07 удалена из кластера" });
    expect(sent).toEqual([{ name: "win-laptop-07", csrf: "csrf-test-token-admin", search: "" }]);
    expect(within(result).getByText(/ПК не в сети/)).toBeInTheDocument();
    await waitFor(() => expect(listed).toBeGreaterThan(before));
  });
});

describe("a node with a deployment", () => {
  it("opens the confirmation in the force variant, names the deployment and sends force=1", async () => {
    const sent: string[] = [];
    server.use(
      http.delete("/api/admin/nodes/:name", ({ request }) => {
        sent.push(new URL(request.url).search);
        return HttpResponse.json({ ...ANSWER, node: "aud-101-pc01", online: true, unassigned: ["gemma-4-e4b"] });
      }),
    );
    const { user } = await openNodes();
    await pickDelete(user, "aud-101-pc01");
    const dialog = await screen.findByRole("dialog", { name: "Удалить aud-101-pc01 из кластера" });
    expect(within(dialog).getByText(/Нода работает на развёртываниях: gemma-4-e4b\./)).toBeInTheDocument();
    expect(within(dialog).queryByText(/Группа split будет остановлена/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Всё равно удалить" }));
    const result = await screen.findByRole("dialog", { name: "Нода aud-101-pc01 удалена из кластера" });
    expect(sent).toEqual(["?force=1"]);
    expect(within(result).getByText("Снята с развёртываний: gemma-4-e4b.")).toBeInTheDocument();
  });

  it("warns that a split group stops and re-forms", async () => {
    state.assignments["aud-204-gpu01"] = "qwen3-30b-a3b";
    const { user } = await openNodes();
    await pickDelete(user, "aud-204-gpu01");
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/qwen3-30b-a3b \(split\)/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Группа split будет остановлена и соберётся заново/)).toBeInTheDocument();
  });

  it("asks again when the server knows a deployment the table did not (409), then sends force=1", async () => {
    const sent: string[] = [];
    server.use(
      http.delete("/api/admin/nodes/:name", ({ request }) => {
        const search = new URL(request.url).search;
        sent.push(search);
        if (search === "") {
          return HttpResponse.json(
            { detail: "Нода работает на развёртываниях: qwen3-30b-a3b (split).", deployments: [{ name: "qwen3-30b-a3b", mode: "split" }] },
            { status: 409 },
          );
        }
        return HttpResponse.json(ANSWER);
      }),
    );
    const { user } = await openNodes();
    await pickDelete(user, "win-laptop-07");
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Удалить" }));
    // the same dialog, now in the force variant
    const dialog = await screen.findByRole("button", { name: "Всё равно удалить" });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(within(screen.getByRole("dialog")).getByText(/qwen3-30b-a3b \(split\)/)).toBeInTheDocument();
    await user.click(dialog);
    await screen.findByRole("dialog", { name: "Нода win-laptop-07 удалена из кластера" });
    expect(sent).toEqual(["", "?force=1"]);
  });

  it("shows another error in the dialog and keeps it open", async () => {
    server.use(
      http.delete("/api/admin/nodes/:name", () => HttpResponse.json({ detail: "Кластер недоступен: нода не найдена" }, { status: 503 })),
    );
    const { user } = await openNodes();
    await pickDelete(user, "win-laptop-07");
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Удалить" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Кластер недоступен: нода не найдена");
    expect(screen.getByRole("dialog", { name: "Удалить win-laptop-07 из кластера" })).toBeInTheDocument();
  });
});

describe("the result", () => {
  it("online: says the agent registers again and gives both uninstall commands with copy buttons, no token", async () => {
    server.use(
      http.delete("/api/admin/nodes/:name", () => HttpResponse.json({ ...ANSWER, online: true, k8s: "deleted" })),
    );
    const { user } = await openNodes();
    await pickDelete(user, "win-laptop-07");
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Удалить" }));
    const result = await screen.findByRole("dialog", { name: "Нода win-laptop-07 удалена из кластера" });
    expect(within(result).getByText(/ПК ещё в сети\./)).toBeInTheDocument();
    expect(within(result).getByTestId("uninstall-windows")).toHaveTextContent(".\\install-node.ps1 -Uninstall -Name win-laptop-07");
    expect(within(result).getByTestId("uninstall-linux")).toHaveTextContent("sudo ./run-node.sh --uninstall");
    expect(within(result).getByRole("button", { name: "Копировать команду удаления для Windows" })).toBeInTheDocument();
    expect(within(result).getByRole("button", { name: "Копировать команду удаления для Linux" })).toBeInTheDocument();
    expect(result.textContent).not.toMatch(/token|токен: [A-Za-z0-9]|K10/i);
    expect(within(result).queryByText(/кластер не ответил/)).not.toBeInTheDocument();
    await user.click(within(result).getByRole("button", { name: "Закрыть" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("says when the cluster did not answer, so the Kubernetes node is still there", async () => {
    server.use(http.delete("/api/admin/nodes/:name", () => HttpResponse.json({ ...ANSWER, k8s: "unavailable" })));
    const { user } = await openNodes();
    await pickDelete(user, "win-laptop-07");
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Удалить" }));
    expect(await screen.findByText(/Объект ноды в Kubernetes не удалён: кластер не ответил/)).toBeInTheDocument();
  });

  it("says when the k3s password stayed, what clears it and how to do it by hand", async () => {
    server.use(http.delete("/api/admin/nodes/:name", () => HttpResponse.json({ ...ANSWER, k3s_password: "left" })));
    const { user } = await openNodes();
    await pickDelete(user, "win-laptop-07");
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Удалить" }));
    expect(await screen.findByText(/Пароль ноды k3s не удалён/)).toBeInTheDocument();
    expect(screen.getByText(/delete secret win-laptop-07\.node-password\.k3s/)).toBeInTheDocument();
  });

  it("outlives the row: the node leaves the table and the dialog stays", async () => {
    const { user } = await openNodes();
    await pickDelete(user, "win-laptop-07");
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Удалить" }));
    await screen.findByRole("dialog", { name: "Нода win-laptop-07 удалена из кластера" });
    await waitFor(() => expect(screen.queryByRole("row", { name: /win-laptop-07/ })).not.toBeInTheDocument());
    expect(screen.getByRole("dialog", { name: "Нода win-laptop-07 удалена из кластера" })).toBeInTheDocument();
  });
});

describe("the bulk bar", () => {
  const tick = (user: ReturnType<typeof renderApp>["user"], node: string) =>
    user.click(screen.getByRole("checkbox", { name: `Выбрать ${node}` }));

  it("offers «Удалить из кластера…» for the ticked nodes and lists what it removes", async () => {
    const { user } = await openNodes();
    const toolbar = screen.getByRole("toolbar", { name: "Действия над выбранными нодами" });
    await tick(user, "win-laptop-07");
    await tick(user, "aud-101-pc01");
    await user.click(within(toolbar).getByRole("button", { name: "Удалить из кластера…" }));
    const dialog = await screen.findByRole("dialog", { name: "Удалить из кластера" });
    expect(within(dialog).getByText("2 ноды:")).toBeInTheDocument();
    expect(within(dialog).getByText(/aud-101-pc01/)).toHaveTextContent("развёртывание gemma-4-e4b");
    expect(within(dialog).getByText(/Нода работает на развёртываниях: gemma-4-e4b\./)).toBeInTheDocument();
    expect(within(dialog).getByText(/запись ноды в портале/)).toBeInTheDocument();
  });

  it("deletes node by node, with force only for a node with a deployment, and lists the results", async () => {
    const sent: string[] = [];
    server.use(
      http.delete("/api/admin/nodes/:name", ({ request, params }) => {
        sent.push(`${String(params.name)}${new URL(request.url).search}`);
        const name = String(params.name);
        if (name === "aud-204-gpu01") return HttpResponse.json({ detail: "Кластер недоступен" }, { status: 503 });
        return HttpResponse.json({ ...ANSWER, node: name, online: name === "aud-101-pc01" });
      }),
    );
    const { user } = await openNodes();
    for (const n of ["win-laptop-07", "aud-101-pc01", "aud-204-gpu01"]) await tick(user, n);
    const toolbar = screen.getByRole("toolbar", { name: "Действия над выбранными нодами" });
    await user.click(within(toolbar).getByRole("button", { name: "Удалить из кластера…" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Удалить" }));
    const results = await screen.findByRole("list", { name: "Результат по нодам" });
    await waitFor(() => expect(within(results).getAllByRole("listitem")).toHaveLength(3));
    expect(sent).toEqual(["aud-101-pc01?force=1", "aud-204-gpu01?force=1", "win-laptop-07"]); // table order
    const text = results.textContent ?? "";
    expect(text).toMatch(/win-laptop-07\s*Удалена из кластера(?!;)/);
    expect(text).toMatch(/aud-101-pc01\s*Удалена из кластера; ПК в сети/);
    expect(text).toMatch(/aud-204-gpu01\s*Кластер недоступен/);
  });

  it("is off with nothing ticked", async () => {
    await openNodes();
    expect(screen.getByText(/Отметьте ноды, чтобы .* удалить из кластера/)).toBeInTheDocument();
  });
});
