import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { fixtures } from "../mocks/handlers";
import { renderApp } from "../test/render";

/** nodes: pc01 ready, pc02 on maintenance, gpu01 ready and in the split group, laptop down; llm-main is the main one */
const withSplitGpu = () =>
  http.get("/api/admin/nodes", () =>
    HttpResponse.json({
      ...fixtures.nodes,
      nodes: fixtures.nodes.nodes.map((n) => (n.name === "aud-204-gpu01" ? { ...n, deployment: "qwen3-30b-a3b" } : n)),
    }),
  );

async function openNodes() {
  const view = renderApp("/admin/nodes", "admin");
  await screen.findByRole("row", { name: /aud-204-gpu01/ });
  return view;
}

const rowOf = (name: RegExp) => screen.getByRole("row", { name });
const tick = async (user: ReturnType<typeof renderApp>["user"], node: string) =>
  user.click(screen.getByRole("checkbox", { name: `Выбрать ${node}` }));
const toolbar = () => screen.getByRole("toolbar", { name: "Действия над выбранными нодами" });

describe("bulk node actions: selection", () => {
  it("the main node has no checkbox, every other row has one", async () => {
    await openNodes();
    expect(within(rowOf(/llm-main/)).queryByRole("checkbox")).not.toBeInTheDocument();
    for (const name of ["aud-101-pc01", "aud-101-pc02", "aud-204-gpu01", "win-laptop-07"]) {
      expect(screen.getByRole("checkbox", { name: `Выбрать ${name}` })).not.toBeChecked();
    }
  });

  it("the toolbar space is there before and after the selection, with the same height", async () => {
    const { user } = await openNodes();
    expect(within(toolbar()).getByText(/Отметьте ноды/)).toBeInTheDocument();
    expect(within(toolbar()).queryByRole("button", { name: "Разбудить" })).not.toBeInTheDocument();
    await tick(user, "aud-101-pc01");
    expect(within(toolbar()).getByText("Выбрано: 1 нода")).toBeInTheDocument();
    expect(within(toolbar()).getByRole("button", { name: "На обслуживание" })).toBeInTheDocument();
    // the same element, not a new one that pushes the table
    expect(screen.getAllByRole("toolbar")).toHaveLength(1);
  });

  it("the header checkbox selects every node except the main one, and clears them again", async () => {
    const { user } = await openNodes();
    const all = screen.getByRole("checkbox", { name: "Выбрать все ноды" });
    await user.click(all);
    expect(within(toolbar()).getByText("Выбрано: 4 ноды")).toBeInTheDocument();
    expect(all).toBeChecked();
    for (const name of ["aud-101-pc01", "aud-101-pc02", "aud-204-gpu01", "win-laptop-07"]) {
      expect(screen.getByRole("checkbox", { name: `Выбрать ${name}` })).toBeChecked();
    }
    await user.click(all);
    expect(within(toolbar()).getByText(/Отметьте ноды/)).toBeInTheDocument();
    expect(all).not.toBeChecked();
  });

  it("the header checkbox is indeterminate while only some rows are ticked", async () => {
    const { user } = await openNodes();
    const all = screen.getByRole("checkbox", { name: "Выбрать все ноды" }) as HTMLInputElement;
    await tick(user, "aud-101-pc01");
    expect(all.indeterminate).toBe(true);
    expect(all).not.toBeChecked();
    await user.click(all);
    expect(all.indeterminate).toBe(false);
    expect(all).toBeChecked();
  });
});

describe("bulk node actions: maintenance", () => {
  it("asks once, lists the nodes, then calls the node endpoint for each and shows every result", async () => {
    const calls: { name: string; body: unknown; csrf: string | null }[] = [];
    server.use(
      http.put("/api/admin/nodes/:name/maintenance", async ({ request, params }) => {
        const name = String(params.name);
        calls.push({ name, body: await request.json(), csrf: request.headers.get("X-CSRF-Token") });
        if (name === "win-laptop-07") return HttpResponse.json({ detail: "Кластер недоступен" }, { status: 503 });
        return HttpResponse.json({});
      }),
    );
    const { user } = await openNodes();
    await tick(user, "aud-101-pc01");
    await tick(user, "win-laptop-07");
    await user.click(within(toolbar()).getByRole("button", { name: "На обслуживание" }));

    const dialog = await screen.findByRole("dialog", { name: "Перевести на обслуживание" });
    expect(within(dialog).getByText("2 ноды:")).toBeInTheDocument();
    expect(within(dialog).getByText("aud-101-pc01")).toBeInTheDocument();
    expect(within(dialog).getByText("win-laptop-07")).toBeInTheDocument();
    expect(within(dialog).queryByText(/Группа split будет остановлена/)).not.toBeInTheDocument();
    expect(calls).toEqual([]); // nothing before the confirmation

    await user.click(within(dialog).getByRole("button", { name: "На обслуживание" }));
    const results = await screen.findByRole("list", { name: "Результат по нодам" });
    await waitFor(() => expect(within(results).getByText("Кластер недоступен")).toBeInTheDocument());
    expect(within(results).getByText("На обслуживании")).toBeInTheDocument();
    expect(calls).toEqual([
      { name: "aud-101-pc01", body: { maintenance: true }, csrf: "csrf-test-token-admin" },
      { name: "win-laptop-07", body: { maintenance: true }, csrf: "csrf-test-token-admin" },
    ]);
    // the table was refreshed once at the end and the failed node stays selected
    await waitFor(() => expect(within(toolbar()).getByText("Выбрано: 1 нода")).toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: "Выбрать win-laptop-07" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Выбрать aud-101-pc01" })).not.toBeChecked();

    await user.click(screen.getByRole("button", { name: "Закрыть" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("goes node by node, not all at once", async () => {
    const log: string[] = [];
    server.use(
      http.put("/api/admin/nodes/:name/maintenance", async ({ params }) => {
        log.push(`start ${String(params.name)}`);
        await new Promise((r) => setTimeout(r, 30));
        log.push(`end ${String(params.name)}`);
        return HttpResponse.json({});
      }),
    );
    const { user } = await openNodes();
    await tick(user, "aud-101-pc01");
    await tick(user, "aud-204-gpu01");
    await user.click(within(toolbar()).getByRole("button", { name: "На обслуживание" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "На обслуживание" }));
    await waitFor(() => expect(log).toHaveLength(4));
    expect(log).toEqual(["start aud-101-pc01", "end aud-101-pc01", "start aud-204-gpu01", "end aud-204-gpu01"]);
  });

  it("applies the split warning when a selected node belongs to a split group", async () => {
    server.use(withSplitGpu());
    const { user } = await openNodes();
    await tick(user, "aud-204-gpu01");
    await tick(user, "aud-101-pc01");
    await user.click(within(toolbar()).getByRole("button", { name: "На обслуживание" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Группа split будет остановлена/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("Cancel calls nothing", async () => {
    let calls = 0;
    server.use(
      http.put("/api/admin/nodes/:name/maintenance", () => {
        calls += 1;
        return HttpResponse.json({});
      }),
    );
    const { user } = await openNodes();
    await tick(user, "aud-101-pc01");
    await user.click(within(toolbar()).getByRole("button", { name: "На обслуживание" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Отмена" }));
    expect(calls).toBe(0);
  });

  it("«Вернуть в работу» takes only the nodes that are on maintenance", async () => {
    const names: string[] = [];
    server.use(
      http.put("/api/admin/nodes/:name/maintenance", async ({ params, request }) => {
        names.push(`${String(params.name)}:${JSON.stringify(await request.json())}`);
        return HttpResponse.json({});
      }),
    );
    const { user } = await openNodes();
    await tick(user, "aud-101-pc01"); // ready
    await tick(user, "aud-101-pc02"); // on maintenance
    await user.click(within(toolbar()).getByRole("button", { name: "Вернуть в работу" }));
    const dialog = await screen.findByRole("dialog", { name: "Вернуть в работу" });
    expect(within(dialog).getByText("1 нода:")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Вернуть в работу" }));
    await waitFor(() => expect(names).toEqual(['aud-101-pc02:{"maintenance":false}']));
  });

  it("a button with nothing to do is disabled", async () => {
    const { user } = await openNodes();
    await tick(user, "aud-101-pc01"); // ready, not on maintenance
    expect(within(toolbar()).getByRole("button", { name: "Вернуть в работу" })).toBeDisabled();
    expect(within(toolbar()).getByRole("button", { name: "Разбудить" })).toBeDisabled(); // it is in the network
    expect(within(toolbar()).getByRole("button", { name: "На обслуживание" })).toBeEnabled();
  });
});

describe("bulk node actions: assignment", () => {
  it("assigns the chosen deployment to every selected node", async () => {
    const puts: { name: string; body: unknown }[] = [];
    server.use(
      http.put("/api/admin/nodes/:name/assignment", async ({ params, request }) => {
        puts.push({ name: String(params.name), body: await request.json() });
        return HttpResponse.json({});
      }),
    );
    const { user } = await openNodes();
    await tick(user, "aud-101-pc01");
    await tick(user, "win-laptop-07");
    const apply = within(toolbar()).getByRole("button", { name: "Применить" });
    expect(apply).toBeDisabled();
    await user.selectOptions(within(toolbar()).getByLabelText("Назначить развёртывание выбранным"), "qwen3-30b-a3b");
    await user.click(apply);
    const dialog = await screen.findByRole("dialog", { name: "Назначить развёртывание qwen3-30b-a3b" });
    await user.click(within(dialog).getByRole("button", { name: "Назначить" }));
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts).toEqual([
      { name: "aud-101-pc01", body: { deployment_id: 2 } },
      { name: "win-laptop-07", body: { deployment_id: 2 } },
    ]);
    const results = await screen.findByRole("list", { name: "Результат по нодам" });
    await waitFor(() => expect(within(results).getAllByText("Назначено")).toHaveLength(2));
  });

  it("can take the assignment off several nodes", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.put("/api/admin/nodes/:name/assignment", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({});
      }),
    );
    const { user } = await openNodes();
    await tick(user, "aud-101-pc01");
    await user.selectOptions(within(toolbar()).getByLabelText("Назначить развёртывание выбранным"), "Снять назначение");
    await user.click(within(toolbar()).getByRole("button", { name: "Применить" }));
    const dialog = await screen.findByRole("dialog", { name: "Снять назначение" });
    await user.click(within(dialog).getByRole("button", { name: "Снять назначение" }));
    await waitFor(() => expect(bodies).toEqual([{ deployment_id: null }]));
  });

  it("warns about a split group when a node of it is moved away", async () => {
    server.use(withSplitGpu());
    const { user } = await openNodes();
    await tick(user, "aud-204-gpu01");
    await user.selectOptions(within(toolbar()).getByLabelText("Назначить развёртывание выбранным"), "gemma-4-e4b");
    await user.click(within(toolbar()).getByRole("button", { name: "Применить" }));
    expect(within(await screen.findByRole("dialog")).getByText(/Группа split будет остановлена/)).toBeInTheDocument();
  });
});

describe("bulk node actions: waking", () => {
  it("wakes only the nodes that are not in the network and shows the answer of each", async () => {
    const woken: string[] = [];
    server.use(
      http.post("/api/admin/nodes/:name/wake", ({ params, request }) => {
        woken.push(`${String(params.name)}:${request.headers.get("X-CSRF-Token")}`);
        return HttpResponse.json({ node: params.name, targets: ["192.168.3.255"], relay: "aud-101-pc01" });
      }),
    );
    const { user } = await openNodes();
    await tick(user, "win-laptop-07"); // down
    await tick(user, "aud-101-pc01"); // ready: left out
    await user.click(within(toolbar()).getByRole("button", { name: "Разбудить" }));
    const dialog = await screen.findByRole("dialog", { name: "Разбудить ноды" });
    expect(within(dialog).getByText("1 нода:")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Разбудить" }));
    const results = await screen.findByRole("list", { name: "Результат по нодам" });
    await waitFor(() => expect(within(results).getByText(/Пакет отправлен, в том числе через ноду aud-101-pc01/)).toBeInTheDocument());
    expect(woken).toEqual(["win-laptop-07:csrf-test-token-admin"]);
  });

  it("shows the refusal of a node that never reported its network card", async () => {
    server.use(
      http.post("/api/admin/nodes/:name/wake", () =>
        HttpResponse.json({ detail: "Нода не сообщила адрес сетевой карты: переустановите её (install-node.ps1)" }, { status: 404 }),
      ),
    );
    const { user } = await openNodes();
    await tick(user, "win-laptop-07");
    await user.click(within(toolbar()).getByRole("button", { name: "Разбудить" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Разбудить" }));
    expect(await screen.findByText(/Нода не сообщила адрес сетевой карты/)).toBeInTheDocument();
  });
});
