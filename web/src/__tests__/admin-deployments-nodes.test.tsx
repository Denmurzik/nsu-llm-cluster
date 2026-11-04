import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { nodesCount } from "../components/DeploymentNodes";
import { server } from "../mocks/server";
import { fixtures, state } from "../mocks/handlers";
import { renderApp } from "../test/render";

type NodeOver = Partial<(typeof fixtures.nodes.nodes)[number]>;
const node = (name: string, over: NodeOver = {}) => ({
  ...fixtures.nodes.nodes[1],
  name,
  deployment: null,
  ready: true,
  maintenance: false,
  backends: 1,
  ...over,
});
const nodesOf = (list: ReturnType<typeof node>[]) =>
  http.get("/api/admin/nodes", () => HttpResponse.json({ ...fixtures.nodes, nodes: list }));

const pod = (ready: boolean) => ({
  name: "p", role: "inference", phase: "Running", ready, restarts: 0, message: "", last_log: null, progress: null,
});
const statusOf = (groups: string[][], pods: Record<string, boolean>) =>
  http.get("/api/admin/deployments/:id/status", () =>
    HttpResponse.json({
      ...fixtures.deploymentStatus,
      nodes: Object.entries(pods).map(([name, ready]) => ({ name, source: "k8s", applied: ready, pods: [pod(ready)] })),
      groups,
    }),
  );

const rowOf = (name: RegExp) => screen.getByRole("row", { name });
/** the nodes are a line of their own right under the row of the deployment */
const lineOf = (name: RegExp) => rowOf(name).nextElementSibling as HTMLElement;
const dotOf = (el: HTMLElement) => el.querySelector("i.dot") as HTMLElement;
const stored = (id: number) => state.deployments.find((d) => d.id === id) as { id: number; enabled: boolean };

async function openList() {
  const view = renderApp("/admin/deployments", "admin");
  await screen.findByRole("row", { name: /gemma-4-e4b/ });
  return view;
}

describe("nodes count", () => {
  it("says «нода», «ноды», «нод»", () => {
    expect([1, 2, 5, 11, 21, 24].map(nodesCount)).toEqual(["1 нода", "2 ноды", "5 нод", "11 нод", "21 нода", "24 ноды"]);
  });
});

describe("nodes of a deployment in the list", () => {
  it("shows the nodes of a replicas deployment with a state dot and the count, with no status calls", async () => {
    const statusCalls: string[] = [];
    server.use(
      http.get("/api/admin/deployments/:id/status", ({ params }) => {
        statusCalls.push(String(params.id));
        return HttpResponse.json(fixtures.deploymentStatus);
      }),
    );
    await openList();
    const row = lineOf(/gemma-4-e4b/);
    const pc01 = await within(row).findByTitle("aud-101-pc01, готова");
    expect(dotOf(pc01)).toHaveClass("ready");
    expect(within(pc01).getByText("aud-101-pc01")).toBeInTheDocument();
    const pc02 = within(row).getByTitle("aud-101-pc02, на обслуживании");
    expect(dotOf(pc02)).toHaveClass("starting");
    expect(dotOf(within(row).getByTitle("aud-204-gpu01, готова"))).toHaveClass("ready");
    expect(within(row).getByText("3 ноды")).toBeInTheDocument();

    expect(within(lineOf(/llama-3-1-8b/)).getByText("Нет нод")).toBeInTheDocument();
    // replicas and a split deployment with no nodes need no status
    await new Promise((r) => setTimeout(r, 50));
    expect(statusCalls).toEqual([]);
  });

  it("tells a node that loads the model from one that is down", async () => {
    server.use(
      nodesOf([
        node("a-ready", { deployment: "gemma-4-e4b", backends: 2 }),
        node("b-loading", { deployment: "gemma-4-e4b", backends: 0 }),
        node("c-down", { deployment: "gemma-4-e4b", ready: false, backends: 0 }),
      ]),
    );
    await openList();
    const row = lineOf(/gemma-4-e4b/);
    expect(dotOf(await within(row).findByTitle("a-ready, готова"))).toHaveClass("ready");
    expect(dotOf(within(row).getByTitle("b-loading, загружает модель"))).toHaveClass("starting");
    expect(dotOf(within(row).getByTitle("c-down, не в сети"))).toHaveClass("down");
  });

  it("does not call the nodes of a switched-off deployment ready", async () => {
    stored(1).enabled = false;
    await openList();
    const row = lineOf(/gemma-4-e4b/);
    const gpu = await within(row).findByTitle("aud-204-gpu01, развёртывание выключено");
    expect(dotOf(gpu)).toHaveClass("unknown");
    expect(within(rowOf(/gemma-4-e4b/)).getByText("Выключено")).toBeInTheDocument();
  });

  it("keeps the row compact: four nodes and «+N» for the rest, the count says all of them", async () => {
    const six = ["n1", "n2", "n3", "n4", "n5", "n6"].map((n) => node(n, { deployment: "gemma-4-e4b" }));
    server.use(nodesOf(six));
    await openList();
    const row = lineOf(/gemma-4-e4b/);
    await within(row).findByTitle("n4, готова");
    expect(within(row).queryByTitle("n5, готова")).toBeNull();
    const more = within(row).getByText("+2");
    expect(more).toHaveAttribute("title", "n5, n6");
    expect(more).toHaveTextContent("ещё: n5, n6");
    expect(within(row).getByText("6 нод")).toBeInTheDocument();
  });

  it("takes the names from the assignment in the list while the nodes are not read", async () => {
    server.use(
      http.get("/api/admin/deployments", () =>
        HttpResponse.json(fixtures.deployments.map((d) => (d.id === 1 ? { ...d, assigned_nodes: ["lonely-pc"] } : d))),
      ),
      nodesOf([]),
    );
    await openList();
    const lonely = await within(lineOf(/gemma-4-e4b/)).findByTitle("lonely-pc, нет данных");
    expect(dotOf(lonely)).toHaveClass("unknown");
    expect(within(lineOf(/gemma-4-e4b/)).getByText("1 нода")).toBeInTheDocument();
  });
});

describe("nodes line label and failure", () => {
  it("names the line «Ноды» for a screen reader", async () => {
    await openList();
    const line = lineOf(/gemma-4-e4b/);
    expect(await within(line).findByRole("group", { name: "Ноды" })).toBeInTheDocument();
  });

  it("says so above the table when the nodes cannot be read", async () => {
    server.use(http.get("/api/admin/nodes", () => HttpResponse.json({ detail: "Кластер не отвечает" }, { status: 502 })));
    renderApp("/admin/deployments", "admin");
    await screen.findByRole("row", { name: /gemma-4-e4b/ });
    expect(await screen.findByText("Ноды не загрузились")).toBeInTheDocument();
    expect(within(lineOf(/gemma-4-e4b/)).getByText("—")).toBeInTheDocument();
  });
});

describe("split groups in the list", () => {
  const split = [
    node("nsu-4060", { deployment: "qwen3-30b-a3b", backends: 0 }),
    node("nsu-4070", { deployment: "qwen3-30b-a3b", backends: 1 }),
    node("nsu-spare", { deployment: "qwen3-30b-a3b", backends: 0 }),
  ];

  it("shows a group as «a + b» with the leader first, states from the pods, a spare node apart", async () => {
    const calls: string[] = [];
    server.use(
      nodesOf(split),
      http.get("/api/admin/deployments/:id/status", ({ params }) => {
        calls.push(String(params.id));
        return undefined;
      }),
      statusOf([["nsu-4070", "nsu-4060"]], { "nsu-4070": true, "nsu-4060": false }),
    );
    await openList();
    const row = lineOf(/qwen3-30b-a3b/);
    const group = await within(row).findByRole("group", { name: "Группа разделения: nsu-4070 + nsu-4060" });
    const tags = within(group).getAllByTitle(/, /);
    expect(tags.map((t) => t.title)).toEqual(["nsu-4070, готова", "nsu-4060, загружает модель"]);
    expect(within(group).getByText("+")).toBeInTheDocument();
    expect(dotOf(tags[0])).toHaveClass("ready");
    expect(dotOf(tags[1])).toHaveClass("starting");

    const spare = within(row).getByTitle("nsu-spare, в резерве");
    expect(group).not.toContainElement(spare);
    expect(within(row).getByText("3 ноды")).toBeInTheDocument();
    // status of the split row only
    expect(calls).toEqual(["2"]);
  });

  it("shows the nodes at once and the groups when the status arrives", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => void (release = r));
    server.use(
      nodesOf(split),
      http.get("/api/admin/deployments/:id/status", async () => {
        await gate;
        return HttpResponse.json({
          ...fixtures.deploymentStatus,
          nodes: [
            { name: "nsu-4070", source: "k8s", applied: true, pods: [pod(true)] },
            { name: "nsu-4060", source: "k8s", applied: true, pods: [pod(true)] },
          ],
          groups: [["nsu-4070", "nsu-4060"]],
        });
      }),
    );
    await openList();
    const row = lineOf(/qwen3-30b-a3b/);
    const early = await within(row).findByTitle("nsu-4060, состояние загружается");
    expect(dotOf(early)).toHaveClass("unknown");
    expect(within(row).queryByRole("group", { name: /Группа разделения/ })).toBeNull();

    release();
    expect(await within(row).findByRole("group", { name: "Группа разделения: nsu-4070 + nsu-4060" })).toBeInTheDocument();
    expect(dotOf(within(row).getByTitle("nsu-4060, готова"))).toHaveClass("ready");
  });

  it("keeps the plain list when the status cannot be read", async () => {
    server.use(
      nodesOf(split),
      http.get("/api/admin/deployments/:id/status", () => HttpResponse.json({ detail: "Кластер не отвечает" }, { status: 502 })),
    );
    await openList();
    const row = lineOf(/qwen3-30b-a3b/);
    expect(await within(row).findByTitle("nsu-4070, нет данных")).toBeInTheDocument();
    expect(within(row).getByText("3 ноды")).toBeInTheDocument();
    expect(within(row).queryByRole("group", { name: /Группа разделения/ })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not read the status of a switched-off split deployment", async () => {
    stored(2).enabled = false;
    const calls: string[] = [];
    server.use(
      nodesOf(split),
      http.get("/api/admin/deployments/:id/status", ({ params }) => {
        calls.push(String(params.id));
        return HttpResponse.json(fixtures.deploymentStatus);
      }),
    );
    await openList();
    const row = lineOf(/qwen3-30b-a3b/);
    expect(await within(row).findByTitle("nsu-4070, развёртывание выключено")).toBeInTheDocument();
    expect(calls).toEqual([]);
  });
});
