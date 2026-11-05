import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { fmtBytes } from "../lib/format";
import { fixtures } from "../mocks/handlers";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

// «Очистить модели» (task 2-26): the size column, the row button, the bulk action, the confirmation and the results.
// Fixture nodes: aud-101-pc01 ready, 41.2 GB held; aud-101-pc02 on maintenance, unknown; aud-204-gpu01 ready, 18.6 GB;
// win-laptop-07 down, empty; llm-main is the main one.

/** a size the way the page writes it, whatever space it puts before the unit; `before`/`after` are regexp text */
const size = (n: number, before = "", after = "") =>
  new RegExp(before + fmtBytes(n).replace(/\s/g, String.raw`\s`) + after);

const answer = (node: string, over: Record<string, unknown> = {}) => ({
  node, status: "done", freed_bytes: 12884901888, models_bytes: 1, removed: [], skipped: [], ...over,
});

async function openNodes() {
  const view = renderApp("/admin/nodes", "admin");
  await screen.findByRole("row", { name: /aud-204-gpu01/ });
  return view;
}

const rowOf = (name: RegExp) => screen.getByRole("row", { name });
const toolbar = () => screen.getByRole("toolbar", { name: "Действия над выбранными нодами" });
const tick = (user: ReturnType<typeof renderApp>["user"], node: string) =>
  user.click(screen.getByRole("checkbox", { name: `Выбрать ${node}` }));

describe("models on disk: the column", () => {
  it("shows how much each node holds, a dash for one that has not reported, nothing for the main node", async () => {
    await openNodes();
    expect(screen.getByRole("columnheader", { name: "Модели на диске" })).toBeInTheDocument();
    expect(within(rowOf(/aud-101-pc01/)).getByText(size(41200000000, "^", "$"))).toBeInTheDocument();
    expect(within(rowOf(/aud-204-gpu01/)).getByText(size(18600000000, "^", "$"))).toBeInTheDocument();
    expect(within(rowOf(/win-laptop-07/)).getByText(size(0, "^", "$"))).toBeInTheDocument();
    const cells = within(rowOf(/aud-101-pc02/)).getAllByRole("cell");
    expect(cells[6]).toHaveTextContent("—"); // pick, node, accel, VRAM, RAM, capacity, then this column
    expect(screen.getByText(/Модели на диске: файлы моделей и кеш тензоров/)).toBeInTheDocument();
  });
});

describe("«Очистить модели» in the row", () => {
  it("is offered for every node but the main one, and is off for a node that is down", async () => {
    await openNodes();
    expect(within(rowOf(/llm-main/)).queryByRole("button", { name: /Очистить модели/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Очистить модели aud-101-pc01" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Очистить модели aud-101-pc02" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Очистить модели win-laptop-07" })).toBeDisabled();
  });

  it("asks first: names the node, what it holds and what stays; Cancel calls nothing", async () => {
    let calls = 0;
    server.use(http.post("/api/admin/nodes/:name/purge-models", () => (calls++, HttpResponse.json(answer("x")))));
    const { user } = await openNodes();
    await user.click(screen.getByRole("button", { name: "Очистить модели aud-101-pc01" }));
    const dialog = await screen.findByRole("dialog", { name: "Очистить модели на aud-101-pc01" });
    expect(within(dialog).getByText(size(41200000000, "занято "))).toBeInTheDocument();
    expect(within(dialog).getByText(/Нода с развёртыванием сохранит то, что оно использует/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Нода без развёртывания очищается полностью/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(calls).toBe(0);
  });

  it("POSTs with CSRF, shows what was freed and refreshes the table", async () => {
    const sent: { name: string; csrf: string | null }[] = [];
    let listed = 0;
    server.use(
      http.post("/api/admin/nodes/:name/purge-models", ({ request, params }) => {
        sent.push({ name: String(params.name), csrf: request.headers.get("X-CSRF-Token") });
        return HttpResponse.json(answer(String(params.name)));
      }),
      http.get("/api/admin/nodes", () => (listed++, HttpResponse.json(fixtures.nodes))),
    );
    const { user } = await openNodes();
    const before = listed;
    await user.click(screen.getByRole("button", { name: "Очистить модели aud-101-pc01" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Очистить" }));
    expect(await within(rowOf(/aud-101-pc01/)).findByRole("status")).toHaveTextContent(size(12884901888, "Освобождено "));
    expect(sent).toEqual([{ name: "aud-101-pc01", csrf: "csrf-test-token-admin" }]);
    await waitFor(() => expect(listed).toBeGreaterThan(before));
  });

  it("says so when there was nothing to remove, and when the node has not answered yet", async () => {
    let n = 0;
    server.use(
      http.post("/api/admin/nodes/:name/purge-models", ({ params }) =>
        HttpResponse.json(
          n++ === 0
            ? answer(String(params.name), { freed_bytes: 0 })
            : answer(String(params.name), { status: "pending", freed_bytes: null, models_bytes: null }),
        ),
      ),
    );
    const { user } = await openNodes();
    await user.click(screen.getByRole("button", { name: "Очистить модели aud-101-pc01" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Очистить" }));
    expect(await within(rowOf(/aud-101-pc01/)).findByRole("status")).toHaveTextContent("Удалять было нечего");
    await user.click(screen.getByRole("button", { name: "Очистить модели aud-204-gpu01" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Очистить" }));
    expect(await within(rowOf(/aud-204-gpu01/)).findByRole("status")).toHaveTextContent("нода ещё не ответила");
  });

  it("shows the reason when the cleanup failed", async () => {
    server.use(
      http.post("/api/admin/nodes/:name/purge-models", () =>
        HttpResponse.json({ detail: "Очистка не выполнена: задание на ноде не выполнено" }, { status: 502 }),
      ),
    );
    const { user } = await openNodes();
    await user.click(screen.getByRole("button", { name: "Очистить модели aud-101-pc01" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Очистить" }));
    expect(await within(rowOf(/aud-101-pc01/)).findByRole("alert")).toHaveTextContent(
      "Очистка не выполнена: задание на ноде не выполнено",
    );
  });
});

describe("«Очистить модели» for the selected nodes", () => {
  it("is off until a node that is up is ticked", async () => {
    const { user } = await openNodes();
    expect(within(toolbar()).queryByRole("button", { name: "Очистить модели" })).not.toBeInTheDocument();
    await tick(user, "win-laptop-07");
    expect(within(toolbar()).getByRole("button", { name: "Очистить модели" })).toBeDisabled();
    await tick(user, "aud-101-pc01");
    expect(within(toolbar()).getByRole("button", { name: "Очистить модели" })).toBeEnabled();
  });

  it("lists the nodes that are up with what they hold, says what stays, then cleans them one by one", async () => {
    const calls: { name: string; csrf: string | null }[] = [];
    server.use(
      http.post("/api/admin/nodes/:name/purge-models", ({ request, params }) => {
        const name = String(params.name);
        calls.push({ name, csrf: request.headers.get("X-CSRF-Token") });
        if (name === "aud-204-gpu01") {
          return HttpResponse.json({ detail: "Нода не в сети: очистку выполняет сама нода" }, { status: 409 });
        }
        return HttpResponse.json(answer(name));
      }),
    );
    const { user } = await openNodes();
    await tick(user, "aud-101-pc01");
    await tick(user, "aud-204-gpu01");
    await tick(user, "win-laptop-07"); // down: left out
    await user.click(within(toolbar()).getByRole("button", { name: "Очистить модели" }));

    const dialog = await screen.findByRole("dialog", { name: "Очистить модели" });
    expect(within(dialog).getByText("2 ноды:")).toBeInTheDocument();
    const items = within(within(dialog).getByRole("list", { name: "Ноды действия" })).getAllByRole("listitem");
    expect(items.map((i) => i.textContent)).toEqual([
      `aud-101-pc01 — занято ${fmtBytes(41200000000)}`,
      `aud-204-gpu01 — занято ${fmtBytes(18600000000)}`,
    ]);
    expect(within(dialog).getByText(size(41200000000 + 18600000000, "занимают "))).toBeInTheDocument();
    expect(within(dialog).getByText(/Нода с развёртыванием сохранит то, что оно использует/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/Группа split будет остановлена/)).not.toBeInTheDocument();
    expect(calls).toEqual([]); // nothing before the confirmation

    await user.click(within(dialog).getByRole("button", { name: "Очистить" }));
    const results = await screen.findByRole("list", { name: "Результат по нодам" });
    await waitFor(() => expect(within(results).getByText("Нода не в сети: очистку выполняет сама нода")).toBeInTheDocument());
    expect(within(results).getByText(size(12884901888, "^Освобождено ", "$"))).toBeInTheDocument();
    expect(calls).toEqual([
      { name: "aud-101-pc01", csrf: "csrf-test-token-admin" },
      { name: "aud-204-gpu01", csrf: "csrf-test-token-admin" },
    ]);
    // the node that failed stays selected, the one that was cleaned does not
    await user.click(screen.getByRole("button", { name: "Закрыть" }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "Выбрать aud-204-gpu01" })).toBeChecked());
    expect(screen.getByRole("checkbox", { name: "Выбрать aud-101-pc01" })).not.toBeChecked();
  });
});
