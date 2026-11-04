import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { fixtures } from "../mocks/handlers";
import { renderApp } from "../test/render";

const extra = [
  // the controller gave up on this one; this other one never reported its network card
  { name: "aud-301-pc05", wakeable: true, wake: { tries: 6, max_tries: 6, since: "2026-11-03T11:00:00+00:00" } },
  { name: "aud-301-pc06", wakeable: false, wake: null },
];

/** both pages list two more down class PCs */
function withDownNodes() {
  const node = (base: Record<string, unknown>, e: (typeof extra)[number]) => ({
    ...base, ...e, ready: false, deployment: null, backends: 0, maintenance: false, is_main: false,
  });
  const nodes = JSON.parse(JSON.stringify(fixtures.nodes));
  nodes.nodes.push(...extra.map((e) => node(nodes.nodes[1], e)));
  const status = JSON.parse(JSON.stringify(fixtures.status));
  status.nodes.push(...extra.map((e) => node(status.nodes[1], e)));
  server.use(
    http.get("/api/admin/nodes", () => HttpResponse.json(nodes)),
    http.get("/api/admin/status", () => HttpResponse.json(status)),
  );
}

// Wake-on-LAN (task 2-18): the controller's attempts and the «Разбудить» button on «Ноды» and «Состояние»
describe.each([
  { page: "/admin/nodes", table: "Ноды" },
  { page: "/admin/status", table: "Состояние нод" },
])("wake-on-lan on $page", ({ page, table }) => {
  beforeEach(withDownNodes);

  it("shows the attempt of a node being woken", async () => {
    renderApp(page, "admin");
    const rows = await screen.findByRole("table", { name: table });
    const lost = within(rows).getByRole("row", { name: /win-laptop-07/ });
    expect(within(lost).getByText("будим (попытка 2)")).toBeInTheDocument();
    const gaveUp = within(rows).getByRole("row", { name: /aud-301-pc05/ });
    expect(within(gaveUp).getByText("не просыпается (попыток: 6)")).toBeInTheDocument();
  });

  it("offers «Разбудить» only for a down node that reported its network card", async () => {
    renderApp(page, "admin");
    const rows = await screen.findByRole("table", { name: table });
    expect(within(rows).getByRole("button", { name: "Разбудить win-laptop-07" })).toBeInTheDocument();
    expect(within(rows).queryByRole("button", { name: "Разбудить aud-204-gpu01" })).not.toBeInTheDocument(); // ready
    expect(within(rows).queryByRole("button", { name: "Разбудить aud-301-pc06" })).not.toBeInTheDocument(); // no NIC
  });

  it("POSTs the wake with CSRF and says where the packet went", async () => {
    let sent: { name: string; csrf: string | null } | null = null;
    server.use(
      http.post("/api/admin/nodes/:name/wake", ({ request, params }) => {
        sent = { name: String(params.name), csrf: request.headers.get("X-CSRF-Token") };
        return HttpResponse.json({ node: params.name, targets: ["192.168.3.255", "192.168.3.77"], relay: null });
      }),
    );
    const { user } = renderApp(page, "admin");
    await user.click(await screen.findByRole("button", { name: "Разбудить win-laptop-07" }));
    await waitFor(() => expect(sent).toEqual({ name: "win-laptop-07", csrf: "csrf-test-token-admin" }));
    expect(await screen.findByText("Пакет пробуждения отправлен: 192.168.3.255, 192.168.3.77")).toBeInTheDocument();
  });

  it("names the live node of the subnet that also sent it", async () => {
    server.use(
      http.post("/api/admin/nodes/:name/wake", ({ params }) =>
        HttpResponse.json({ node: params.name, targets: ["192.168.3.255"], relay: "aud-101-pc01" }),
      ),
    );
    const { user } = renderApp(page, "admin");
    await user.click(await screen.findByRole("button", { name: "Разбудить win-laptop-07" }));
    expect(await screen.findByText("Пакет пробуждения отправлен: 192.168.3.255, и через ноду aud-101-pc01")).toBeInTheDocument();
  });

  it("shows the server's reason when the packet could not be sent", async () => {
    server.use(
      http.post("/api/admin/nodes/:name/wake", () =>
        HttpResponse.json({ detail: "Пакет пробуждения не отправлен: 192.168.3.255:9: Network is unreachable" }, { status: 502 }),
      ),
    );
    const { user } = renderApp(page, "admin");
    await user.click(await screen.findByRole("button", { name: "Разбудить win-laptop-07" }));
    expect(await screen.findByText(/Network is unreachable/)).toBeInTheDocument();
  });
});
