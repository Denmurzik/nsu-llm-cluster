import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { scheduleText } from "../pages/AdminBackups";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";
import type { BackupEntry, BackupsData } from "../types";

const T1 = "20261101T031700Z";
const T2 = "20261103T211700Z";

const entry = (ts: string, extra: Partial<BackupEntry> = {}): BackupEntry => ({
  name: ts,
  kind: "set",
  at: `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)}T${ts.slice(9, 11)}:${ts.slice(11, 13)}:00+00:00`,
  size_bytes: 5 * 2 ** 30,
  complete: true,
  restore_command: `docker exec -it llm-main /opt/llm/main/backup/pg-restore.sh ${ts}`,
  files: [
    { name: `litellm-${ts}.dump`, db: "litellm", size_bytes: 2 * 2 ** 30, sha256: null },
    { name: `portal-${ts}.dump`, db: "portal", size_bytes: 3 * 2 ** 30, sha256: "ab".repeat(32) },
  ],
  ...extra,
});

function data(patch: Partial<BackupsData> = {}): BackupsData {
  return {
    available: true,
    items: [entry(T2), entry(T1)],
    disk: { free_bytes: 40 * 2 ** 30, total_bytes: 100 * 2 ** 30 },
    schedule: { cron: "17 21 * * *", timezone: "UTC", suspended: false, last_schedule_at: "2026-11-03T21:17:00+00:00" },
    retention_keep: 14,
    running: false,
    last_run: { name: "postgres-backup-29", state: "succeeded", manual: false, started_at: "2026-11-03T21:17:00+00:00", finished_at: "2026-11-03T21:19:00+00:00" },
    ...patch,
  };
}

function serve(current: () => BackupsData) {
  server.use(http.get("/api/admin/backups", () => HttpResponse.json(current())));
}

describe("backups page", () => {
  it("is reached from the admin menu and is for admins only", async () => {
    serve(() => data());
    const { user } = renderApp("/admin/status", "admin");
    await user.click(await screen.findByRole("link", { name: "Резервные копии" }));
    expect(await screen.findByRole("heading", { name: "Резервные копии", level: 1 })).toBeInTheDocument();
  });

  it("refuses a user", async () => {
    renderApp("/admin/backups", "user");
    expect(await screen.findByText("Недостаточно прав")).toBeInTheDocument();
  });

  it("lists the copies with kind, size and a download link per file", async () => {
    serve(() => data({ items: [entry(T2), entry(T1, { kind: "pre-restore", name: `pre-restore-${T1}`, restore_command: null })] }));
    renderApp("/admin/backups", "admin");
    const table = await screen.findByRole("table", { name: "Резервные копии" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Ежедневная копия")).toBeInTheDocument();
    expect(within(rows[0]).getByText("5,0 ГБ")).toBeInTheDocument();
    const link = within(rows[0]).getByRole("link", { name: `Скачать portal-${T2}.dump` });
    expect(link).toHaveAttribute("href", `/api/admin/backups/portal-${T2}.dump/download`);
    expect(link).toHaveAttribute("download");
    expect(within(rows[0]).getByText(/sha256 abababababababab…/)).toBeInTheDocument();
    expect(within(rows[1]).getByText("Перед восстановлением")).toBeInTheDocument();
    expect(within(rows[1]).queryByRole("button", { name: /Как восстановить/ })).not.toBeInTheDocument();
  });

  it("says when there are no copies and when the folder is unreadable", async () => {
    serve(() => data({ items: [], available: false }));
    renderApp("/admin/backups", "admin");
    expect(await screen.findByText("Каталог копий недоступен")).toBeInTheDocument();
    expect(screen.getByText("Копий пока нет")).toBeInTheDocument();
  });

  it("shows the schedule, retention, free space and the last run", async () => {
    serve(() => data());
    renderApp("/admin/backups", "admin");
    expect(await screen.findByText("каждый день в 21:17 UTC")).toBeInTheDocument();
    expect(screen.getByText("14 последних наборов")).toBeInTheDocument();
    expect(screen.getByText("40,0 ГБ из 100,0 ГБ")).toBeInTheDocument();
    expect(screen.getByText(/успешно/)).toBeInTheDocument();
  });

  it("warns about a failed last run and names the log command", async () => {
    serve(() =>
      data({
        last_run: { name: "postgres-backup-manual-1", state: "failed", manual: true, started_at: "2026-11-03T21:17:00+00:00", finished_at: null },
      }),
    );
    renderApp("/admin/backups", "admin");
    expect(await screen.findByText(/закончился ошибкой/)).toBeInTheDocument();
    expect(screen.getByText(/logs job\/postgres-backup-manual-1/)).toBeInTheDocument();
  });

  it("says when Kubernetes cannot be asked", async () => {
    serve(() => data({ schedule: null, retention_keep: null, running: null, last_run: null }));
    renderApp("/admin/backups", "admin");
    expect(await screen.findByText("Нет данных: Kubernetes недоступен")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Сделать копию сейчас" })).toBeEnabled();
  });

  it("starts a copy and reloads the list", async () => {
    let posts = 0;
    server.use(
      http.post("/api/admin/backups", () => {
        posts += 1;
        return HttpResponse.json({ job: "postgres-backup-manual-20261104-100000" }, { status: 202 });
      }),
    );
    serve(() => data());
    const { user } = renderApp("/admin/backups", "admin");
    await user.click(await screen.findByRole("button", { name: "Сделать копию сейчас" }));
    expect(await screen.findByText(/Копия запущена/)).toBeInTheDocument();
    expect(posts).toBe(1);
  });

  it("disables the button while a copy is being made", async () => {
    serve(() => data({ running: true }));
    renderApp("/admin/backups", "admin");
    const button = await screen.findByRole("button", { name: "Копия делается" });
    expect(button).toBeDisabled();
  });

  it("shows the refusal of a second copy", async () => {
    server.use(http.post("/api/admin/backups", () => HttpResponse.json({ detail: "Копия уже делается: дождитесь её окончания" }, { status: 409 })));
    serve(() => data());
    const { user } = renderApp("/admin/backups", "admin");
    await user.click(await screen.findByRole("button", { name: "Сделать копию сейчас" }));
    expect(await screen.findByText("Копия уже делается: дождитесь её окончания")).toBeInTheDocument();
  });

  it("asks before deleting and deletes the chosen set", async () => {
    let items = [entry(T2), entry(T1)];
    const deleted: string[] = [];
    server.use(
      http.delete("/api/admin/backups/:name", ({ params }) => {
        deleted.push(String(params.name));
        items = items.filter((e) => e.name !== params.name);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    serve(() => data({ items }));
    const { user } = renderApp("/admin/backups", "admin");
    const table = await screen.findByRole("table", { name: "Резервные копии" });
    const first = within(table).getAllByRole("row")[1];
    await user.click(within(first).getByRole("button", { name: /Удалить копию/ }));
    const dialog = await screen.findByRole("dialog", { name: "Удаление копии" });
    expect(within(dialog).getByText(/нельзя отменить/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    expect(deleted).toEqual([]);

    await user.click(within(first).getByRole("button", { name: /Удалить копию/ }));
    await user.click(within(await screen.findByRole("dialog", { name: "Удаление копии" })).getByRole("button", { name: "Удалить" }));
    await waitFor(() => expect(deleted).toEqual([T2]));
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(2));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows why a delete failed", async () => {
    server.use(http.delete("/api/admin/backups/:name", () => HttpResponse.json({ detail: "Такой копии нет" }, { status: 404 })));
    serve(() => data());
    const { user } = renderApp("/admin/backups", "admin");
    const table = await screen.findByRole("table", { name: "Резервные копии" });
    await user.click(within(within(table).getAllByRole("row")[1]).getByRole("button", { name: /Удалить копию/ }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Удалить" }));
    expect(await screen.findByText("Такой копии нет")).toBeInTheDocument();
  });

  it("shows the exact restore command with a copy button", async () => {
    serve(() => data());
    const { user } = renderApp("/admin/backups", "admin");
    const table = await screen.findByRole("table", { name: "Резервные копии" });
    await user.click(within(within(table).getAllByRole("row")[2]).getByRole("button", { name: /Как восстановить/ }));
    const dialog = await screen.findByRole("dialog", { name: "Восстановление из копии" });
    expect(within(dialog).getByText(`docker exec -it llm-main /opt/llm/main/backup/pg-restore.sh ${T1}`)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Копировать команду" })).toBeInTheDocument();
    expect(within(dialog).getByText(/Всё, что записано после этой копии, пропадёт/)).toBeInTheDocument();
  });

  it("does not offer a restore of an incomplete set", async () => {
    serve(() => data({ items: [entry(T2, { complete: false, restore_command: null })] }));
    renderApp("/admin/backups", "admin");
    const button = await screen.findByRole("button", { name: /Как восстановить/ });
    expect(button).toBeDisabled();
    expect(screen.getByText("Неполный набор: есть не все базы")).toBeInTheDocument();
  });
});

describe("scheduleText", () => {
  it("reads a daily cron and keeps any other as it is", () => {
    expect(scheduleText("17 21 * * *")).toBe("каждый день в 21:17 UTC");
    expect(scheduleText("5 3 * * *")).toBe("каждый день в 03:05 UTC");
    expect(scheduleText("0 */6 * * *")).toBe("по расписанию cron «0 */6 * * *», время UTC");
  });
});
