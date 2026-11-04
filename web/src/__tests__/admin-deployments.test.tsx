import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { fixtures } from "../mocks/handlers";
import { renderApp } from "../test/render";

const REPO = "unsloth/gemma-4-E4B-it-GGUF";

describe("admin deployments list", () => {
  it("shows a status per deployment", async () => {
    renderApp("/admin/deployments", "admin");
    const gemma = await screen.findByRole("row", { name: /gemma-4-e4b/ });
    expect(within(gemma).getByText("Готова")).toBeInTheDocument();
    expect(within(gemma).getByText("gemma-4-E4B-it-Q4_K_M.gguf", { exact: false })).toBeInTheDocument();
    expect(within(screen.getByRole("row", { name: /qwen3-30b-a3b/ })).getByText("Запускается")).toBeInTheDocument();
    expect(within(screen.getByRole("row", { name: /llama-3-1-8b/ })).getByText("Недоступна")).toBeInTheDocument();
  });

  it("marks disabled deployments as off", async () => {
    server.use(
      http.get("/api/admin/deployments", () =>
        HttpResponse.json(fixtures.deployments.map((d) => (d.id === 3 ? { ...d, enabled: false } : d))),
      ),
    );
    renderApp("/admin/deployments", "admin");
    const row = await screen.findByRole("row", { name: /llama-3-1-8b/ });
    expect(within(row).getByText("Выключено")).toBeInTheDocument();
  });

  it("marks experimental deployments and filters the list by kind", async () => {
    server.use(
      http.get("/api/admin/deployments", () =>
        HttpResponse.json(fixtures.deployments.map((d) => (d.id === 2 ? { ...d, experimental: true } : d))),
      ),
    );
    const view = renderApp("/admin/deployments", "admin");
    const qwen = await screen.findByRole("row", { name: /qwen3-30b-a3b/ });
    expect(within(qwen).getByText("экспериментальная")).toBeInTheDocument();
    expect(within(screen.getByRole("row", { name: /gemma-4-e4b/ })).queryByText("экспериментальная")).toBeNull();

    await view.user.click(screen.getByRole("tab", { name: "Экспериментальные" }));
    expect(screen.getByRole("row", { name: /qwen3-30b-a3b/ })).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /gemma-4-e4b/ })).toBeNull();

    await view.user.click(screen.getByRole("tab", { name: "Основные" }));
    expect(screen.getByRole("row", { name: /gemma-4-e4b/ })).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /qwen3-30b-a3b/ })).toBeNull();

    await view.user.click(screen.getByRole("tab", { name: "Все" }));
    expect(screen.getByRole("row", { name: /qwen3-30b-a3b/ })).toBeInTheDocument();
    expect(screen.getByRole("row", { name: /gemma-4-e4b/ })).toBeInTheDocument();
  });

  it("says so when the filter leaves nothing", async () => {
    const view = renderApp("/admin/deployments", "admin");
    await screen.findByRole("row", { name: /gemma-4-e4b/ });
    await view.user.click(screen.getByRole("tab", { name: "Экспериментальные" }));
    expect(screen.getByText("Таких развёртываний нет")).toBeInTheDocument();
  });

  it("links to the wizard and to the edit page", async () => {
    renderApp("/admin/deployments", "admin");
    expect(await screen.findByRole("link", { name: "Создать развёртывание" })).toHaveAttribute(
      "href",
      "/admin/deployments/new",
    );
    expect(await screen.findByRole("link", { name: "gemma-4-e4b" })).toHaveAttribute("href", "/admin/deployments/1");
  });

  it("refuses regular users", async () => {
    renderApp("/admin/deployments", "user");
    expect(await screen.findByText("Недостаточно прав")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Создать развёртывание" })).not.toBeInTheDocument();
  });
});

describe("deployment wizard", () => {
  async function openWizard() {
    const view = renderApp("/admin/deployments/new", "admin");
    await view.user.type(await screen.findByLabelText("Репозиторий Hugging Face"), REPO);
    await view.user.click(screen.getByRole("button", { name: "Найти файлы" }));
    return view;
  }

  it("shows what a GPU node holds in VRAM and in RAM and fills its meter by that", async () => {
    const GB = 10 ** 9;
    server.use(
      http.get("/api/admin/hf/estimate", () =>
        HttpResponse.json({
          ...fixtures.hfEstimate,
          estimate: { ...fixtures.hfEstimate.estimate, device_bytes: 5.7 * GB, host_bytes: 2.3 * GB },
          nodes: [{ name: "gpu-4070", capacity_bytes: 7 * GB, need_bytes: 3.5 * GB, fits: true }],
        }),
      ),
    );
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    const panel = await screen.findByRole("region", { name: "Оценка памяти" });
    expect(await within(panel).findByText("На GPU-ноде: видеопамять, RAM (входные эмбеддинги)")).toBeInTheDocument();
    expect(within(panel).getByText(/^[0-9,]+\sГБ, [0-9,]+\sГБ$/)).toBeInTheDocument();
    const meter = within(panel).getByText("gpu-4070").closest("tr")!.querySelector(".meter i") as HTMLElement;
    expect(meter.style.width).toBe("50%");
  });

  it("lists GGUF files with quant and size", async () => {
    await openWizard();
    const files = await screen.findByRole("radiogroup", { name: "Файл модели" });
    const q4 = within(files).getByRole("radio", { name: /Q4_K_M/ });
    expect(q4).toHaveAccessibleName(/4,7\sГБ/);
    expect(within(files).getByRole("radio", { name: /Q8_0/ })).toHaveAccessibleName(/7,6\sГБ/);
    expect(within(files).getAllByRole("radio")).toHaveLength(4);
  });

  it("shows the Hugging Face error", async () => {
    const { user } = renderApp("/admin/deployments/new", "admin");
    await user.type(await screen.findByLabelText("Репозиторий Hugging Face"), "user/hf-down");
    await user.click(screen.getByRole("button", { name: "Найти файлы" }));
    expect(await screen.findByText("Hugging Face недоступен")).toBeInTheDocument();
  });

  it("shows the not found error", async () => {
    const { user } = renderApp("/admin/deployments/new", "admin");
    await user.type(await screen.findByLabelText("Репозиторий Hugging Face"), "user/missing");
    await user.click(screen.getByRole("button", { name: "Найти файлы" }));
    expect(await screen.findByText("Репозиторий или файл не найден")).toBeInTheDocument();
  });

  it("estimates memory per node and recommends a group size", async () => {
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));

    const panel = await screen.findByRole("region", { name: "Оценка памяти" });
    const pc01 = await within(panel).findByRole("row", { name: /aud-101-pc01/ });
    expect(within(pc01).getByText("Влезает")).toBeInTheDocument();
    expect(within(within(panel).getByRole("row", { name: /aud-101-pc02/ })).getByText("Не влезает")).toBeInTheDocument();
    expect(within(within(panel).getByRole("row", { name: /aud-204-gpu01/ })).getByText("Влезает")).toBeInTheDocument();
    expect(within(panel).getByText("Рекомендуемый размер группы: 1")).toBeInTheDocument();
    expect(within(panel).getByText("4,7 ГБ")).toBeInTheDocument();
  });

  it("updates the estimate when context and parallelism change (debounced)", async () => {
    const calls: URLSearchParams[] = [];
    server.use(
      http.get("/api/admin/hf/estimate", ({ request }) => {
        const params = new URL(request.url).searchParams;
        calls.push(params);
        return HttpResponse.json({
          ...fixtures.hfEstimate,
          estimate: {
            ...fixtures.hfEstimate.estimate,
            kv_bytes: Number(params.get("ctx_per_request")) * Number(params.get("parallel")) * 16384,
          },
        });
      }),
    );
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("region", { name: "Оценка памяти" });
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    const before = calls.length;
    expect(calls[before - 1].get("repo")).toBe(REPO);
    expect(calls[before - 1].get("file")).toBe("gemma-4-E4B-it-Q4_K_M.gguf");
    expect(calls[before - 1].get("engine")).toBe("llamacpp");
    expect(calls[before - 1].get("ctx_per_request")).toBe("8192");
    expect(calls[before - 1].get("parallel")).toBe("2");

    const ctx = screen.getByLabelText("Контекст на запрос");
    await user.clear(ctx);
    await user.type(ctx, "32768");
    const parallel = screen.getByLabelText("Параллельность");
    await user.clear(parallel);
    await user.type(parallel, "4");

    await waitFor(() => {
      const last = calls[calls.length - 1];
      expect(last.get("ctx_per_request")).toBe("32768");
      expect(last.get("parallel")).toBe("4");
    });
    // five keystrokes in two fields must not produce one request each
    expect(calls.length - before).toBeLessThan(5);
    expect(await within(screen.getByRole("region", { name: "Оценка памяти" })).findByText("2,0 ГБ")).toBeInTheDocument();
  });

  it("disables submit when context exceeds the model maximum and says why", async () => {
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("region", { name: "Оценка памяти" });
    const ctx = screen.getByLabelText("Контекст на запрос");
    await user.clear(ctx);
    await user.type(ctx, "200000");
    expect(await screen.findByText(/Контекст на запрос больше максимума модели: 131\s072/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeDisabled();

    await user.clear(ctx);
    await user.type(ctx, "8192");
    await waitFor(() => expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeEnabled());
  });

  it("requires a group of at least two for split and allows any accelerator", async () => {
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("region", { name: "Оценка памяти" });
    await user.click(screen.getByRole("radio", { name: "Разделение по нодам" }));
    expect(screen.getByLabelText("Размер группы")).toHaveValue(2);
    expect(screen.getByLabelText("Ускоритель")).toHaveValue("any");
    // GPU and CPU nodes in one group: the RAM fields apply (to the GPU nodes and to the CPU nodes)
    expect(screen.getByLabelText("Использовать RAM нод, когда не хватает видеопамяти")).toBeChecked();
    expect(screen.getByLabelText("Доля RAM ноды, %")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeEnabled());
    await user.clear(screen.getByLabelText("Размер группы"));
    await user.type(screen.getByLabelText("Размер группы"), "1");
    expect(await screen.findByText("Размер группы не меньше 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeDisabled();
  });

  it("posts the exact body", async () => {
    let posted: { csrf: string | null; body: unknown } | null = null;
    server.use(
      http.post("/api/admin/deployments", async ({ request }) => {
        posted = { csrf: request.headers.get("X-CSRF-Token"), body: await request.json() };
        return HttpResponse.json({ ...fixtures.deploymentDetail, id: 7, name: "gemma-4-e4b-it" }, { status: 201 });
      }),
    );
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("region", { name: "Оценка памяти" });

    expect(screen.getByLabelText("Имя развёртывания")).toHaveValue("gemma-4-e4b-it");
    await user.click(screen.getByRole("checkbox", { name: "aud-101-pc01" }));
    await user.click(screen.getByRole("checkbox", { name: "aud-204-gpu01" }));
    await user.click(screen.getByRole("checkbox", { name: "Назначать свободные ноды автоматически" }));
    const submit = screen.getByRole("button", { name: "Создать развёртывание" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);
    // both nodes belong to gemma-4-e4b: the admin confirms taking them over
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Забрать ноды и создать" }));

    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toEqual({
      csrf: "csrf-test-token-admin",
      body: {
        name: "gemma-4-e4b-it",
        engine: "llamacpp",
        repo: REPO,
        file: "gemma-4-E4B-it-Q4_K_M.gguf",
        mode: "replicas",
        group_size: 2,
        accel: "any",
        ctx_per_request: 8192,
        parallel: 2,
        auto_assign: true,
        enabled: true,
        // the llama.cpp settings: a new deployment gets the q8_0 cache, the repository has a projector (F16 is offered)
        reasoning: "off",
        kv_cache_type: "q8_0",
        sampling: null,
        draft_repo: null,
        draft_file: null,
        mmproj_file: "mmproj-F16.gguf",
        extra_args: null,
        system_prompt: null,
        experimental: false,
        nodes: ["aud-101-pc01", "aud-204-gpu01"],
      },
    });
    expect(await screen.findByRole("heading", { name: "Развёртывания" })).toBeInTheDocument();
  });

  it("shows the server error on a duplicate name", async () => {
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("region", { name: "Оценка памяти" });
    const name = screen.getByLabelText("Имя развёртывания");
    await user.clear(name);
    await user.type(name, "gemma-4-e4b");
    const submit = screen.getByRole("button", { name: "Создать развёртывание" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);
    expect(await screen.findByText("Развёртывание с таким именем уже есть")).toBeInTheDocument();
  });

  it("rejects an invalid name", async () => {
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("region", { name: "Оценка памяти" });
    const name = screen.getByLabelText("Имя развёртывания");
    await user.clear(name);
    await user.type(name, "Bad Name");
    expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeDisabled();
    expect(screen.getByText(/Имя: строчные латинские буквы, цифры и дефис/)).toBeInTheDocument();
  });

  it("marks nodes held by another deployment and asks before taking them", async () => {
    let posts = 0;
    server.use(
      http.post("/api/admin/deployments", async () => {
        posts += 1;
        return HttpResponse.json({ ...fixtures.deploymentDetail, id: 8 }, { status: 201 });
      }),
    );
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("region", { name: "Оценка памяти" });
    const group = screen.getByRole("group", { name: "Ноды" });
    expect(within(group).getAllByText("занята: gemma-4-e4b")).toHaveLength(3);
    expect(within(within(group).getByText("win-laptop-07").closest("label")!).queryByText(/занята/)).toBeNull();

    await user.click(screen.getByRole("checkbox", { name: "aud-101-pc01" }));
    const submit = screen.getByRole("button", { name: "Создать развёртывание" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("aud-101-pc01");
    expect(dialog).toHaveTextContent("gemma-4-e4b");
    expect(posts).toBe(0);
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(posts).toBe(0);

    await user.click(submit);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Забрать ноды и создать" }));
    await waitFor(() => expect(posts).toBe(1));
  });

  it("does not ask when the chosen node is free", async () => {
    let posted: { nodes?: string[] } | null = null;
    server.use(
      http.post("/api/admin/deployments", async ({ request }) => {
        posted = (await request.json()) as { nodes?: string[] };
        return HttpResponse.json({ ...fixtures.deploymentDetail, id: 9 }, { status: 201 });
      }),
    );
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("region", { name: "Оценка памяти" });
    await user.click(screen.getByRole("checkbox", { name: "win-laptop-07" }));
    const submit = screen.getByRole("button", { name: "Создать развёртывание" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted!.nodes).toEqual(["win-laptop-07"]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("drops chosen nodes that no longer match the accelerator", async () => {
    let posted: { nodes?: string[]; accel?: string } | null = null;
    server.use(
      http.post("/api/admin/deployments", async ({ request }) => {
        posted = (await request.json()) as { nodes?: string[]; accel?: string };
        return HttpResponse.json({ ...fixtures.deploymentDetail, id: 10 }, { status: 201 });
      }),
    );
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("region", { name: "Оценка памяти" });
    await user.click(screen.getByRole("checkbox", { name: "win-laptop-07" }));
    expect(screen.getByRole("checkbox", { name: "win-laptop-07" })).toBeChecked();

    await user.selectOptions(screen.getByLabelText("Ускоритель"), "nvidia");
    expect(screen.queryByRole("checkbox", { name: "win-laptop-07" })).not.toBeInTheDocument();
    // back to any: the cpu node is offered again but is not silently still selected
    await user.selectOptions(screen.getByLabelText("Ускоритель"), "any");
    expect(screen.getByRole("checkbox", { name: "win-laptop-07" })).not.toBeChecked();

    await user.selectOptions(screen.getByLabelText("Ускоритель"), "nvidia");
    await user.click(screen.getByRole("checkbox", { name: "aud-204-gpu01" }));
    const submit = screen.getByRole("button", { name: "Создать развёртывание" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Забрать ноды и создать" }));
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({ accel: "nvidia", nodes: ["aud-204-gpu01"] });
  });

  it("says why the create button is disabled", async () => {
    const { user } = await openWizard();
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("region", { name: "Оценка памяти" });
    const name = screen.getByLabelText("Имя развёртывания");
    await waitFor(() => expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeEnabled());

    await user.clear(name);
    expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeDisabled();
    expect(screen.getByText("Укажите имя развёртывания")).toBeInTheDocument();

    await user.type(name, "Bad Name");
    expect(screen.getByText("Исправьте имя развёртывания")).toBeInTheDocument();
    await user.clear(name);
    await user.type(name, "ok-name");
    expect(screen.queryByText("Исправьте имя развёртывания")).not.toBeInTheDocument();

    const ctx = screen.getByLabelText("Контекст на запрос");
    await user.clear(ctx);
    await user.type(ctx, "10");
    expect(screen.getByText("Исправьте выделенные поля")).toBeInTheDocument();
  });

  it("does not offer vLLM for a new deployment", async () => {
    renderApp("/admin/deployments/new", "admin");
    await screen.findByLabelText("Репозиторий Hugging Face");
    expect(screen.queryByLabelText("Движок")).not.toBeInTheDocument();
    expect(screen.queryByText(/vLLM/)).not.toBeInTheDocument();
  });
});

describe("deployment edit page", () => {
  it("edits the schedule, spare nodes and regroup pause through PATCH without a restart", async () => {
    let body: unknown = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ ...fixtures.deploymentDetail, ...fixtures.deployments[1] });
      }),
    );
    const { user } = renderApp("/admin/deployments/2", "admin");
    const spare = await screen.findByLabelText("Резервные ноды");
    expect(spare).toHaveValue(1);
    expect(screen.getByRole("checkbox", { name: "Работать по расписанию" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Пт" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Сб" })).not.toBeChecked();
    expect(
      screen.getByText("Сейчас вне окна расписания: развёртывание остановлено, ноды свободны для других развёртываний"),
    ).toBeInTheDocument();
    const grace = screen.getByLabelText("Пауза перед пересборкой, с");
    expect(grace).toHaveValue(null);
    expect(grace).toHaveAttribute("placeholder", "600");

    await user.click(screen.getByRole("checkbox", { name: "Сб" }));
    fireEvent.change(screen.getByLabelText("Конец окна"), { target: { value: "22:00" } });
    await user.clear(spare);
    await user.type(spare, "2");
    await user.type(grace, "300");
    expect(screen.queryByText("Поды перезапустятся по одной ноде")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() =>
      expect(body).toEqual({
        spare_nodes: 2,
        regroup_grace_s: 300,
        schedule: { days: [1, 2, 3, 4, 5, 6], start: "08:00", end: "22:00" },
      }),
    );
  });

  it("shows a window that runs to the end of the day and keeps it as 24:00", async () => {
    let body: unknown = null;
    server.use(
      http.get("/api/admin/deployments/:id", () =>
        HttpResponse.json({
          ...fixtures.deploymentDetail,
          ...fixtures.deployments[1],
          schedule: { days: [6, 7], start: "10:00", end: "24:00" },
        }),
      ),
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(fixtures.deploymentDetail);
      }),
    );
    const { user } = renderApp("/admin/deployments/2", "admin");
    const toEnd = await screen.findByRole("checkbox", { name: "До конца дня" });
    expect(toEnd).toBeChecked();
    expect(screen.getByLabelText("Конец окна")).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "Пт" }));
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(body).toEqual({ schedule: { days: [5, 6, 7], start: "10:00", end: "24:00" } }));

    // the saved deployment is loaded again (the window still ends at 24:00)
    await user.click(await screen.findByRole("checkbox", { name: "До конца дня" }));
    const end = screen.getByLabelText("Конец окна");
    expect(end).toBeEnabled();
    fireEvent.change(end, { target: { value: "18:00" } });
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(body).toEqual({ schedule: { days: [6, 7], start: "10:00", end: "18:00" } }));
  });

  it("sets the end of the window to the end of the day", async () => {
    let body: unknown = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(fixtures.deploymentDetail);
      }),
    );
    const { user } = renderApp("/admin/deployments/2", "admin");
    const toEnd = await screen.findByRole("checkbox", { name: "До конца дня" });
    expect(toEnd).not.toBeChecked();
    await user.click(toEnd);
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(body).toEqual({ schedule: { days: [1, 2, 3, 4, 5], start: "08:00", end: "24:00" } }));
  });

  it("explains the spare nodes and the drain at the end of the window", async () => {
    renderApp("/admin/deployments/2", "admin");
    expect(await screen.findByText(/свободная резервная нода заменяет пропавшую сразу, без паузы/)).toBeInTheDocument();
    expect(screen.getByText(/новые запросы перестают поступать, начатые ответы дописываются ещё 60 с/)).toBeInTheDocument();
  });

  it("shows the configured drain, not a fixed one", async () => {
    server.use(
      http.get("/api/admin/deployments/:id", () =>
        HttpResponse.json({ ...fixtures.deploymentDetail, ...fixtures.deployments[1], schedule_drain_s: 45 }),
      ),
    );
    renderApp("/admin/deployments/2", "admin");
    expect(await screen.findByText(/начатые ответы дописываются ещё 45 с/)).toBeInTheDocument();
  });

  it("turns the schedule off with null", async () => {
    let body: unknown = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(fixtures.deploymentDetail);
      }),
    );
    const { user } = renderApp("/admin/deployments/2", "admin");
    await user.click(await screen.findByRole("checkbox", { name: "Работать по расписанию" }));
    expect(screen.queryByRole("checkbox", { name: "Пн" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(body).toEqual({ schedule: null }));
  });

  it("checks the schedule window before saving", async () => {
    const { user } = renderApp("/admin/deployments/1", "admin");
    await user.click(await screen.findByRole("checkbox", { name: "Работать по расписанию" }));
    for (const day of ["Пн", "Вт", "Ср", "Чт", "Пт"]) await user.click(screen.getByRole("checkbox", { name: day }));
    expect(screen.getByText("Выберите хотя бы один день расписания")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Сохранить" })).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "Сб" }));
    fireEvent.change(screen.getByLabelText("Конец окна"), { target: { value: "08:00" } });
    expect(screen.getByText("Начало и конец окна расписания совпадают")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Сохранить" })).toBeDisabled();
  });

  it("shows the pinned commit and updates it to the latest version", async () => {
    let csrf: string | null = null;
    server.use(
      http.post("/api/admin/deployments/:id/refresh-revision", ({ request }) => {
        csrf = request.headers.get("X-CSRF-Token");
        const deployment = { ...fixtures.deploymentDetail, revision: "77aa11bb22cc33dd44ee55ff6600778899aabbcc" };
        return HttpResponse.json({ changed: true, deployment });
      }),
    );
    const { user } = renderApp("/admin/deployments/1", "admin");
    expect(await screen.findByText("3f2a9c1")).toHaveAttribute("title", "3f2a9c1d7e5b4a8f6c0d2e1b9a7f5c3d1e0b8a6f");
    await user.click(screen.getByRole("button", { name: "Обновить до последней версии" }));
    expect(
      await screen.findByText("Зафиксирована версия 77aa11b: ноды перезагрузят модель по одной"),
    ).toBeInTheDocument();
    expect(csrf).toBeTruthy();
  });

  it("says when the deployment still follows main and when the commit is the latest", async () => {
    server.use(
      http.get("/api/admin/deployments/:id", () => HttpResponse.json({ ...fixtures.deploymentDetail, revision: null })),
    );
    const { user } = renderApp("/admin/deployments/1", "admin");
    expect(await screen.findByText("main, не зафиксирована")).toBeInTheDocument();
    server.use(
      http.post("/api/admin/deployments/:id/refresh-revision", () =>
        HttpResponse.json({ detail: "Hugging Face недоступен" }, { status: 502 }),
      ),
    );
    await user.click(screen.getByRole("button", { name: "Обновить до последней версии" }));
    expect(await screen.findByText("Hugging Face недоступен")).toBeInTheDocument();
  });

  it("offers spare nodes only in split mode and shows the replicas pause default", async () => {
    renderApp("/admin/deployments/1", "admin");
    const grace = await screen.findByLabelText("Пауза перед пересборкой, с");
    expect(grace).toHaveAttribute("placeholder", "120");
    expect(screen.queryByLabelText("Резервные ноды")).not.toBeInTheDocument();
  });

  it("shows the restart warning and PATCHes only what changed", async () => {
    let patched: { csrf: string | null; body: unknown } | null = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        patched = { csrf: request.headers.get("X-CSRF-Token"), body: await request.json() };
        return HttpResponse.json(fixtures.deploymentDetail);
      }),
    );
    const { user } = renderApp("/admin/deployments/1", "admin");
    expect(await screen.findByRole("heading", { name: "gemma-4-e4b" })).toBeInTheDocument();
    expect(screen.queryByText("Поды перезапустятся по одной ноде")).not.toBeInTheDocument();

    const ctx = await screen.findByLabelText("Контекст на запрос");
    expect(ctx).toHaveValue(8192);
    await user.clear(ctx);
    await user.type(ctx, "4096");
    const parallel = screen.getByLabelText("Параллельность");
    await user.clear(parallel);
    await user.type(parallel, "3");
    await user.click(screen.getByRole("checkbox", { name: "aud-101-pc02" }));

    expect(screen.getByText("Поды перезапустятся по одной ноде")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(patched).not.toBeNull());
    expect(patched).toEqual({
      csrf: "csrf-test-token-admin",
      body: { ctx_per_request: 4096, parallel: 3, nodes: ["aud-101-pc01", "aud-204-gpu01"] },
    });
  });

  it("blocks a context above the model maximum", async () => {
    const { user } = renderApp("/admin/deployments/1", "admin");
    const ctx = await screen.findByLabelText("Контекст на запрос");
    await user.clear(ctx);
    await user.type(ctx, "999999");
    expect(await screen.findByText(/Контекст на запрос больше максимума модели: 131\s072/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Сохранить" })).toBeDisabled();
  });

  it("notes missing model metadata", async () => {
    server.use(
      http.get("/api/admin/deployments/:id", () =>
        HttpResponse.json({ ...fixtures.deploymentDetail, meta: null, meta_missing: true }),
      ),
    );
    renderApp("/admin/deployments/1", "admin");
    expect(
      await screen.findByText("Метаданные модели ещё не загружены, максимум контекста не проверяется"),
    ).toBeInTheDocument();
  });

  it("renders the status table with pods and opens logs", async () => {
    const { user } = renderApp("/admin/deployments/1", "admin");
    const table = await screen.findByRole("table", { name: "Поды развёртывания" });
    const row = await within(table).findByRole("row", { name: /llm-gemma-4-e4b-cpu-m4d9q/ });
    expect(within(row).getByText("aud-101-pc02")).toBeInTheDocument();
    expect(within(row).getByText("Работает")).toBeInTheDocument();
    expect(within(row).getByText("2")).toBeInTheDocument();
    expect(within(row).getByText("Загрузка модели")).toBeInTheDocument();
    expect(within(row).getByText("llama_model_load: loading tensors 212/340")).toBeInTheDocument();
    expect(within(within(table).getByRole("row", { name: /llm-gemma-4-e4b-cpu-x7k2p/ })).getByText("Готов")).toBeInTheDocument();
    expect(within(row).getByText("Скачивание 42,5 %")).toBeInTheDocument();
    expect(within(row).getByText("2,0 ГБ из 4,7 ГБ")).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Логи пода llm-gemma-4-e4b-cpu-m4d9q" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/loading tensors 340\/340/)).toBeInTheDocument();
  });

  it("shows the load stages of pods that are not ready", async () => {
    const pod = (name: string, progress: unknown) => ({
      name, role: "inference", phase: "Running", ready: false, restarts: 0, message: "", last_log: null, progress,
    });
    server.use(
      http.get("/api/admin/deployments/:id/status", () =>
        HttpResponse.json({
          ...fixtures.deploymentStatus,
          nodes: [
            {
              name: "pc01", source: "k8s", applied: false,
              pods: [
                pod("p-load", { stage: "loading", percent: null, done_bytes: null, total_bytes: null }),
                pod("p-start", { stage: "starting", percent: null, done_bytes: null, total_bytes: null }),
                pod("p-dl", { stage: "downloading", percent: null, done_bytes: null, total_bytes: null }),
                pod("p-none", null),
              ],
            },
          ],
        }),
      ),
    );
    renderApp("/admin/deployments/1", "admin");
    const table = await screen.findByRole("table", { name: "Поды развёртывания" });
    const cell = async (name: string, text: string) =>
      expect(within(await within(table).findByRole("row", { name: new RegExp(name) })).getByText(text)).toBeInTheDocument();
    await cell("p-load", "Загрузка весов");
    await cell("p-start", "Запуск");
    await cell("p-dl", "Скачивание");
    await cell("p-none", "Не готов");
  });

  it("deletes a deployment after confirmation", async () => {
    let deleted = false;
    server.use(
      http.delete("/api/admin/deployments/:id", () => {
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { user } = renderApp("/admin/deployments/1", "admin");
    await user.click(await screen.findByRole("button", { name: "Удалить развёртывание" }));
    const dialog = await screen.findByRole("dialog");
    expect(deleted).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Удалить" }));
    await waitFor(() => expect(deleted).toBe(true));
    expect(await screen.findByRole("heading", { name: "Развёртывания" })).toBeInTheDocument();
  });

  it("toggles enabled through PATCH", async () => {
    let body: unknown = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ ...fixtures.deploymentDetail, enabled: false });
      }),
    );
    const { user } = renderApp("/admin/deployments/1", "admin");
    await user.click(await screen.findByRole("checkbox", { name: "Развёртывание включено" }));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(body).toEqual({ enabled: false }));
  });

  it("changes the accelerator through PATCH and warns about the restart", async () => {
    let body: unknown = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ ...fixtures.deploymentDetail, accel: "cpu" });
      }),
    );
    const { user } = renderApp("/admin/deployments/1", "admin");
    const accel = await screen.findByLabelText("Ускоритель");
    expect(accel).toHaveValue("any");
    expect(accel).toBeEnabled();
    expect(screen.queryByText("Поды перезапустятся по одной ноде")).not.toBeInTheDocument();
    await user.selectOptions(accel, "cpu");
    expect(screen.getByText("Поды перезапустятся по одной ноде")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(body).toEqual({ accel: "cpu" }));
  });

  it("switches an any deployment to split and keeps the accelerator any", async () => {
    let body: unknown = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(fixtures.deploymentDetail);
      }),
    );
    const { user } = renderApp("/admin/deployments/1", "admin");
    await user.click(await screen.findByRole("radio", { name: "Разделение по нодам" }));
    expect(screen.getByLabelText("Ускоритель")).toHaveValue("any");
    expect(screen.getByLabelText("Доля RAM ноды, %")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Сохранить" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(body).toEqual({ mode: "split", group_size: 2 }));
  });

  it("keeps the accelerator fixed for vLLM", async () => {
    renderApp("/admin/deployments/3", "admin");
    const accel = await screen.findByLabelText("Ускоритель");
    expect(accel).toHaveValue("nvidia");
    expect(accel).toBeDisabled();
  });

  it("shows nodes held by another deployment and confirms before taking them", async () => {
    const own = { ...fixtures.deploymentDetail, id: 2, name: "qwen3-30b-a3b", assigned_nodes: [] as string[] };
    let body: unknown = null;
    server.use(
      http.get("/api/admin/deployments/:id", () => HttpResponse.json(own)),
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(own);
      }),
    );
    const { user } = renderApp("/admin/deployments/2", "admin");
    await screen.findByRole("heading", { name: "qwen3-30b-a3b" });
    const group = await screen.findByRole("group", { name: "Ноды" });
    expect(within(group).getAllByText("занята: gemma-4-e4b")).toHaveLength(3);

    await user.click(screen.getByRole("checkbox", { name: "aud-101-pc01" }));
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("aud-101-pc01");
    expect(dialog).toHaveTextContent("gemma-4-e4b");
    expect(body).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Забрать ноды и сохранить" }));
    await waitFor(() => expect(body).toEqual({ nodes: ["aud-101-pc01"] }));
  });

  it("does not ask about nodes the deployment already owns", async () => {
    let body: unknown = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(fixtures.deploymentDetail);
      }),
    );
    const { user } = renderApp("/admin/deployments/1", "admin");
    const group = await screen.findByRole("group", { name: "Ноды" });
    expect(within(group).queryByText(/занята/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "aud-101-pc02" }));
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(body).toEqual({ nodes: ["aud-101-pc01", "aud-204-gpu01"] }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
