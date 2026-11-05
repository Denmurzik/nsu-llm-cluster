import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setCsrfToken } from "../api";
import { NodeUpdateButton, NodeVersionTag, updateResultText } from "../components/NodeVersions";
import { shortDigest, versionText, type NodeVersion, type VersionsResponse } from "../lib/nodeVersions";
import { fixtures, state } from "../mocks/handlers";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

const node = (patch: Partial<NodeVersion> = {}): NodeVersion => ({
  ...(fixtures.versions.nodes[1] as NodeVersion),
  ...patch,
});

describe("version text", () => {
  it("joins the version and the commit, else the start of the image digest", () => {
    expect(versionText({ version: "0.1.0", revision: "9f3c2ab4d1e07b65", digest: null })).toBe("0.1.0 · 9f3c2ab");
    expect(versionText({ version: "0.1.0", revision: null, digest: "sha256:a41c7e90b2d35f68c1a0e4b7" })).toBe("0.1.0 · a41c7e90b2d3");
    expect(versionText({ version: "0.1.0", revision: "unknown", digest: null })).toBe("0.1.0");
    expect(versionText({ version: null, revision: null, digest: null })).toBe("нет данных");
    expect(shortDigest("sha256:0123456789abcdef0123")).toBe("0123456789ab");
  });
});

describe("version tag of a node", () => {
  it("is a quiet tag, and «устарела» only for a node that differs from the registry", () => {
    const { rerender } = render(<NodeVersionTag info={node({ outdated: true })} />);
    expect(screen.getByText("0.0.9 · 1c2d3e4")).toBeInTheDocument();
    expect(screen.getByText("устарела")).toBeInTheDocument();
    rerender(<NodeVersionTag info={node({ outdated: false })} />);
    expect(screen.queryByText("устарела")).not.toBeInTheDocument();
    rerender(<NodeVersionTag info={node({ outdated: null })} />);
    expect(screen.queryByText("устарела")).not.toBeInTheDocument(); // unknown is not a verdict
    rerender(<NodeVersionTag info={undefined} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("tells that an update is requested and waits for the deployment", () => {
    const update = { when: "idle" as const, at: "2026-11-04T11:00:00+00:00", by: "admin@g.nsu.ru", waiting: true };
    render(<NodeVersionTag info={node({ outdated: true, update, updater_seen_at: new Date().toISOString() })} />);
    expect(screen.getByText(/Обновление запрошено: после выключения развёртывания, ждёт выключения развёртывания/)).toBeInTheDocument();
    expect(screen.queryByText(/не спрашивает главного/)).not.toBeInTheDocument();
  });

  it("says when the node does not ask main for updates at all", () => {
    const update = { when: "now" as const, at: "2026-11-04T11:00:00+00:00", by: "admin@g.nsu.ru", waiting: false };
    render(<NodeVersionTag info={node({ outdated: true, update, updater_seen_at: null })} />);
    expect(screen.getByText(/Обновление запрошено: сейчас/)).toBeInTheDocument();
    expect(screen.getByText(/Нода не спрашивает главного об обновлениях/)).toBeInTheDocument();
  });

  it("shows why the last update failed", () => {
    render(<NodeVersionTag info={node({ outdated: true, update_error: "образ не удалось скачать с главного" })} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Обновление не удалось: образ не удалось скачать с главного");
  });
});

describe("«Обновить» of a node", () => {
  beforeEach(() => {
    state.role = "admin";
    setCsrfToken("csrf-test-token-admin");
  });

  it("is offered for an outdated or unknown node, not for one that is up to date", () => {
    const { rerender } = render(<NodeUpdateButton node="aud-101-pc02" info={node({ outdated: true })} onDone={() => {}} />);
    expect(screen.getByRole("button", { name: "Обновить aud-101-pc02" })).toBeInTheDocument();
    rerender(<NodeUpdateButton node="aud-101-pc02" info={node({ outdated: null })} onDone={() => {}} />);
    expect(screen.getByRole("button", { name: "Обновить aud-101-pc02" })).toBeInTheDocument();
    rerender(<NodeUpdateButton node="aud-101-pc02" info={node({ outdated: false })} onDone={() => {}} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    rerender(<NodeUpdateButton node="aud-101-pc02" info={undefined} onDone={() => {}} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("asks when, defaulting to after the deployment is off, and sends the request with CSRF", async () => {
    let sent: { name: string; body: unknown; csrf: string | null } | null = null;
    server.use(
      http.post("/api/admin/nodes/:name/update", async ({ request, params }) => {
        sent = { name: String(params.name), body: await request.json(), csrf: request.headers.get("X-CSRF-Token") };
        return HttpResponse.json({ name: params.name, ok: true, when: "idle", waiting: true });
      }),
    );
    const done = vi.fn();
    const user = userEvent.setup();
    render(<NodeUpdateButton node="aud-101-pc02" info={node({ outdated: true })} onDone={done} />);
    await user.click(screen.getByRole("button", { name: "Обновить aud-101-pc02" }));
    const dialog = await screen.findByRole("dialog", { name: "Обновить aud-101-pc02" });
    expect(within(dialog).getByRole("radio", { name: /После выключения развёртывания/ })).toBeChecked();
    expect(within(dialog).getByRole("radio", { name: /Сейчас/ })).not.toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "Обновить" }));
    await waitFor(() => expect(sent).toEqual({ name: "aud-101-pc02", body: { when: "idle", force: false }, csrf: "csrf-test-token-admin" }));
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("sends «сейчас» when the admin chooses it", async () => {
    let body: unknown = null;
    server.use(
      http.post("/api/admin/nodes/:name/update", async ({ request, params }) => {
        body = await request.json();
        return HttpResponse.json({ name: params.name, ok: true, when: "now", waiting: false });
      }),
    );
    const user = userEvent.setup();
    render(<NodeUpdateButton node="aud-101-pc02" info={node({ outdated: true })} onDone={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Обновить aud-101-pc02" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("radio", { name: /Сейчас/ }));
    await user.click(within(dialog).getByRole("button", { name: "Обновить" }));
    await waitFor(() => expect(body).toEqual({ when: "now", force: false }));
  });

  it("keeps the dialog and shows the reason when main refuses", async () => {
    server.use(
      http.post("/api/admin/nodes/:name/update", () =>
        HttpResponse.json({ detail: "В реестре главного нет образа ноды" }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    render(<NodeUpdateButton node="aud-101-pc02" info={node({ outdated: true })} onDone={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Обновить aud-101-pc02" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Обновить" }));
    expect(await within(dialog).findByText("В реестре главного нет образа ноды")).toBeInTheDocument();
  });

  it("takes a request back", async () => {
    let cancelled: string | null = null;
    server.use(
      http.delete("/api/admin/nodes/:name/update", ({ params }) => {
        cancelled = String(params.name);
        return HttpResponse.json({ name: params.name, cancelled: true });
      }),
    );
    const done = vi.fn();
    const update = { when: "idle" as const, at: null, by: null, waiting: true };
    const user = userEvent.setup();
    render(<NodeUpdateButton node="aud-101-pc02" info={node({ outdated: true, update })} onDone={done} />);
    expect(screen.queryByRole("button", { name: "Обновить aud-101-pc02" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Отменить обновление aud-101-pc02" }));
    await waitFor(() => expect(cancelled).toBe("aud-101-pc02"));
    await waitFor(() => expect(done).toHaveBeenCalled());
  });

  it("words the result of a bulk request per node", () => {
    expect(updateResultText({ name: "a", ok: true, when: "idle", waiting: true })).toBe("Запрошено, ждёт выключения развёртывания");
    expect(updateResultText({ name: "a", ok: true, when: "now", waiting: false })).toBe("Запрошено, нода обновится сейчас");
    expect(updateResultText({ name: "a", ok: false, error: "Нода не найдена" })).toBe("Нода не найдена");
  });
});

describe("«Версии» on the status page", () => {
  it("shows main, the tags of the registry and the nodes grouped by version", async () => {
    renderApp("/admin/status", "admin");
    const block = await screen.findByTestId("versions");
    expect(within(block).getByRole("heading", { name: "Версии" })).toBeInTheDocument();
    const main = within(block).getByRole("table", { name: "Версия главного" });
    expect(within(main).getByText("0.1.0 · 9f3c2ab")).toBeInTheDocument();
    expect(within(main).getByText(/Выключено: ноды обновляются кнопкой «Обновить»/)).toBeInTheDocument();

    const tags = within(block).getByRole("table", { name: "Образы в реестре" });
    expect(within(tags).getAllByRole("row")).toHaveLength(1 + fixtures.versions.registry.tags.length);
    const gpu = within(tags).getByRole("row", { name: /0\.1\.0-gpu/ });
    expect(within(gpu).getByText("для нод")).toBeInTheDocument();
    expect(within(gpu).getByText("5b1a0c2e7d4f")).toBeInTheDocument();
    const old = within(tags).getByRole("row", { name: /0\.0\.9-cpu/ });
    expect(within(old).queryByText("для нод")).not.toBeInTheDocument();

    const groups = within(block).getByRole("table", { name: "Ноды по версиям" });
    const stale = within(groups).getByRole("row", { name: /aud-101-pc02/ });
    expect(within(stale).getByText("0.0.9 · 1c2d3e4")).toBeInTheDocument();
    expect(within(stale).getByText("устарела")).toBeInTheDocument();
    const fresh = within(groups).getByRole("row", { name: /aud-101-pc01/ });
    expect(within(fresh).queryByText("устарела")).not.toBeInTheDocument();
    const unknown = within(groups).getByRole("row", { name: /win-laptop-07/ });
    expect(within(unknown).queryByText("устарела")).not.toBeInTheDocument();
  });

  it("warns when the registry of main does not answer, and keeps the rest of the page", async () => {
    const down: VersionsResponse = JSON.parse(JSON.stringify(fixtures.versions));
    down.registry = { repo: "nsu-llm-node", available: false, error: "ConnectError: refused", tags: [] };
    down.auto_update = true;
    server.use(http.get("/api/admin/versions", () => HttpResponse.json(down)));
    renderApp("/admin/status", "admin");
    const block = await screen.findByTestId("versions");
    expect(within(block).getByText(/Реестр образов главного не отвечает \(ConnectError: refused\)/)).toBeInTheDocument();
    expect(within(block).getByText("Реестр недоступен")).toBeInTheDocument();
    expect(within(block).getByText(/Включено: нода заменяет контейнер сама/)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Состояние нод" })).toBeInTheDocument();
  });

  it("does not hide the status page when the versions cannot be read", async () => {
    server.use(http.get("/api/admin/versions", () => HttpResponse.json({ detail: "сбой" }, { status: 500 })));
    renderApp("/admin/status", "admin");
    expect(await screen.findByRole("table", { name: "Состояние нод" })).toBeInTheDocument();
    expect(await screen.findByText(/Не удалось получить версии/)).toBeInTheDocument();
  });
});

describe("versions on the nodes page", () => {
  it("shows a version tag per node, «устарела» for the outdated one, and the version of main", async () => {
    renderApp("/admin/nodes", "admin");
    const table = await screen.findByRole("table", { name: "Ноды" });
    expect(within(table).getByRole("columnheader", { name: "Версия" })).toBeInTheDocument();
    const stale = await within(table).findByRole("row", { name: /aud-101-pc02/ });
    expect(await within(stale).findByText("устарела")).toBeInTheDocument();
    expect(within(stale).getByText("0.0.9 · 1c2d3e4")).toBeInTheDocument();
    const fresh = within(table).getByRole("row", { name: /aud-101-pc01/ });
    expect(within(fresh).getByText("0.1.0 · 9f3c2ab")).toBeInTheDocument();
    expect(within(fresh).queryByText("устарела")).not.toBeInTheDocument();
    const unknown = within(table).getByRole("row", { name: /win-laptop-07/ });
    expect(within(unknown).queryByText("устарела")).not.toBeInTheDocument();
    const main = within(table).getByRole("row", { name: /llm-main/ });
    expect(within(main).getByText("0.1.0 · 9f3c2ab")).toBeInTheDocument();
    expect(within(main).queryByRole("button", { name: /Обновить/ })).not.toBeInTheDocument();
  });

  it("offers «Обновить» for a node that is not up to date or unknown, not for one that is current and not for main", async () => {
    renderApp("/admin/nodes", "admin");
    const table = await screen.findByRole("table", { name: "Ноды" });
    expect(await within(table).findByRole("button", { name: "Обновить aud-101-pc02" })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "Обновить win-laptop-07" })).toBeInTheDocument();
    expect(within(table).queryByRole("button", { name: "Обновить aud-101-pc01" })).not.toBeInTheDocument();
    expect(within(table).queryByRole("button", { name: "Обновить llm-main" })).not.toBeInTheDocument();
  });

  it("keeps the table when the versions cannot be read", async () => {
    server.use(http.get("/api/admin/versions", () => HttpResponse.json({ detail: "сбой" }, { status: 500 })));
    renderApp("/admin/nodes", "admin");
    const table = await screen.findByRole("table", { name: "Ноды" });
    expect(await within(table).findByRole("row", { name: /aud-101-pc02/ })).toBeInTheDocument();
    expect(within(table).queryByText("устарела")).not.toBeInTheDocument();
  });

  it("updates the ticked nodes with one request, leaving out the one that is current", async () => {
    let sent: { names: string[]; when: string; csrf: string | null } | null = null;
    server.use(
      http.post("/api/admin/nodes/update", async ({ request }) => {
        const body = (await request.json()) as { names: string[]; when: string };
        sent = { ...body, csrf: request.headers.get("X-CSRF-Token") };
        return HttpResponse.json({
          results: [
            { name: "aud-101-pc02", ok: true, when: "now", waiting: false },
            { name: "win-laptop-07", ok: false, error: "В реестре главного нет образа ноды" },
          ],
        });
      }),
    );
    const { user } = renderApp("/admin/nodes", "admin");
    const table = await screen.findByRole("table", { name: "Ноды" });
    await within(table).findByRole("button", { name: "Обновить aud-101-pc02" });
    const bar = screen.getByRole("group", { name: "Обновление образа выбранных нод" });
    expect(within(bar).getByText("Отметьте ноды, чтобы обновить их образ")).toBeInTheDocument();
    for (const name of ["aud-101-pc01", "aud-101-pc02", "win-laptop-07"]) await user.click(within(table).getByRole("checkbox", { name: `Выбрать ${name}` }));
    expect(within(bar).getByText("Образ: 2 из 3 требует обновления")).toBeInTheDocument();
    await user.click(within(bar).getByRole("button", { name: "Обновить образ" }));
    const dialog = await screen.findByRole("dialog", { name: "Обновить образ нод" });
    expect(within(within(dialog).getByRole("list", { name: "Ноды обновления" })).getAllByRole("listitem")).toHaveLength(2);
    await user.click(within(dialog).getByRole("radio", { name: /Сейчас/ }));
    await user.click(within(dialog).getByRole("button", { name: "Обновить" }));
    await waitFor(() => expect(sent).toEqual({ names: ["aud-101-pc02", "win-laptop-07"], when: "now", csrf: "csrf-test-token-admin" }));
    const result = await screen.findByRole("list", { name: "Результат по нодам" });
    expect(within(result).getByText("Запрошено, нода обновится сейчас")).toBeInTheDocument();
    expect(within(result).getByText("В реестре главного нет образа ноды")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Закрыть" }));
    // the node that could not be asked stays ticked
    expect(within(table).getByRole("checkbox", { name: "Выбрать win-laptop-07" })).toBeChecked();
    expect(within(table).getByRole("checkbox", { name: "Выбрать aud-101-pc02" })).not.toBeChecked();
  });
});
