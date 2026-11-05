import { screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { fixtures } from "../mocks/handlers";
import { renderApp } from "../test/render";

type Status = typeof fixtures.status;
const withStatus = (patch: (s: Status) => void) => {
  const copy = JSON.parse(JSON.stringify(fixtures.status)) as Status;
  patch(copy);
  server.use(http.get("/api/admin/status", () => HttpResponse.json(copy)));
};

describe("admin status page", () => {
  it("is reached from the admin menu", async () => {
    const { user } = renderApp("/admin/nodes", "admin");
    await user.click(await screen.findByRole("link", { name: "Состояние" }));
    expect(await screen.findByRole("heading", { name: "Состояние", level: 1 })).toBeInTheDocument();
  });

  it("is for admins only", async () => {
    renderApp("/admin/status", "user");
    expect(await screen.findByText("Недостаточно прав")).toBeInTheDocument();
  });

  it("lists nodes with accelerator, memory, heartbeat age and state", async () => {
    renderApp("/admin/status", "admin");
    const table = await screen.findByRole("table", { name: "Состояние нод" });
    const gpu = within(table).getByRole("row", { name: /aud-204-gpu01/ });
    expect(within(gpu).getByText("nvidia")).toBeInTheDocument();
    expect(within(gpu).getByText("12,0 ГБ")).toBeInTheDocument();
    expect(within(gpu).getByText("32,0 ГБ")).toBeInTheDocument();
    expect(within(gpu).getByText("9 с назад")).toBeInTheDocument();
    expect(within(gpu).getByText("Готова")).toBeInTheDocument();

    const maint = within(table).getByRole("row", { name: /aud-101-pc02/ });
    expect(within(maint).getByText("На обслуживании")).toBeInTheDocument();

    const lost = within(table).getByRole("row", { name: /win-laptop-07/ });
    expect(within(lost).getByText("Не в сети")).toBeInTheDocument();
    expect(within(lost).getByText("21 мин назад")).toBeInTheDocument();

    expect(within(within(table).getByRole("row", { name: /llm-main/ })).getByText("главная")).toBeInTheDocument();
  });

  it("shows a node without a heartbeat as unknown", async () => {
    withStatus((s) => {
      (s.nodes[1] as { heartbeat_age_s: number | null }).heartbeat_age_s = null;
    });
    renderApp("/admin/status", "admin");
    const table = await screen.findByRole("table", { name: "Состояние нод" }); // the «Версии» block names the nodes too
    const row = within(table).getByRole("row", { name: /aud-204-gpu01/ });
    expect(within(row).getByText("нет данных")).toBeInTheDocument();
  });

  it("lists deployments with their state and ready backends", async () => {
    renderApp("/admin/status", "admin");
    const table = await screen.findByRole("table", { name: "Состояние развёртываний" });
    const ready = within(table).getByRole("row", { name: /gemma-4-e4b/ });
    expect(within(ready).getByText("Готова")).toBeInTheDocument();
    expect(within(ready).getByText("2 экземпляра")).toBeInTheDocument();
    const split = within(table).getByRole("row", { name: /qwen3-30b-a3b/ });
    expect(within(split).getByText("Запускается")).toBeInTheDocument();
    expect(within(split).getByText("Разделение")).toBeInTheDocument();
    const off = within(table).getByRole("row", { name: /llama-3-8b/ });
    expect(within(off).getByText("Выключено")).toBeInTheDocument();
  });

  it("shows the gateway counters for 5 minutes and an hour", async () => {
    renderApp("/admin/status", "admin");
    const table = await screen.findByRole("table", { name: "Отказы шлюза" });
    const limited = within(table).getByRole("row", { name: /429/ });
    expect(within(limited).getByText("12")).toBeInTheDocument();
    expect(within(limited).getByText("340")).toBeInTheDocument();
    const errors = within(table).getByRole("row", { name: /5xx/ });
    expect(within(errors).getByText("0")).toBeInTheDocument();
    expect(within(errors).getByText("3")).toBeInTheDocument();
  });

  it("says so when the counters cannot be read", async () => {
    withStatus((s) => {
      s.gateway = { available: false, last_5m: null, last_1h: null } as unknown as Status["gateway"];
    });
    renderApp("/admin/status", "admin");
    expect(await screen.findByText("Счётчики шлюза недоступны")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Отказы шлюза" })).not.toBeInTheDocument();
  });

  it("reports the main server's GPU and the latest backup", async () => {
    renderApp("/admin/status", "admin");
    expect(await screen.findByText("NVIDIA GeForce RTX 4090, 24564 MiB")).toBeInTheDocument();
    const backup = screen.getByTestId("backup-summary");
    expect(backup).toHaveTextContent("03.11.2026");
    expect(backup).toHaveTextContent("50 МБ");
    expect(backup).toHaveTextContent("Хранится наборов: 14");
    expect(screen.queryByText(/Последней копии больше суток/)).not.toBeInTheDocument();
  });

  it("warns about a stale backup and about a missing GPU", async () => {
    withStatus((s) => {
      s.main.gpu = null as unknown as string;
      s.backups.last_age_s = 3 * 24 * 3600;
    });
    renderApp("/admin/status", "admin");
    expect(await screen.findByText("Не обнаружена")).toBeInTheDocument();
    expect(screen.getByText(/Последней копии больше полутора суток/)).toBeInTheDocument();
  });

  it("warns about a backup only past the threshold the text names (one and a half days)", async () => {
    withStatus((s) => {
      s.backups.last_age_s = 1.2 * 24 * 3600;
    });
    renderApp("/admin/status", "admin");
    expect(await screen.findByTestId("backup-summary")).toBeInTheDocument();
    expect(screen.queryByText(/Последней копии больше/)).not.toBeInTheDocument();
  });

  it("keeps the last data and says so when a refresh fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let calls = 0;
      server.use(
        http.get("/api/admin/status", () => {
          calls += 1;
          return calls === 1
            ? HttpResponse.json(fixtures.status)
            : HttpResponse.json({ detail: "Ошибка 500" }, { status: 500 });
        }),
      );
      renderApp("/admin/status", "admin");
      expect(await screen.findByRole("table", { name: "Состояние нод" })).toBeInTheDocument();
      expect(screen.queryByText(/Не удалось обновить данные/)).not.toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(10_000);
      expect(await screen.findByText(/Не удалось обновить данные/)).toBeInTheDocument();
      expect(screen.getByRole("table", { name: "Состояние нод" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("says there are no backups yet", async () => {
    withStatus((s) => {
      s.backups = {
        available: true,
        count: 0,
        last_at: null,
        last_age_s: null,
        last_size_bytes: null,
      } as unknown as Status["backups"];
    });
    renderApp("/admin/status", "admin");
    expect(await screen.findByText("Копий пока нет")).toBeInTheDocument();
  });

  it("says when the backup directory cannot be read", async () => {
    withStatus((s) => {
      s.backups = {
        available: false,
        count: 0,
        last_at: null,
        last_age_s: null,
        last_size_bytes: null,
      } as unknown as Status["backups"];
    });
    renderApp("/admin/status", "admin");
    expect(await screen.findByText("Каталог копий недоступен")).toBeInTheDocument();
  });

  it("warns when the cluster is unavailable", async () => {
    withStatus((s) => {
      s.k8s_available = false;
    });
    renderApp("/admin/status", "admin");
    expect(await screen.findByText(/Kubernetes недоступен/)).toBeInTheDocument();
  });

  it("shows the server's error", async () => {
    server.use(http.get("/api/admin/status", () => HttpResponse.json({ detail: "Ошибка 500" }, { status: 500 })));
    renderApp("/admin/status", "admin");
    expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка 500");
  });
});
