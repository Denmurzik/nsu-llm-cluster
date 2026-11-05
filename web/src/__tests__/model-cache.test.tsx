import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { state } from "../mocks/handlers";
import { server } from "../mocks/server";
import { pageText, renderApp } from "../test/render";

const GiB = 2 ** 30;
const EMOJI = /\p{Extended_Pictographic}|\p{Emoji_Presentation}|️/u;

async function openPage() {
  const view = renderApp("/admin/cache", "admin");
  const table = await screen.findByRole("table", { name: "Файлы в кэше" });
  return { ...view, table };
}

describe("«Кэш моделей»", () => {
  it("is a page of the admin menu and closed to a user", async () => {
    renderApp("/admin/deployments", "admin");
    expect(await screen.findByRole("link", { name: "Кэш моделей" })).toHaveAttribute("href", "/admin/cache");
  });

  it("is refused to a user without admin rights", async () => {
    renderApp("/admin/cache", "user");
    expect(await screen.findByText("Недостаточно прав")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Файлы в кэше" })).not.toBeInTheDocument();
  });

  it("shows how much of the limit and of the disk is used", async () => {
    await openPage();
    const usage = screen.getByRole("region", { name: "Занятое место" });
    expect(usage).toHaveTextContent(/Занято 68,0 ГБ из лимита 150,0 ГБ, 45 %/);
    expect(usage).toHaveTextContent(/Можно скачать ещё 82,0 ГБ/);
    expect(usage).toHaveTextContent(/Свободно на диске 600,0 ГБ из 1024,0 ГБ, зеркало держит в запасе 153,6 ГБ/);
    expect(within(usage).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "45");
  });

  it("lists the files by repository with size, commit, last use and state", async () => {
    const { table } = await openPage();
    expect(within(table).getByText("org/big-GGUF")).toBeInTheDocument();
    expect(within(table).getByText("org/model-GGUF")).toBeInTheDocument();
    const model = within(table).getByRole("row", { name: "org/model-GGUF/model-Q4_K_M.gguf" });
    expect(within(model).getByText("4,0 ГБ")).toBeInTheDocument();
    expect(within(model).getByText("1a2b3c4")).toBeInTheDocument();
    expect(within(model).getByText("В кэше")).toBeInTheDocument();
    expect(within(model).getByText(/\d{2}\.\d{2}\.\d{4}/)).toBeInTheDocument();
    const partial = within(table).getByRole("row", { name: "org/new-GGUF/new-Q8_0.gguf" });
    expect(within(partial).getByText("3,0 ГБ из 8,0 ГБ")).toBeInTheDocument();
    expect(within(partial).getByText("Скачивается, 37 %")).toBeInTheDocument();
  });

  it("marks what is in use and disables its delete button, with the reason in sight", async () => {
    const { table } = await openPage();
    const used = within(table).getByRole("row", { name: "org/model-GGUF/model-Q4_K_M.gguf" });
    expect(within(used).getByText("Используется развёртыванием m1")).toBeInTheDocument();
    expect(within(used).getByRole("button", { name: "Удалить файл model-Q4_K_M.gguf" })).toBeDisabled();
    const busy = within(table).getByRole("row", { name: "org/big-GGUF/Q4_K_M/big-Q4_K_M-00002-of-00002.gguf" });
    expect(within(busy).getByText("Отдаётся нодам сейчас")).toBeInTheDocument();
    expect(within(busy).getByRole("button", { name: /Удалить файл/ })).toBeDisabled();
    const loading = within(table).getByRole("row", { name: "org/new-GGUF/new-Q8_0.gguf" });
    expect(within(loading).getByText("Скачивается сейчас")).toBeInTheDocument();
    expect(within(loading).getByRole("button", { name: /Удалить файл/ })).toBeDisabled();
    // a file nobody uses
    const free = within(table).getByRole("row", { name: "org/model-GGUF/mmproj-F16.gguf" });
    expect(within(free).getByRole("button", { name: "Удалить файл mmproj-F16.gguf" })).toBeEnabled();
    // a repository with a file in use, or being sent, cannot go as a whole
    expect(screen.getByRole("button", { name: "Удалить репозиторий org/model-GGUF из кэша" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Удалить репозиторий org/big-GGUF из кэша" })).toBeDisabled();
    expect(within(table).getByText("Есть файлы развёртывания m1")).toBeInTheDocument();
  });

  it("deletes a file after a confirmation and says how much was freed", async () => {
    const { user, table } = await openPage();
    await user.click(within(table).getByRole("button", { name: "Удалить файл mmproj-F16.gguf" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Удалить mmproj-F16.gguf из кэша зеркала? Освободится 990 МБ.");
    expect(state.cache.files.map((f) => f.path)).toContain("mmproj-F16.gguf"); // nothing happened yet
    await user.click(within(dialog).getByRole("button", { name: "Удалить" }));
    await screen.findByText("Из кэша удалено: 990 МБ");
    expect(state.cache.files.map((f) => f.path)).not.toContain("mmproj-F16.gguf");
    expect(screen.queryByRole("row", { name: "org/model-GGUF/mmproj-F16.gguf" })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Занятое место" })).toHaveTextContent(/Занято 67,0 ГБ/);
  });

  it("does not delete on «Отмена»", async () => {
    const { user, table } = await openPage();
    await user.click(within(table).getByRole("button", { name: "Удалить файл mmproj-F16.gguf" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Отмена" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(state.cache.files.map((f) => f.path)).toContain("mmproj-F16.gguf");
  });

  it("deletes a whole repository that nobody uses", async () => {
    state.cache.files = state.cache.files.filter((f) => f.repo === "org/model-GGUF" && f.path === "mmproj-F16.gguf");
    const { user } = await openPage();
    await user.click(screen.getByRole("button", { name: "Удалить репозиторий org/model-GGUF из кэша" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Удалить из кэша все файлы org/model-GGUF (1)?");
    await user.click(within(dialog).getByRole("button", { name: "Удалить" }));
    await screen.findByText("Из кэша удалено: 990 МБ");
    await screen.findByText(/В кэше пока нет файлов/);
  });

  it("shows the reason when the server refuses a delete", async () => {
    server.use(
      http.delete("/api/admin/cache/*", () =>
        HttpResponse.json({ detail: "Файл сейчас отдаётся нодам или скачивается: попробуйте позже" }, { status: 409 }),
      ),
    );
    const { user, table } = await openPage();
    await user.click(within(table).getByRole("button", { name: "Удалить файл mmproj-F16.gguf" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Удалить" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Файл сейчас отдаётся нодам или скачивается");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("says so when the mirror does not answer", async () => {
    server.use(
      http.get("/api/admin/cache", () =>
        HttpResponse.json({ detail: "Зеркало Hugging Face на главном недоступно или выключено (HF_MIRROR_ENABLED): кэша моделей нет" }, { status: 503 }),
      ),
    );
    renderApp("/admin/cache", "admin");
    expect(await screen.findByRole("alert")).toHaveTextContent("Зеркало Hugging Face на главном недоступно");
    expect(screen.queryByRole("table", { name: "Файлы в кэше" })).not.toBeInTheDocument();
  });

  it("explains an empty cache", async () => {
    state.cache.files = [];
    state.cache.used_bytes = 0;
    state.prefetch = [];
    renderApp("/admin/cache", "admin");
    expect(await screen.findByText(/В кэше пока нет файлов/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Занятое место" })).toHaveTextContent(/Занято 0 Б из лимита 150,0 ГБ, 0 %/);
    expect(screen.queryByRole("region", { name: "Загрузки" })).not.toBeInTheDocument();
  });

  it("appears whole: the usage, the downloads and the table come with the same answer", async () => {
    renderApp("/admin/cache", "admin");
    await screen.findByRole("region", { name: "Занятое место" });
    expect(screen.getByRole("region", { name: "Загрузки" })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Файлы в кэше" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Скачать заранее" })).toBeInTheDocument();
  });

  it("keeps the copy rules: no exclamation marks, no emoji, no trailing period in short strings", async () => {
    await openPage();
    const text = pageText();
    expect(text).not.toContain("!");
    expect(text).not.toMatch(EMOJI);
    const labels = Array.from(document.querySelectorAll("button, label, h1, h2, h3, th, a, legend")).map(
      (el) => (el.textContent ?? "").trim(),
    );
    expect(labels.filter((t) => t.endsWith("."))).toEqual([]);
  });
});

describe("«Скачать заранее»", () => {
  async function find(user: ReturnType<typeof renderApp>["user"], repo = "unsloth/gemma-4-E4B-it-GGUF") {
    await user.type(screen.getByLabelText("Репозиторий Hugging Face"), repo);
    await user.click(screen.getByRole("button", { name: "Найти файлы" }));
    return screen.findByRole("group", { name: "Что скачать" });
  }

  it("finds the files of a repository and queues the chosen ones, with the projector", async () => {
    const { user } = await openPage();
    const files = await find(user);
    expect(within(files).getAllByRole("checkbox")).toHaveLength(4 + 3); // four models, three projectors
    expect(screen.getByRole("button", { name: "Скачать заранее" })).toBeDisabled();
    await user.click(within(files).getByRole("checkbox", { name: /gemma-4-E4B-it-Q4_K_M\.gguf/ }));
    await user.click(within(files).getByRole("checkbox", { name: /mmproj-F16/ }));
    expect(screen.getByText("2 файла, 5,7 ГБ")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Скачать заранее" }));
    expect(await screen.findByText(/Поставлено в очередь: 2 файла/)).toBeInTheDocument();
    expect(state.prefetch.filter((j) => j.repo === "unsloth/gemma-4-E4B-it-GGUF").map((j) => [j.path, j.state])).toEqual([
      ["gemma-4-E4B-it-Q4_K_M.gguf", "queued"],
      ["mmproj-F16.gguf", "queued"],
    ]);
    const jobs = screen.getByRole("region", { name: "Загрузки" });
    expect(within(jobs).getByRole("listitem", { name: "Загрузка gemma-4-E4B-it-Q4_K_M.gguf" })).toHaveTextContent("В очереди");
    expect(screen.getByRole("button", { name: "Скачать заранее" })).toBeDisabled(); // the choice is cleared
  });

  it("sends every part of a multi-file model", async () => {
    server.use(
      http.get("/api/admin/hf/files", () =>
        HttpResponse.json([
          {
            file: "Q4/big-00001-of-00002.gguf",
            size_bytes: 80 * 2 ** 20,
            quant: "Q4_K_M",
            shards: ["Q4/big-00001-of-00002.gguf", "Q4/big-00002-of-00002.gguf"],
          },
        ]),
      ),
    );
    const { user } = await openPage();
    const files = await find(user, "org/big");
    expect(within(files).getByText("2 части")).toBeInTheDocument();
    await user.click(within(files).getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Скачать заранее" }));
    await screen.findByText(/Поставлено в очередь: 2 файла/);
    expect(state.prefetch.filter((j) => j.repo === "org/big").map((j) => j.path)).toEqual([
      "Q4/big-00001-of-00002.gguf",
      "Q4/big-00002-of-00002.gguf",
    ]);
  });

  it("marks a file that is in the cache already", async () => {
    server.use(
      http.get("/api/admin/hf/files", () =>
        HttpResponse.json([{ file: "model-Q4_K_M.gguf", size_bytes: 4 * GiB, quant: "Q4_K_M", shards: [] }]),
      ),
    );
    const { user } = await openPage();
    const files = await find(user, "org/model-GGUF");
    expect(within(files).getByText("уже в кэше")).toBeInTheDocument();
  });

  it("warns when the choice does not fit the free room, and refuses what is bigger than the limit", async () => {
    server.use(
      http.get("/api/admin/hf/files", () =>
        HttpResponse.json([
          { file: "a.gguf", size_bytes: 100 * GiB, quant: "Q8_0", shards: [] },
          { file: "b.gguf", size_bytes: 200 * GiB, quant: "F16", shards: [] },
        ]),
      ),
    );
    const { user } = await openPage();
    const files = await find(user, "org/huge");
    await user.click(within(files).getByRole("checkbox", { name: /Q8_0/ }));
    expect(screen.getByText(/Выбрано 100,0 ГБ, свободно 82,0 ГБ/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Скачать заранее" })).toBeEnabled();
    await user.click(within(files).getByRole("checkbox", { name: /F16/ }));
    expect(screen.getByText(/лимит кэша 150,0 ГБ: такой объём не поместится/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Скачать заранее" })).toBeDisabled();
  });

  it("shows what Hugging Face says when the repository is not found", async () => {
    const { user } = await openPage();
    await user.type(screen.getByLabelText("Репозиторий Hugging Face"), "org/missing");
    await user.click(screen.getByRole("button", { name: "Найти файлы" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Репозиторий или файл не найден");
  });

  it("tells a repository without GGUF files", async () => {
    server.use(http.get("/api/admin/hf/files", () => HttpResponse.json([])));
    const { user } = await openPage();
    await user.type(screen.getByLabelText("Репозиторий Hugging Face"), "org/empty");
    await user.click(screen.getByRole("button", { name: "Найти файлы" }));
    expect(await screen.findByText("В репозитории нет файлов GGUF")).toBeInTheDocument();
  });
});

describe("the downloads", () => {
  it("shows progress, speed and the time left of a running one, and the reason of a failed one", async () => {
    await openPage();
    const jobs = screen.getByRole("region", { name: "Загрузки" });
    const running = within(jobs).getByRole("listitem", { name: "Загрузка new-Q8_0.gguf" });
    expect(running).toHaveTextContent("37 %, 3,0 ГБ из 8,0 ГБ, 50 МБ/с, осталось 2 мин");
    expect(within(running).getByRole("progressbar", { name: "Загрузка new-Q8_0.gguf" })).toHaveAttribute("aria-valuenow", "37");
    expect(within(jobs).getByRole("listitem", { name: "Загрузка mmproj-F16.gguf" })).toHaveTextContent("В очереди");
    expect(within(jobs).getByRole("listitem", { name: "Загрузка model-Q4_K_M.gguf" })).toHaveTextContent("Уже в кэше");
    const failed = within(jobs).getByRole("listitem", { name: "Загрузка gated-Q4_K_M.gguf" });
    expect(failed).toHaveTextContent("Ошибка");
    expect(failed).toHaveTextContent("Invalid credentials in Authorization header");
  });

  it("cancels a running download", async () => {
    const { user } = await openPage();
    await user.click(screen.getByRole("button", { name: "Отменить загрузку new-Q8_0.gguf" }));
    await waitFor(() => expect(state.prefetch.find((j) => j.path === "new-Q8_0.gguf")?.state).toBe("cancelled"));
    const row = await screen.findByRole("listitem", { name: "Загрузка new-Q8_0.gguf" });
    await waitFor(() => expect(row).toHaveTextContent("Отменено"));
    expect(within(row).getByRole("button", { name: "Убрать new-Q8_0.gguf из списка" })).toBeInTheDocument();
  });

  it("removes a finished one and clears all the finished", async () => {
    const { user } = await openPage();
    await user.click(screen.getByRole("button", { name: "Убрать model-Q4_K_M.gguf из списка" }));
    await waitFor(() => expect(screen.queryByRole("listitem", { name: "Загрузка model-Q4_K_M.gguf" })).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Убрать завершённые" }));
    await waitFor(() => expect(screen.queryByRole("listitem", { name: "Загрузка gated-Q4_K_M.gguf" })).not.toBeInTheDocument());
    expect(screen.getAllByRole("listitem", { name: /Загрузка/ })).toHaveLength(2); // the running and the queued stay
    expect(screen.queryByRole("button", { name: "Убрать завершённые" })).not.toBeInTheDocument();
  });

  it("polls faster while something is downloading", async () => {
    const seen: number[] = [];
    const start = Date.now();
    server.use(
      http.get("/api/admin/prefetch", () => {
        seen.push(Date.now() - start);
        return HttpResponse.json({ jobs: state.prefetch, concurrency: 2 });
      }),
    );
    await openPage();
    await waitFor(() => expect(seen.length).toBeGreaterThanOrEqual(3), { timeout: 6000 });
    // a first load, one more when the page switches to the fast interval, then every 2 s: not every 10 s
    expect(seen[2]).toBeLessThan(6000);
  }, 10000);
});

describe("«Скачать заранее» on the deployment page", () => {
  it("queues the weights of the deployment and links to the progress", async () => {
    const { user } = renderApp("/admin/deployments/1", "admin");
    await user.click(await screen.findByRole("button", { name: "Скачать заранее" }));
    expect(await screen.findByText(/Поставлено в очередь: 1 файл/)).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /Ход загрузки в «Кэше моделей»/ })).toHaveAttribute("href", "/admin/cache");
    expect(state.prefetch.map((j) => [j.repo, j.revision, j.path])).toContainEqual([
      "unsloth/gemma-4-E4B-it-GGUF",
      "3f2a9c1d7e5b4a8f6c0d2e1b9a7f5c3d1e0b8a6f",
      "gemma-4-E4B-it-Q4_K_M.gguf",
    ]);
  });

  it("shows why it cannot", async () => {
    server.use(
      http.post("/api/admin/deployments/:id/prefetch", () =>
        HttpResponse.json({ detail: "Зеркало Hugging Face на главном недоступно" }, { status: 503 }),
      ),
    );
    const { user } = renderApp("/admin/deployments/1", "admin");
    await user.click(await screen.findByRole("button", { name: "Скачать заранее" }));
    expect(await screen.findByText("Зеркало Hugging Face на главном недоступно")).toBeInTheDocument();
  });
});

describe("«Скачать заранее» in the deployment list", () => {
  const button = (name: string) => `Скачать заранее файлы развёртывания ${name}`;

  it("queues the weights of a deployment from its row and confirms it without moving the row", async () => {
    const { user } = renderApp("/admin/deployments", "admin");
    const row = await screen.findByRole("row", { name: /gemma-4-e4b/ });
    await user.click(within(row).getByRole("button", { name: button("gemma-4-e4b") }));
    expect(await within(row).findByText("Поставлено в очередь: 1 файл")).toHaveClass("visually-hidden");
    expect(state.prefetch.map((j) => [j.repo, j.revision, j.path])).toContainEqual([
      "unsloth/gemma-4-E4B-it-GGUF",
      "3f2a9c1d7e5b4a8f6c0d2e1b9a7f5c3d1e0b8a6f",
      "gemma-4-E4B-it-Q4_K_M.gguf",
    ]);
    // the check mark replaces the icon, nothing is added to the row that could move it
    expect(within(row).getByRole("button", { name: button("gemma-4-e4b") })).toBeEnabled();
  });

  it("puts a failure on the page's error line", async () => {
    server.use(
      http.post("/api/admin/deployments/:id/prefetch", () =>
        HttpResponse.json({ detail: "Зеркало Hugging Face на главном недоступно" }, { status: 503 }),
      ),
    );
    const { user } = renderApp("/admin/deployments", "admin");
    await user.click(await screen.findByRole("button", { name: button("gemma-4-e4b") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Зеркало Hugging Face на главном недоступно");
  });

  it("is not offered for a deployment of own files", async () => {
    state.deployments.push({ ...state.deployments[0], id: 77, name: "own-model", repo: "local/own", file: "own.gguf" });
    renderApp("/admin/deployments", "admin");
    await screen.findByRole("row", { name: /own-model/ });
    expect(screen.getByRole("button", { name: button("gemma-4-e4b") })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: button("own-model") })).not.toBeInTheDocument();
  });
});
