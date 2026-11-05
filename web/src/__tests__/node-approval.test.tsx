import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { fixtures } from "../mocks/handlers";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

// Task SEC-1: a node registered under a new name waits for «Подтвердить» (single and bulk); «Сбросить ключ ноды…» in the
// «⋯» menu. Fixture nodes: aud-101-pc01, aud-101-pc02, aud-204-gpu01, win-laptop-07 (all approved, with keys), llm-main.

const pending = () =>
  http.get("/api/admin/nodes", () =>
    HttpResponse.json({
      ...fixtures.nodes,
      nodes: fixtures.nodes.nodes.map((n) =>
        n.name === "win-laptop-07" || n.name === "aud-101-pc02"
          ? { ...n, approved: false, claim_ip: n.name === "win-laptop-07" ? "192.168.3.77" : "192.168.3.42", deployment: null }
          : n,
      ),
    }),
  );

async function openNodes() {
  const view = renderApp("/admin/nodes", "admin");
  await screen.findByRole("row", { name: /win-laptop-07/ });
  return view;
}

const rowOf = (name: RegExp) => screen.getByRole("row", { name });

describe("a node waiting for approval", () => {
  it("is marked with the address it called from and is approved by its own button", async () => {
    const approved: string[] = [];
    server.use(
      pending(),
      http.post("/api/admin/nodes/:name/approve", ({ params }) => {
        approved.push(String(params.name));
        return HttpResponse.json({ approved: [params.name] });
      }),
    );
    const { user } = await openNodes();
    const row = rowOf(/win-laptop-07/);
    expect(within(row).getByText("Ожидает подтверждения")).toBeInTheDocument();
    expect(within(row).getByText("с адреса 192.168.3.77")).toBeInTheDocument();
    // an approved node has no such button
    expect(within(rowOf(/aud-101-pc01/)).queryByRole("button", { name: "Подтвердить" })).not.toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Подтвердить" }));
    // the row's button asks first, with the address (the approval decides who the node is)
    const dialog = await screen.findByRole("dialog", { name: "Подтвердить win-laptop-07" });
    expect(within(dialog).getByText("192.168.3.77")).toBeInTheDocument();
    expect(approved).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Подтвердить" }));
    await waitFor(() => expect(approved).toEqual(["win-laptop-07"]));
  });

  it("claims on a known name are a warning of their own, picked one by one against the known addresses, never in bulk", async () => {
    const approved: unknown[] = [];
    const rejected: string[] = [];
    server.use(
      http.get("/api/admin/nodes", () =>
        HttpResponse.json({
          ...fixtures.nodes,
          nodes: fixtures.nodes.nodes.map((n) =>
            n.name === "aud-101-pc01"
              ? {
                  ...n,
                  known_addresses: ["192.168.3.41"],
                  claims: [
                    { id: 7, ip: "10.66.66.66", at: "2026-11-05T08:00:00+00:00", known: false },
                    { id: 8, ip: "192.168.3.41", at: "2026-11-05T08:01:00+00:00", known: true },
                  ],
                  claims_refused: 2,
                }
              : n,
          ),
        }),
      ),
      http.post("/api/admin/nodes/:name/approve", async ({ params, request }) => {
        approved.push({ name: params.name, body: await request.json() });
        return HttpResponse.json({ approved: [params.name] });
      }),
      http.delete("/api/admin/nodes/:name/claims", ({ request }) => {
        rejected.push(new URL(request.url).search);
        return HttpResponse.json({ node: "aud-101-pc01", rejected: 1 });
      }),
    );
    const { user } = await openNodes();
    const row = rowOf(/aud-101-pc01/);
    expect(within(row).getByText("Попытка перерегистрации известного имени")).toBeInTheDocument();
    expect(within(row).getByText("Готова")).toBeInTheDocument(); // the node keeps working
    expect(within(row).queryByRole("button", { name: "Подтвердить" })).not.toBeInTheDocument();
    // not in the bulk approval
    await user.click(screen.getByRole("checkbox", { name: "Выбрать aud-101-pc01" }));
    const toolbar = screen.getByRole("toolbar", { name: "Действия над выбранными нодами" });
    expect(within(toolbar).getByRole("button", { name: "Подтвердить" })).toBeDisabled();
    // the dialog: the known addresses next to each claim
    await user.click(within(row).getByRole("button", { name: "Заявки: 2" }));
    const dialog = await screen.findByRole("dialog", { name: "Заявки на имя aud-101-pc01" });
    expect(within(dialog).getAllByText("192.168.3.41").length).toBeGreaterThan(0);
    expect(within(dialog).getByText("незнакомый адрес")).toBeInTheDocument();
    expect(within(dialog).getByText(/Ещё 2 регистраций отклонено/)).toBeInTheDocument();
    const rows = within(within(dialog).getByRole("table", { name: "Заявки" })).getAllByRole("row");
    await user.click(within(rows[1]).getByRole("button", { name: "Сделать нодой…" }));
    const confirm = await screen.findByRole("dialog", { name: "Сделать заявку нодой aud-101-pc01" });
    expect(within(confirm).getByText(/не совпадает ни с одним знакомым адресом/)).toBeInTheDocument();
    expect(approved).toEqual([]);
    await user.click(within(confirm).getByRole("button", { name: "Сделать нодой" }));
    await waitFor(() => expect(approved).toEqual([{ name: "aud-101-pc01", body: { claim_id: 7 } }]));
    // rejecting them all
    await user.click(within(rowOf(/aud-101-pc01/)).getByRole("button", { name: "Заявки: 2" }));
    const again = await screen.findByRole("dialog", { name: "Заявки на имя aud-101-pc01" });
    await user.click(within(again).getByRole("button", { name: "Отклонить все" }));
    await waitFor(() => expect(rejected).toEqual([""]));
  });

  it("a node of before without a key is marked, and its key can be reset", async () => {
    server.use(
      http.get("/api/admin/nodes", () =>
        HttpResponse.json({
          ...fixtures.nodes,
          nodes: fixtures.nodes.nodes.map((n) => (n.name === "aud-101-pc01" ? { ...n, has_key: false, legacy: true } : n)),
        }),
      ),
    );
    const { user } = await openNodes();
    expect(within(rowOf(/aud-101-pc01/)).getByText("без ключа (старый образ)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Действия: aud-101-pc01" }));
    expect(await screen.findByRole("menuitem", { name: "Сбросить ключ ноды…" })).toBeInTheDocument();
  });

  it("several are approved at once from the toolbar, only those that wait", async () => {
    const approved: string[] = [];
    server.use(
      pending(),
      http.post("/api/admin/nodes/:name/approve", ({ params }) => {
        approved.push(String(params.name));
        return HttpResponse.json({ approved: [params.name] });
      }),
    );
    const { user } = await openNodes();
    for (const name of ["aud-101-pc01", "aud-101-pc02", "win-laptop-07"]) {
      await user.click(screen.getByRole("checkbox", { name: `Выбрать ${name}` }));
    }
    const toolbar = screen.getByRole("toolbar", { name: "Действия над выбранными нодами" });
    await user.click(within(toolbar).getByRole("button", { name: "Подтвердить" }));
    const dialog = await screen.findByRole("dialog", { name: "Подтвердить ноды" });
    const listed = within(within(dialog).getByRole("list", { name: "Ноды действия" })).getAllByRole("listitem");
    expect(listed.map((li) => li.textContent)).toEqual([
      "aud-101-pc02 — с адреса 192.168.3.42",
      "win-laptop-07 — с адреса 192.168.3.77",
    ]);
    expect(within(dialog).getByText(/сверьте имя и адрес/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Подтвердить" }));
    await screen.findByRole("dialog", { name: "Результат: Подтвердить ноды" });
    await waitFor(() => expect(approved).toEqual(["aud-101-pc02", "win-laptop-07"]));
  });

  it("the toolbar button is off when no selected node waits", async () => {
    const { user } = await openNodes();
    await user.click(screen.getByRole("checkbox", { name: "Выбрать aud-101-pc01" }));
    const toolbar = screen.getByRole("toolbar", { name: "Действия над выбранными нодами" });
    expect(within(toolbar).getByRole("button", { name: "Подтвердить" })).toBeDisabled();
  });
});

describe("«Сбросить ключ ноды…»", () => {
  it("asks first, then resets the key and says the node waits for approval after it registers again", async () => {
    const reset: string[] = [];
    server.use(
      http.post("/api/admin/nodes/:name/reset-key", ({ params }) => {
        reset.push(String(params.name));
        return HttpResponse.json({ node: params.name, approved: false });
      }),
    );
    const { user } = await openNodes();
    await user.click(screen.getByRole("button", { name: "Действия: aud-101-pc01" }));
    await user.click(await screen.findByRole("menuitem", { name: "Сбросить ключ ноды…" }));
    const ask = await screen.findByRole("dialog", { name: "Сбросить ключ aud-101-pc01" });
    expect(within(ask).getByText(/будет ждать подтверждения/)).toBeInTheDocument();
    expect(reset).toEqual([]);
    await user.click(within(ask).getByRole("button", { name: "Сбросить ключ" }));
    await screen.findByRole("dialog", { name: "Ключ aud-101-pc01 сброшен" });
    expect(reset).toEqual(["aud-101-pc01"]);
  });

  it("is not offered for a node without a key", async () => {
    server.use(
      http.get("/api/admin/nodes", () =>
        HttpResponse.json({ ...fixtures.nodes, nodes: fixtures.nodes.nodes.map((n) => ({ ...n, has_key: false })) }),
      ),
    );
    const { user } = await openNodes();
    await user.click(screen.getByRole("button", { name: "Действия: aud-101-pc01" }));
    const menu = await screen.findByRole("menu", { name: "Действия: aud-101-pc01" });
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["Удалить из кластера…"]);
  });
});
