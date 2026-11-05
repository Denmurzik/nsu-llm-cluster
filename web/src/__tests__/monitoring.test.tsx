import { screen, waitFor, within } from "@testing-library/react";
import { delay, http, HttpResponse } from "msw";
import { fixtures, historyFor } from "../mocks/handlers";
import { server } from "../mocks/server";
import { renderApp, pageText } from "../test/render";

type Live = typeof fixtures.metricsLive;
const withLive = (patch: (l: Live) => void) => {
  const copy = JSON.parse(JSON.stringify(fixtures.metricsLive)) as Live;
  patch(copy);
  server.use(http.get("/api/admin/metrics/live", () => HttpResponse.json(copy)));
};

/** the first node of the picker (aud-101-pc02) is a CPU one in the fixture; the charts of a GPU need it to have one */
const firstNodeHasGpu = () => withLive((l) => void (l.nodes.find((n) => n.name === "aud-101-pc02")!.gpu_count = 1));

describe("live load of the nodes on «Состояние»", () => {
  it("shows GPU, VRAM, RAM and temperature bars per node", async () => {
    renderApp("/admin/status", "admin");
    const table = await screen.findByRole("table", { name: "Нагрузка нод" });
    await within(table).findByText(/llama-server запущен/);
    const row = within(table).getByText("aud-204-gpu01").closest("tr")!;
    const bar = (name: string) => within(row).getByRole("progressbar", { name });
    expect(bar("GPU aud-204-gpu01")).toHaveAttribute("aria-valuenow", "63");
    expect(bar("VRAM aud-204-gpu01")).toHaveAttribute("aria-valuenow", "80");
    expect(bar("RAM aud-204-gpu01")).toHaveAttribute("aria-valuenow", "44");
    expect(bar("Температура aud-204-gpu01")).toHaveAttribute("aria-valuenow", "71");
    expect(within(row).getByText("63 %")).toBeInTheDocument();
    expect(within(row).getByText("9,6 ГБ из 12,0 ГБ")).toBeInTheDocument();
    expect(within(row).getByText("14,0 ГБ из 32,0 ГБ")).toBeInTheDocument();
    expect(within(row).getByText("71 °C")).toBeInTheDocument();
    expect(within(row).getByText(/llama-server запущен, замер 9 с назад/)).toBeInTheDocument();
    // the bar fills by the share
    expect((bar("GPU aud-204-gpu01").querySelector("i") as HTMLElement).style.width).toBe("63%");
  });

  it("shows a node without a GPU with empty GPU bars, and lists no main server", async () => {
    renderApp("/admin/status", "admin");
    const table = await screen.findByRole("table", { name: "Нагрузка нод" });
    const row = (await within(table).findByText("aud-101-pc02")).closest("tr")!;
    await within(row).findByText("нет GPU");
    expect(within(row).getByText("процессы модели не запущены, замер 16 с назад")).toBeInTheDocument();
    expect(within(row).getByRole("progressbar", { name: "VRAM aud-101-pc02" })).not.toHaveAttribute("aria-valuenow");
    expect(within(row).getByRole("progressbar", { name: "RAM aud-101-pc02" })).toHaveAttribute("aria-valuenow", "38");
    expect(within(table).queryByText("llm-main")).not.toBeInTheDocument();
  });

  it("says a node that sent nothing has no samples", async () => {
    renderApp("/admin/status", "admin");
    const table = await screen.findByRole("table", { name: "Нагрузка нод" });
    const row = (await within(table).findByText("win-laptop-07")).closest("tr")!;
    await within(row).findByText("нет замеров");
  });

  it("marks a nearly full VRAM or a hot GPU", async () => {
    withLive((l) => {
      Object.assign(l.nodes[0], { vram_used_mib: 12000, gpu_temp_c: 90 });
    });
    renderApp("/admin/status", "admin");
    const table = await screen.findByRole("table", { name: "Нагрузка нод" });
    await within(table).findByText("11,7 ГБ из 12,0 ГБ");
    const row = within(table).getByText("aud-204-gpu01").closest("tr")!;
    expect(within(row).getByRole("progressbar", { name: "VRAM aud-204-gpu01" })).toHaveClass("over");
    expect(within(row).getByRole("progressbar", { name: "Температура aud-204-gpu01" })).toHaveClass("over");
    expect(within(row).getByRole("progressbar", { name: "GPU aud-204-gpu01" })).not.toHaveClass("over");
  });

  it("keeps the rows and the bars in place while the samples load", async () => {
    server.use(
      http.get("/api/admin/metrics/live", async () => {
        await delay(250);
        return HttpResponse.json(fixtures.metricsLive);
      }),
    );
    renderApp("/admin/status", "admin");
    const table = await screen.findByRole("table", { name: "Нагрузка нод" });
    const before = within(table).getAllByRole("row").length;
    expect(within(table).getByRole("progressbar", { name: "GPU aud-204-gpu01" })).toHaveAttribute("aria-valuetext", "нет данных");
    await within(table).findByText("63 %");
    expect(within(table).getAllByRole("row")).toHaveLength(before);
  });

  it("tells when the samples cannot be had and still shows the status", async () => {
    server.use(http.get("/api/admin/metrics/live", () => HttpResponse.json({ detail: "x" }, { status: 500 })));
    renderApp("/admin/status", "admin");
    expect(await screen.findByText("Замеры нагрузки недоступны")).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Состояние нод" })).toBeInTheDocument();
  });

  it("tells when the live store cannot be read", async () => {
    withLive((l) => {
      l.available = false;
      l.nodes = [];
    });
    renderApp("/admin/status", "admin");
    expect(await screen.findByText("Хранилище замеров недоступно, нагрузка нод не показана")).toBeInTheDocument();
  });
});

describe("«Мониторинг» page", () => {
  it("is in the admin menu", async () => {
    const { user } = renderApp("/admin/status", "admin");
    await user.click(await screen.findByRole("link", { name: "Мониторинг" }));
    expect(await screen.findByRole("heading", { name: "Мониторинг", level: 1 })).toBeInTheDocument();
  });

  it("refuses a user", async () => {
    renderApp("/admin/monitoring", "user");
    expect(await screen.findByText("Недостаточно прав")).toBeInTheDocument();
  });

  it("draws the charts of the first node and of the first deployment for the last hour", async () => {
    firstNodeHasGpu();
    const urls: URL[] = [];
    server.use(
      http.get("/api/admin/metrics/history", ({ request }) => {
        urls.push(new URL(request.url));
        return HttpResponse.json(historyFor(new URL(request.url)));
      }),
    );
    renderApp("/admin/monitoring", "admin");
    const nodeSection = await screen.findByRole("region", { name: "Графики ноды" });
    for (const title of ["GPU, загрузка", "VRAM", "Оперативная память", "Температура GPU", "CPU, загрузка"]) {
      expect(await within(nodeSection).findByRole("figure", { name: title })).toBeInTheDocument();
    }
    const modelSection = screen.getByRole("region", { name: "Графики развёртывания" });
    for (const title of [
      "Запросы в минуту",
      "Генерация, токенов в секунду",
      "Обработка запроса, токенов в секунду",
      "Время до первого токена",
      "Очередь и занятые слоты",
      "Ошибки в минуту",
    ]) {
      expect(await within(modelSection).findByRole("figure", { name: title })).toBeInTheDocument();
    }
    await waitFor(() => expect(urls.length).toBeGreaterThanOrEqual(2));
    const node = urls.find((u) => u.searchParams.get("kind") === "node")!;
    expect(node.searchParams.get("name")).toBe("aud-101-pc02");
    const span = Date.parse(node.searchParams.get("to")!) - Date.parse(node.searchParams.get("from")!);
    expect(span).toBe(3600_000);
    expect(urls.find((u) => u.searchParams.get("kind") === "model")!.searchParams.get("name")).toBe("gemma-4-e4b");
  });

  it("draws a line for the points and writes the latest value in the head", async () => {
    firstNodeHasGpu();
    renderApp("/admin/monitoring", "admin");
    const fig = await screen.findByRole("figure", { name: "GPU, загрузка" });
    await waitFor(() => expect(fig.querySelector("path.line")?.getAttribute("d")).toMatch(/^M[\d. L]+$/));
    // the last point of the fixture is 8 %
    expect(within(fig).getByText("8 %")).toBeInTheDocument();
    expect(fig.querySelectorAll("line.grid")).toHaveLength(3);
  });

  it("asks for a day and a week when the period is switched", async () => {
    firstNodeHasGpu();
    const spans: number[] = [];
    server.use(
      http.get("/api/admin/metrics/history", ({ request }) => {
        const u = new URL(request.url);
        spans.push(Date.parse(u.searchParams.get("to")!) - Date.parse(u.searchParams.get("from")!));
        return HttpResponse.json(historyFor(u));
      }),
    );
    const { user } = renderApp("/admin/monitoring", "admin");
    await screen.findByRole("figure", { name: "GPU, загрузка" });
    expect(screen.getByRole("tab", { name: "Час" })).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("tab", { name: "24 часа" }));
    await waitFor(() => expect(spans).toContain(24 * 3600_000));
    expect(screen.getByRole("tab", { name: "24 часа" })).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("tab", { name: "7 дней" }));
    await waitFor(() => expect(spans).toContain(7 * 24 * 3600_000));
  });

  it("shows another node and another deployment when they are picked", async () => {
    firstNodeHasGpu();
    const names: string[] = [];
    server.use(
      http.get("/api/admin/metrics/history", ({ request }) => {
        const u = new URL(request.url);
        names.push(`${u.searchParams.get("kind")}:${u.searchParams.get("name")}`);
        return HttpResponse.json(historyFor(u));
      }),
    );
    const { user } = renderApp("/admin/monitoring", "admin");
    await screen.findByRole("figure", { name: "GPU, загрузка" });
    await user.selectOptions(screen.getByLabelText("Нода"), "aud-204-gpu01");
    await waitFor(() => expect(names).toContain("node:aud-204-gpu01"));
    await user.selectOptions(screen.getByLabelText("Развёртывание"), "qwen3-30b-a3b");
    await waitFor(() => expect(names).toContain("model:qwen3-30b-a3b"));
  });

  it("never draws the GPU charts of a node that has no GPU, not even while the history loads", async () => {
    server.use(http.get("/api/admin/metrics/history", async ({ request }) => {
      await delay(150);
      return HttpResponse.json(historyFor(new URL(request.url)));
    }));
    renderApp("/admin/monitoring", "admin");
    const cpu = await screen.findByRole("figure", { name: "CPU, загрузка" });
    // the card set is the final one at the first paint: three cards are there, the GPU ones are not, before and after the data
    expect(screen.queryByRole("figure", { name: "GPU, загрузка" })).not.toBeInTheDocument();
    expect(within(cpu).getByText("Загрузка")).toBeInTheDocument();
    await waitFor(() => expect(within(cpu).queryByText("Загрузка")).not.toBeInTheDocument());
    expect(screen.getByRole("figure", { name: "Оперативная память" })).toBeInTheDocument();
    for (const title of ["GPU, загрузка", "VRAM", "Температура GPU"]) {
      expect(screen.queryByRole("figure", { name: title })).not.toBeInTheDocument();
    }
  });

  it("draws the five node cards from the first paint for a node with a GPU and keeps them when the node is switched", async () => {
    server.use(http.get("/api/admin/metrics/history", async ({ request }) => {
      await delay(150);
      return HttpResponse.json(historyFor(new URL(request.url)));
    }));
    const { user } = renderApp("/admin/monitoring", "admin");
    await screen.findByRole("figure", { name: "CPU, загрузка" });
    const section = screen.getByRole("region", { name: "Графики ноды" });
    expect(within(section).getAllByRole("figure")).toHaveLength(2);
    await user.selectOptions(screen.getByLabelText("Нода"), "aud-204-gpu01");
    // at once, while the history of the new node is still on its way
    expect(await within(section).findByRole("figure", { name: "GPU, загрузка" })).toBeInTheDocument();
    expect(within(section).getAllByRole("figure")).toHaveLength(5);
    expect(within(section).getAllByText("Загрузка").length).toBeGreaterThan(0);
    await waitFor(() => expect(within(section).queryByText("Загрузка")).not.toBeInTheDocument());
    expect(within(section).getAllByRole("figure")).toHaveLength(5);
  });

  it("shows nothing of the old period while another one loads", async () => {
    firstNodeHasGpu();
    let slow = false;
    server.use(http.get("/api/admin/metrics/history", async ({ request }) => {
      const u = new URL(request.url);
      if (slow && Date.parse(u.searchParams.get("to")!) - Date.parse(u.searchParams.get("from")!) > 3600_000) await delay(300);
      return HttpResponse.json(historyFor(u));
    }));
    const { user } = renderApp("/admin/monitoring", "admin");
    const fig = await screen.findByRole("figure", { name: "GPU, загрузка" });
    await waitFor(() => expect(fig.querySelector("path.line")?.getAttribute("d")).toMatch(/^M/));
    slow = true;
    await user.click(screen.getByRole("tab", { name: "24 часа" }));
    await waitFor(() => expect(within(fig).getByText("Загрузка")).toBeInTheDocument());
    expect(fig.querySelector("path.line")?.getAttribute("d")).toBe("");
    await waitFor(() => expect(fig.querySelector("path.line")?.getAttribute("d")).toMatch(/^M/));
  });

  it("says so when a period has no samples, and keeps the charts", async () => {
    server.use(
      http.get("/api/admin/metrics/history", ({ request }) => HttpResponse.json({ ...historyFor(new URL(request.url)), points: [] })),
    );
    renderApp("/admin/monitoring", "admin");
    const fig = await screen.findByRole("figure", { name: "CPU, загрузка" });
    expect(await within(fig).findByText("Нет данных за период")).toBeInTheDocument();
  });

  it("tells when there is nothing to show yet", async () => {
    server.use(http.get("/api/admin/metrics/names", () => HttpResponse.json({ node: [], model: [] })));
    renderApp("/admin/monitoring", "admin");
    expect(await screen.findByText(/Замеров нод пока нет/)).toBeInTheDocument();
    expect(screen.getByText(/Замеров развёртываний пока нет/)).toBeInTheDocument();
    expect(screen.queryByRole("figure")).not.toBeInTheDocument();
  });

  it("shows the error of the history", async () => {
    server.use(http.get("/api/admin/metrics/history", () => HttpResponse.json({ detail: "Период не больше года" }, { status: 422 })));
    renderApp("/admin/monitoring", "admin");
    expect((await screen.findAllByText("Период не больше года")).length).toBeGreaterThan(0);
  });

  it("keeps the copy rules: no exclamation marks, no emoji", async () => {
    firstNodeHasGpu();
    renderApp("/admin/monitoring", "admin");
    await screen.findByRole("figure", { name: "GPU, загрузка" });
    await waitFor(() => expect(screen.queryByText("Загрузка")).not.toBeInTheDocument());
    expect(pageText()).not.toContain("!");
    expect(pageText()).not.toMatch(/\p{Extended_Pictographic}/u);
  });
});
