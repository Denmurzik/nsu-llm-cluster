import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { fixtures, state } from "../mocks/handlers";
import { renderApp } from "../test/render";

async function openList() {
  const view = renderApp("/admin/deployments", "admin");
  await screen.findByRole("row", { name: /gemma-4-e4b/ });
  return view;
}

/** the deployment as the mock server holds it: what the next GET of the list returns */
const stored = (id: number) => state.deployments.find((d) => d.id === id) as { id: number; enabled: boolean };

const rowOf = (name: RegExp) => screen.getByRole("row", { name });
const menuOf = (row: HTMLElement, name: string) => within(row).getByRole("button", { name: `Действия: ${name}` });

describe("row actions of the deployment list", () => {
  it("keeps «Проверить» in view and puts the rest into a menu with an accessible name", async () => {
    const { user } = await openList();
    const row = rowOf(/gemma-4-e4b/);
    expect(within(row).getByRole("button", { name: "Проверить" })).toBeEnabled();
    expect(screen.queryByRole("menu")).toBeNull();

    const trigger = menuOf(row, "gemma-4-e4b");
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    const rowsBefore = screen.getAllByRole("row").length;

    await user.click(trigger);
    const menu = screen.getByRole("menu", { name: "Действия: gemma-4-e4b" });
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["Выключить", "Удалить"]);
    expect(within(menu).getByRole("menuitem", { name: "Выключить" })).toHaveFocus();
    // a layer over the page: the table has the same rows and the menu is not inside it
    expect(screen.getAllByRole("row").length).toBe(rowsBefore);
    expect(screen.getByRole("table", { name: "Развёртывания" })).not.toContainElement(menu);
  });

  it("moves between the items with arrows, closes on Escape and gives the focus back", async () => {
    const { user } = await openList();
    const trigger = menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b");
    await user.click(trigger);
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Удалить" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Выключить" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await user.click(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("Tab in the menu gives the focus back to the button and closes the menu", async () => {
    const { user } = await openList();
    const trigger = menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b");
    await user.click(trigger);
    expect(screen.getByRole("menuitem", { name: "Выключить" })).toHaveFocus();
    await user.tab();
    expect(screen.queryByRole("menu")).toBeNull();
    // not on the body: the focus was given back to the button, so Tab moved on from the row
    expect(document.body).not.toHaveFocus();
  });

  it("keeps the focus on the button while a switch runs and after it", async () => {
    const { user } = await openList();
    const trigger = menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b");
    await user.click(trigger);
    await user.click(screen.getByRole("menuitem", { name: "Выключить" }));
    await waitFor(() => expect(within(rowOf(/gemma-4-e4b/)).getByText("Выключено")).toBeInTheDocument());
    expect(menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b")).toHaveFocus();
  });

  it("switches a deployment off at once with a PATCH of enabled and the row shows it", async () => {
    let seen: { id: string; csrf: string | null; body: unknown } | null = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ params, request }) => {
        const body = (await request.json()) as { enabled: boolean };
        seen = { id: String(params.id), csrf: request.headers.get("X-CSRF-Token"), body };
        stored(Number(params.id)).enabled = body.enabled;
        return HttpResponse.json({ ...fixtures.deploymentDetail, ...stored(Number(params.id)) });
      }),
    );
    const { user } = await openList();
    const row = rowOf(/gemma-4-e4b/);
    expect(within(row).getByText("Готова")).toBeInTheDocument();

    await user.click(menuOf(row, "gemma-4-e4b"));
    await user.click(screen.getByRole("menuitem", { name: "Выключить" }));
    // a replicas deployment is not asked about
    expect(screen.queryByRole("dialog")).toBeNull();

    await waitFor(() => expect(within(rowOf(/gemma-4-e4b/)).getByText("Выключено")).toBeInTheDocument());
    expect(seen).toEqual({ id: "1", csrf: "csrf-test-token-admin", body: { enabled: false } });
    expect(within(rowOf(/gemma-4-e4b/)).getByRole("button", { name: "Проверить" })).toBeDisabled();

    await user.click(menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b"));
    expect(screen.getByRole("menuitem", { name: "Включить" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Выключить" })).toBeNull();
  });

  it("switches a disabled deployment on and the row shows it", async () => {
    stored(3).enabled = false;
    let body: unknown = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ params, request }) => {
        body = await request.json();
        stored(Number(params.id)).enabled = true;
        return HttpResponse.json({ ...fixtures.deploymentDetail, ...stored(Number(params.id)) });
      }),
    );
    const { user } = renderApp("/admin/deployments", "admin");
    const row = await screen.findByRole("row", { name: /llama-3-1-8b/ });
    expect(within(row).getByText("Выключено")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Проверить" })).toBeDisabled();

    await user.click(menuOf(row, "llama-3-1-8b"));
    expect(screen.queryByRole("menuitem", { name: "Выключить" })).toBeNull();
    await user.click(screen.getByRole("menuitem", { name: "Включить" }));
    await waitFor(() => expect(within(screen.getByRole("row", { name: /llama-3-1-8b/ })).queryByText("Выключено")).toBeNull());
    expect(body).toEqual({ enabled: true });
    expect(within(screen.getByRole("row", { name: /llama-3-1-8b/ })).getByRole("button", { name: "Проверить" })).toBeEnabled();
  });

  it("asks before switching a split group off and does nothing on cancel", async () => {
    let patches = 0;
    let body: unknown = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        patches += 1;
        body = await request.json();
        stored(2).enabled = false;
        return HttpResponse.json({ ...fixtures.deploymentDetail, ...stored(2) });
      }),
    );
    const { user } = await openList();
    await user.click(menuOf(rowOf(/qwen3-30b-a3b/), "qwen3-30b-a3b"));
    await user.click(screen.getByRole("menuitem", { name: "Выключить" }));
    const dialog = await screen.findByRole("dialog", { name: "Выключение развёртывания" });
    expect(dialog).toHaveTextContent("Выключить развёртывание «qwen3-30b-a3b»?");
    expect(dialog).toHaveTextContent("Поды остановятся, модель пропадёт из каталога, ноды освободятся.");
    expect(patches).toBe(0);

    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(patches).toBe(0);

    await user.click(menuOf(rowOf(/qwen3-30b-a3b/), "qwen3-30b-a3b"));
    await user.click(screen.getByRole("menuitem", { name: "Выключить" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Выключить" }));
    await waitFor(() => expect(patches).toBe(1));
    expect(body).toEqual({ enabled: false });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("switching a split group on needs no question", async () => {
    stored(2).enabled = false;
    let body: unknown = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        body = await request.json();
        stored(2).enabled = true;
        return HttpResponse.json({ ...fixtures.deploymentDetail, ...stored(2) });
      }),
    );
    const { user } = renderApp("/admin/deployments", "admin");
    const row = await screen.findByRole("row", { name: /qwen3-30b-a3b/ });
    await user.click(menuOf(row, "qwen3-30b-a3b"));
    await user.click(screen.getByRole("menuitem", { name: "Включить" }));
    await waitFor(() => expect(body).toEqual({ enabled: true }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("deletes a deployment only after the confirmation and the row goes away", async () => {
    let deleted: { id: string; csrf: string | null } | null = null;
    server.use(
      http.delete("/api/admin/deployments/:id", ({ params, request }) => {
        deleted = { id: String(params.id), csrf: request.headers.get("X-CSRF-Token") };
        state.deployments = state.deployments.filter((d) => d.id !== 1);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { user } = await openList();
    await user.click(menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b"));
    await user.click(screen.getByRole("menuitem", { name: "Удалить" }));

    const dialog = await screen.findByRole("dialog", { name: "Удаление развёртывания" });
    expect(dialog).toHaveTextContent("Удалить развёртывание «gemma-4-e4b»? Это действие нельзя отменить.");
    expect(dialog).toHaveTextContent("Поды остановятся, модель пропадёт из каталога, ноды освободятся.");
    expect(deleted).toBeNull();

    await user.click(within(dialog).getByRole("button", { name: "Удалить" }));
    await waitFor(() => expect(screen.queryByRole("row", { name: /gemma-4-e4b/ })).toBeNull());
    expect(deleted).toEqual({ id: "1", csrf: "csrf-test-token-admin" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("row", { name: /qwen3-30b-a3b/ })).toBeInTheDocument();
  });

  it("cancel in the delete dialog changes nothing", async () => {
    let deletes = 0;
    server.use(
      http.delete("/api/admin/deployments/:id", () => {
        deletes += 1;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { user } = await openList();
    await user.click(menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b"));
    await user.click(screen.getByRole("menuitem", { name: "Удалить" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(deletes).toBe(0);
    expect(screen.getByRole("row", { name: /gemma-4-e4b/ })).toBeInTheDocument();
    // the focus is back on the «⋯» button of the row
    expect(menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b")).toHaveFocus();
  });

  it("shows the server error of a failed switch-off and leaves the row as it was", async () => {
    server.use(
      http.patch("/api/admin/deployments/:id", () =>
        HttpResponse.json({ detail: "Кластер не отвечает, изменение не применено" }, { status: 502 }),
      ),
    );
    const { user } = await openList();
    await user.click(menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b"));
    await user.click(screen.getByRole("menuitem", { name: "Выключить" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Кластер не отвечает, изменение не применено");
    const row = rowOf(/gemma-4-e4b/);
    expect(within(row).getByText("Готова")).toBeInTheDocument();
    expect(within(row).queryByText("Выключено")).toBeNull();
    // the menu works again
    expect(menuOf(row, "gemma-4-e4b")).toBeEnabled();
  });

  it("shows the server error of a failed delete, closes the dialog and keeps the row", async () => {
    server.use(
      http.delete("/api/admin/deployments/:id", () =>
        HttpResponse.json({ detail: "Не удалось удалить поды: кластер не отвечает" }, { status: 502 }),
      ),
    );
    const { user } = await openList();
    await user.click(menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b"));
    await user.click(screen.getByRole("menuitem", { name: "Удалить" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Удалить" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось удалить поды: кластер не отвечает");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("row", { name: /gemma-4-e4b/ })).toBeInTheDocument();
  });

  it("clears an earlier error when the next action starts", async () => {
    server.use(
      http.patch("/api/admin/deployments/:id", () => HttpResponse.json({ detail: "Первая попытка не прошла" }, { status: 502 }), {
        once: true,
      }),
    );
    const { user } = await openList();
    await user.click(menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b"));
    await user.click(screen.getByRole("menuitem", { name: "Выключить" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Первая попытка не прошла");

    await user.click(menuOf(rowOf(/gemma-4-e4b/), "gemma-4-e4b"));
    await user.click(screen.getByRole("menuitem", { name: "Выключить" }));
    await waitFor(() => expect(screen.queryByText("Первая попытка не прошла")).toBeNull());
  });
});
