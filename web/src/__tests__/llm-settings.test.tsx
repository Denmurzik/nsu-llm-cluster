import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import {
  checkLlm,
  DEFAULT_LLM,
  llmBody,
  llmChanges,
  llmFromDeployment,
  pickMmproj,
  validateExtraArgs,
  type LlmSettings,
} from "../lib/llm";
import { fixtures } from "../mocks/handlers";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";
import type { Deployment } from "../types";

const REPO = "unsloth/gemma-4-E4B-it-GGUF";
const dep = fixtures.deploymentDetail as unknown as Deployment;

describe("own flags are checked as the portal does", () => {
  it.each([
    ["--no-warmup --seed 42", null],
    ['--override-kv "a.b=bool:false"', null],
    ["", null],
    ["--port 80", "Флаг --port задаётся порталом"],
    ["--api-key=x", "Флаг --api-key задаётся порталом"],
    ["-ngl 0", "Флаг -ngl задаётся порталом"],
    ["--cache-type-k q4_0", "Флаг --cache-type-k задаётся порталом"],
    ["--no-mmproj", "Флаг --no-mmproj задаётся порталом"],
    ["--my-opt /etc/passwd", "Пути к файлам в своих флагах запрещены: /etc/passwd"],
    ["--my-opt=~/x", "Пути к файлам в своих флагах запрещены: --my-opt=~/x"],
    ["--seed C:\\x", "Пути к файлам в своих флагах запрещены: обратная косая черта не допускается"],
    ["42", "Свои флаги начинаются с «-»: 42"],
    ["--API_KEY=x", "Флаг --API_KEY задаётся порталом"],
    ["--n_gpu_layers 1", "Флаг --n_gpu_layers задаётся порталом"],
    ["--Port=1", "Флаг --Port задаётся порталом"],
    ["-dev CPU", "Флаг -dev задаётся порталом"],
    ["--my-model-file x", "Флаги с файлами, путями и ключами задаются порталом: --my-model-file"],
    ["--some_dir", "Флаги с файлами, путями и ключами задаются порталом: --some_dir"],
    ["--seed ..", "Пути к файлам в своих флагах запрещены: .."],
    ["--Seed 5 --no_warmup", null],
    ['--chat-template ""', "Пустое значение флага: уберите пустые кавычки"],
    ["--note ''", "Пустое значение флага: уберите пустые кавычки"],
    ["-dr ai/gemma", "Флаг -dr задаётся порталом"],
    ['--note "abc', "В своих флагах не закрыта кавычка"],
    ["--note привет", "В своих флагах допустимы только печатные символы ASCII: 'привет'"],
  ])("%s", (text, expected) => {
    expect(validateExtraArgs(text)).toBe(expected);
  });

  it("allows at most 40 parts", () => {
    expect(validateExtraArgs(Array.from({ length: 40 }, (_, i) => `--x${i}`).join(" "))).toBeNull();
    expect(validateExtraArgs(Array.from({ length: 41 }, (_, i) => `--x${i}`).join(" "))).toBe("В своих флагах не больше 40 частей");
  });
});

describe("settings of the form", () => {
  it("checks the sampling ranges, the draft and projector choices and the prompt size", () => {
    const base: LlmSettings = { ...DEFAULT_LLM, sampling: { ...DEFAULT_LLM.sampling } };
    expect(checkLlm(base)).toEqual({});
    const bad = checkLlm({
      ...base,
      sampling: { temperature: "3", top_p: "0.9", top_k: "1.5", min_p: "x", repeat_penalty: "0.4" },
      draftOn: true,
      mmprojOn: true,
      systemPrompt: "a".repeat(8001),
    });
    expect(bad.sampling).toEqual({
      temperature: "от 0 до 2",
      top_k: "нужно целое число",
      min_p: "нужно число",
      repeat_penalty: "от 0.5 до 2",
    });
    expect(bad.draft).toBeTruthy();
    expect(bad.mmproj).toBeTruthy();
    expect(bad.systemPrompt).toBe("Не больше 8000 символов");
  });

  it("offers F16 before BF16 before the other projectors", () => {
    const f = (file: string, quant: string | null) => ({ file, size_bytes: 1, quant });
    expect(pickMmproj([f("mmproj-F32.gguf", "F32"), f("mmproj-BF16.gguf", "BF16"), f("mmproj-F16.gguf", "F16")])).toBe("mmproj-F16.gguf");
    expect(pickMmproj([f("mmproj-F32.gguf", "F32"), f("mmproj-BF16.gguf", "BF16")])).toBe("mmproj-BF16.gguf");
    expect(pickMmproj([f("b.gguf", null), f("a.gguf", null)])).toBe("a.gguf");
    expect(pickMmproj([])).toBeNull();
  });

  it("sends only what changed, and a draft as one pair", () => {
    const saved = { ...dep, kv_cache_type: "f16" as const, draft_repo: null, draft_file: null };
    const form = llmFromDeployment(saved);
    expect(llmChanges(form, saved)).toEqual({});
    expect(llmChanges({ ...form, kv: "q4_0", systemPrompt: "  hi " }, saved)).toEqual({ kv_cache_type: "q4_0", system_prompt: "  hi " });
    expect(llmChanges({ ...form, draftOn: true, draftRepo: "org/d", draftFile: "d.gguf" }, saved)).toEqual({
      draft_repo: "org/d",
      draft_file: "d.gguf",
    });
    const withDraft = { ...saved, draft_repo: "org/d", draft_file: "d.gguf" };
    expect(llmChanges({ ...llmFromDeployment(withDraft), draftOn: false }, withDraft)).toEqual({ draft_repo: null, draft_file: null });
    expect(llmBody({ ...form, sampling: { ...form.sampling, temperature: "0.7" } }).sampling).toEqual({ temperature: 0.7 });
  });
});

async function openWizard() {
  const view = renderApp("/admin/deployments/new", "admin");
  await view.user.type(await screen.findByLabelText("Репозиторий Hugging Face"), REPO);
  await view.user.click(screen.getByRole("button", { name: "Найти файлы" }));
  await view.user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
  await screen.findByRole("region", { name: "Оценка памяти" });
  return view;
}

describe("wizard: «Генерация и модель»", () => {
  it("has the section with the defaults of a new deployment", async () => {
    await openWizard();
    expect(screen.getByRole("heading", { name: "Генерация и модель" })).toBeInTheDocument();
    expect(screen.getByLabelText("Рассуждение")).toHaveValue("off");
    expect(within(screen.getByLabelText("Рассуждение")).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Выключено",
      "Включено",
      "Авто",
    ]);
    expect(screen.getByLabelText("KV-кэш")).toHaveValue("q8_0");
    expect(screen.getByText(/q8_0 вдвое уменьшает память под контекст почти без потери качества/)).toBeInTheDocument();
    expect(screen.getByText("Параметры генерации по умолчанию")).toBeInTheDocument();
    expect(screen.getByText(/клиент может передать свои/)).toBeInTheDocument();
    expect(screen.getByLabelText("Draft-модель")).not.toBeChecked();
    expect(screen.getByLabelText("Экспериментальная (видна только администраторам)")).not.toBeChecked();
    expect(screen.getByLabelText("Свои флаги llama-server")).toHaveValue("");
    expect(screen.getByText(/Добавляется, только если в запросе нет своего\. Виден пользователям с ключом через \/v1\/model\/info/)).toBeInTheDocument();
    expect(screen.getByLabelText("Длина промпта")).toHaveTextContent("0 / 8000");
  });

  it("turns images on by itself with the F16 projector, and lets the admin turn them off", async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      http.post("/api/admin/deployments", async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...fixtures.deploymentDetail, id: 8 }, { status: 201 });
      }),
    );
    const { user } = await openWizard();
    const images = await screen.findByLabelText("Изображения (mmproj)");
    await waitFor(() => expect(images).toBeChecked());
    expect(screen.getByLabelText("Файл проектора")).toHaveValue("mmproj-F16.gguf");
    await waitFor(() => {
      const last = lastEstimate();
      expect(last.get("mmproj_file")).toBe("mmproj-F16.gguf");
    });
    expect(await within(screen.getByRole("region", { name: "Оценка памяти" })).findByText("Проектор изображений")).toBeInTheDocument();

    await user.click(images);
    expect(images).not.toBeChecked();
    expect(screen.queryByLabelText("Файл проектора")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeEnabled());
    await user.click(screen.getByRole("checkbox", { name: "Назначать свободные ноды автоматически" }));
    await user.click(screen.getByRole("button", { name: "Создать развёртывание" }));
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted!.mmproj_file).toBeNull();
  });

  it("sends the settings the admin filled in", async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      http.post("/api/admin/deployments", async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...fixtures.deploymentDetail, id: 8 }, { status: 201 });
      }),
    );
    const { user } = await openWizard();
    await user.selectOptions(screen.getByLabelText("Рассуждение"), "auto");
    await user.selectOptions(screen.getByLabelText("KV-кэш"), "q4_0");
    await user.type(screen.getByLabelText("Температура"), "0.6");
    await user.type(screen.getByLabelText("Top-k"), "40");
    await user.type(screen.getByLabelText("Системный промпт"), "Отвечай кратко");
    expect(screen.getByLabelText("Длина промпта")).toHaveTextContent("14 / 8000");
    await user.click(screen.getByLabelText("Экспериментальная (видна только администраторам)"));
    await user.type(screen.getByLabelText("Свои флаги llama-server"), "--no-warmup");
    await user.click(screen.getByRole("checkbox", { name: "Назначать свободные ноды автоматически" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Создать развёртывание" }));
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({
      reasoning: "auto",
      kv_cache_type: "q4_0",
      sampling: { temperature: 0.6, top_k: 40 },
      system_prompt: "Отвечай кратко",
      experimental: true,
      extra_args: "--no-warmup",
    });
  });

  it("explains a forbidden flag while typing and blocks the form", async () => {
    const { user } = await openWizard();
    await user.type(screen.getByLabelText("Свои флаги llama-server"), "--port 80");
    expect(screen.getByText("Флаг --port задаётся порталом")).toBeInTheDocument();
    expect(screen.getByLabelText("Свои флаги llama-server")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeDisabled();
    expect(screen.getByText("Исправьте выделенные поля")).toBeInTheDocument();
  });

  it("blocks the form on a sampling value out of range", async () => {
    const { user } = await openWizard();
    await user.type(screen.getByLabelText("Температура"), "5");
    expect(screen.getByText("от 0 до 2", { selector: ".error-text" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeDisabled();
  });

  it("asks the estimate with the KV type", async () => {
    const { user } = await openWizard();
    await waitFor(() => expect(lastEstimate().get("kv_cache_type")).toBe("q8_0"));
    await user.selectOptions(screen.getByLabelText("KV-кэш"), "f16");
    await waitFor(() => expect(lastEstimate().get("kv_cache_type")).toBe("f16"));
  });

  it("picks a draft model like the main file and counts it in the estimate", async () => {
    const { user } = await openWizard();
    await user.click(screen.getByLabelText("Draft-модель"));
    await user.type(screen.getByLabelText("Файл draft-модели: репозиторий Hugging Face"), "unsloth/gemma-4-E2B-it-GGUF");
    const picker = screen.getByLabelText("Файл draft-модели: репозиторий Hugging Face").closest(".section") as HTMLElement;
    await user.click(within(picker).getByRole("button", { name: "Найти файлы" }));
    await user.click(await within(picker).findByRole("radio", { name: /Q8_0/ }));
    const panel = screen.getByRole("region", { name: "Оценка памяти" });
    expect(await within(panel).findByText("Draft с KV-кэшем")).toBeInTheDocument();
    await waitFor(() => {
      const last = lastEstimate();
      expect(last.get("draft_repo")).toBe("unsloth/gemma-4-E2B-it-GGUF");
      expect(last.get("draft_file")).toBe("gemma-4-E4B-it-Q8_0.gguf");
    });
    // the draft can have own files too
    expect(within(picker).getByRole("tab", { name: "Свои файлы" })).toBeInTheDocument();
    await user.click(screen.getByLabelText("Draft-модель"));
    expect(screen.queryByLabelText("Файл draft-модели: репозиторий Hugging Face")).not.toBeInTheDocument();
  });

  it("shows the command, collapsed, with a copy button, and the keys masked", async () => {
    const { user } = await openWizard();
    const toggle = await screen.findByRole("button", { name: "Команда llama-server" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const block = await screen.findByText(/llama-server --host 0\.0\.0\.0/);
    expect(block.textContent).toContain("--cache-type-k q8_0 --cache-type-v q8_0");
    expect(block.textContent).toContain("LLAMA_API_KEY=***");
    await waitFor(() => expect(screen.getByText(/--mmproj/)).toBeInTheDocument()); // the projector is chosen once its list is in
    expect(screen.getByRole("button", { name: "Копировать: Сервер" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("KV-кэш"), "q4_0");
    await waitFor(() => expect(screen.getByText(/--cache-type-k q4_0 --cache-type-v q4_0/)).toBeInTheDocument());
  });

  it("shows the portal's 422 text for the form", async () => {
    server.use(
      http.post("/api/admin/deployments/command-preview", () =>
        HttpResponse.json({ detail: "Флаг --seed задаётся порталом" }, { status: 422 }),
      ),
    );
    const { user } = await openWizard();
    await user.type(screen.getByLabelText("Свои флаги llama-server"), "--seed 1");
    // the portal's text sits under the flags field and in the command block
    expect(await screen.findAllByText("Флаг --seed задаётся порталом")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Команда llama-server" }));
    expect(await screen.findAllByText("Флаг --seed задаётся порталом")).toHaveLength(2);
  });
});

describe("«Создать похожее»", () => {
  it("prefills the wizard except the name and the nodes", async () => {
    renderApp("/admin/deployments/new?from=2", "admin");
    expect(await screen.findByText(/Скопировано из/)).toHaveTextContent("Скопировано из qwen3-30b-a3b");
    expect(screen.getByLabelText("Имя развёртывания")).toHaveValue("");
    expect(screen.getByRole("radio", { name: "Разделение по нодам" })).toBeChecked();
    expect(screen.getByLabelText("Контекст на запрос")).toHaveValue(16384);
    expect(screen.getByLabelText("Параллельность")).toHaveValue(1);
    expect(screen.getByLabelText("Ускоритель")).toHaveValue("cpu");
    expect(screen.getByLabelText("KV-кэш")).toHaveValue("q8_0");
    expect(screen.getByRole("checkbox", { name: "Назначать свободные ноды автоматически" })).not.toBeChecked();
    for (const box of within(await screen.findByRole("group", { name: "Ноды" })).getAllByRole("checkbox")) {
      expect(box).not.toBeChecked();
    }
    expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeDisabled();
    expect(screen.getByText("Укажите имя развёртывания")).toBeInTheDocument();
  });

  it("creates the copy with the copied settings and the new name", async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      http.get("/api/admin/deployments/:id", ({ params }) =>
        HttpResponse.json({
          ...fixtures.deploymentDetail,
          id: Number(params.id),
          reasoning: "on",
          kv_cache_type: "q4_0",
          sampling: { temperature: 0.3 },
          extra_args: "--no-warmup",
          system_prompt: "Кратко",
          experimental: true,
          mmproj_file: "mmproj-BF16.gguf",
        }),
      ),
      http.post("/api/admin/deployments", async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...fixtures.deploymentDetail, id: 9 }, { status: 201 });
      }),
    );
    const { user } = renderApp("/admin/deployments/new?from=1", "admin");
    await screen.findByText(/Скопировано из/);
    expect(screen.getByLabelText("Рассуждение")).toHaveValue("on");
    expect(screen.getByLabelText("Свои флаги llama-server")).toHaveValue("--no-warmup");
    await user.type(screen.getByLabelText("Имя развёртывания"), "copy-1");
    await waitFor(() => expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeEnabled());
    await user.click(screen.getByRole("checkbox", { name: "Назначать свободные ноды автоматически" }));
    await user.click(screen.getByRole("button", { name: "Создать развёртывание" }));
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({
      name: "copy-1",
      reasoning: "on",
      kv_cache_type: "q4_0",
      sampling: { temperature: 0.3 },
      extra_args: "--no-warmup",
      system_prompt: "Кратко",
      experimental: true,
      mmproj_file: "mmproj-BF16.gguf",
      nodes: [],
    });
  });

  it("has a button on the edit page, not for a vLLM deployment", async () => {
    const { unmount } = renderApp("/admin/deployments/1", "admin");
    expect(await screen.findByRole("link", { name: "Создать похожее" })).toHaveAttribute("href", "/admin/deployments/new?from=1");
    unmount();
    renderApp("/admin/deployments/3", "admin");
    await screen.findByRole("heading", { name: "llama-3-1-8b" });
    expect(screen.queryByRole("link", { name: "Создать похожее" })).not.toBeInTheDocument();
  });
});

describe("edit page: settings and command", () => {
  it("shows the saved settings and the saved command", async () => {
    server.use(
      http.get("/api/admin/deployments/:id", () =>
        HttpResponse.json({ ...fixtures.deploymentDetail, reasoning: "on", sampling: { top_p: 0.9 }, extra_args: "--seed 7" }),
      ),
    );
    const { user } = renderApp("/admin/deployments/1", "admin");
    expect(await screen.findByLabelText("Рассуждение")).toHaveValue("on");
    expect(screen.getByLabelText("Top-p")).toHaveValue(0.9);
    expect(screen.getByLabelText("Свои флаги llama-server")).toHaveValue("--seed 7");
    await user.click(await screen.findByRole("button", { name: "Команда llama-server" }));
    expect(await screen.findByText(/--alias gemma-4-e4b/)).toBeInTheDocument();
  });

  it("a changed KV type restarts the pods, a changed prompt does not", async () => {
    let patched: Record<string, unknown> | null = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        patched = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(fixtures.deploymentDetail);
      }),
    );
    const { user } = renderApp("/admin/deployments/1", "admin");
    await user.type(await screen.findByLabelText("Системный промпт"), "Привет");
    await waitFor(() => expect(screen.getByRole("button", { name: "Сохранить" })).toBeEnabled());
    expect(screen.queryByText("Поды перезапустятся по одной ноде")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("KV-кэш"), "f16");
    expect(await screen.findByText("Поды перезапустятся по одной ноде")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(patched).not.toBeNull());
    expect(patched).toEqual({ kv_cache_type: "f16", system_prompt: "Привет" });
  });

  it("asks for the command of the form once it differs from the saved one", async () => {
    const { user } = renderApp("/admin/deployments/1", "admin");
    await screen.findByRole("button", { name: "Команда llama-server" });
    await user.selectOptions(screen.getByLabelText("KV-кэш"), "q4_0");
    const toggle = await screen.findByRole("button", { name: "Команда llama-server с несохранёнными изменениями" });
    await user.click(toggle);
    expect(await screen.findByText(/--cache-type-k q4_0 --cache-type-v q4_0/)).toBeInTheDocument();
  });

  it("shows a vLLM deployment with its engine and without the llama.cpp settings", async () => {
    renderApp("/admin/deployments/3", "admin");
    await screen.findByRole("heading", { name: "llama-3-1-8b" });
    expect(screen.getByText(/Движок vLLM/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Генерация и модель" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Команда llama-server/ })).not.toBeInTheDocument();
  });
});

/** the estimate requests the mock server saw: the last one */
function lastEstimate(): URLSearchParams {
  const seen = estimateCalls.at(-1);
  if (!seen) throw new Error("no estimate request yet");
  return seen;
}

const estimateCalls: URLSearchParams[] = [];
beforeEach(() => {
  estimateCalls.length = 0;
});
server.events.on("request:start", ({ request }) => {
  const url = new URL(request.url);
  if (url.pathname === "/api/admin/hf/estimate") estimateCalls.push(url.searchParams);
});

describe("the estimate panel", () => {
  it("shows the parts besides the model", async () => {
    server.use(
      http.get("/api/admin/hf/estimate", () =>
        HttpResponse.json({
          ...fixtures.hfEstimate,
          estimate: {
            ...fixtures.hfEstimate.estimate,
            draft_weights_bytes: 700 * 2 ** 20,
            draft_kv_bytes: 100 * 2 ** 20,
            mmproj_bytes: 900 * 2 ** 20,
          },
        }),
      ),
    );
    await openWizard();
    const panel = screen.getByRole("region", { name: "Оценка памяти" });
    const draft = await within(panel).findByText("Draft с KV-кэшем");
    expect(draft.closest("div")).toHaveTextContent("800 МБ");
    expect(within(panel).getByText("Проектор изображений").closest("div")).toHaveTextContent("900 МБ");
  });
});
