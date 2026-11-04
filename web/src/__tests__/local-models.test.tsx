import { screen, waitFor, within } from "@testing-library/react";
import { delay, http, HttpResponse } from "msw";
import { setCsrfToken } from "../api";
import { ChunkedUpload, suggestModelName, type UploadState } from "../lib/upload";
import { fixtures, putUploadPart, state } from "../mocks/handlers";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";
import type { ModelUpload } from "../types";

const gguf = (name: string, size: number) => new File([new Uint8Array(size).fill(7)], name);

describe("«Мои модели» page", () => {
  it("lists own models with their files, memory estimate, state and deployments", async () => {
    renderApp("/admin/local-models", "admin");
    const table = await screen.findByRole("table", { name: "Мои модели" });
    expect(within(table).getByText("local/Qwen3-235B-A22B-Q2_K")).toBeInTheDocument();
    const big = within(table).getByRole("row", { name: "Qwen3-235B-A22B-Q2_K/Qwen3-235B-A22B-Q2_K-00001-of-00002.gguf" });
    expect(within(big).getByText("Q2_K")).toBeInTheDocument();
    expect(within(big).getByText("qwen3moe")).toBeInTheDocument();
    expect(within(big).getByText("2 части, из каталога")).toBeInTheDocument();
    expect(within(big).getByText("82,9 ГБ")).toBeInTheDocument(); // estimate at 8192 × 2
    expect(within(big).getByText("qwen3-235b")).toBeInTheDocument();
    const broken = within(table).getByRole("row", { name: "gemma-3-270m-it/broken.gguf" });
    expect(within(broken).getByText("Файл не является GGUF")).toBeInTheDocument();
    // the upload another session started and left
    const pending = screen.getByRole("listitem", { name: "Загрузка DeepSeek-R1-IQ1_S-00001-of-00003.gguf" });
    expect(within(pending).getByText(/Прервана на 25 %/)).toBeInTheDocument();
    expect(screen.getByText(/\/var\/lib\/llm-hf\/models/, { selector: ".hint .mono" })).toBeInTheDocument();
  });

  it("uploads a file part by part with CSRF and hands it over for verification", async () => {
    const parts: { offset: string | null; size: number; csrf: string | null; type: string | null }[] = [];
    server.events.on("request:start", async ({ request }) => {
      if (request.method === "PUT" && request.url.includes("/uploads/")) {
        const clone = request.clone();
        parts.push({
          offset: new URL(clone.url).searchParams.get("offset"),
          size: (await clone.arrayBuffer()).byteLength,
          csrf: clone.headers.get("X-CSRF-Token"),
          type: clone.headers.get("Content-Type"),
        });
      }
    });
    const { user } = renderApp("/admin/local-models", "admin");
    await screen.findByRole("table", { name: "Мои модели" });
    await user.upload(screen.getByLabelText("Файлы GGUF"), gguf("My-Model-Q4_K_M.gguf", 10));
    expect(screen.getByLabelText("Модель")).toHaveValue("My-Model-Q4_K_M");
    await user.click(screen.getByRole("button", { name: "Загрузить" }));
    await waitFor(() => expect(state.local.uploads.at(-1)?.status).toBe("verifying"));
    server.events.removeAllListeners();
    expect(parts.map((p) => [p.offset, p.size])).toEqual([
      ["0", 4],
      ["4", 4],
      ["8", 2],
    ]);
    expect(parts.every((p) => p.csrf === "csrf-test-token-admin" && p.type === "application/octet-stream")).toBe(true);
    const row = await screen.findByRole("listitem", { name: "Загрузка My-Model-Q4_K_M.gguf" });
    expect(within(row).getByText(/Проверка sha256 и заголовка GGUF/)).toBeInTheDocument();
  });

  it("refuses a model name the portal would reject and a malformed sha256", async () => {
    const { user } = renderApp("/admin/local-models", "admin");
    await user.upload(await screen.findByLabelText("Файлы GGUF"), gguf("m.gguf", 10));
    await user.clear(screen.getByLabelText("Модель"));
    await user.type(screen.getByLabelText("Модель"), "a/b");
    expect(screen.getByText(/Латинские буквы, цифры, точка/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Загрузить" })).toBeDisabled();
    await user.clear(screen.getByLabelText("Модель"));
    await user.type(screen.getByLabelText("Модель"), "ok");
    await user.type(screen.getByLabelText("sha256 файла, необязательно"), "abc");
    expect(screen.getByText("64 шестнадцатеричных символа")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Загрузить" })).toBeDisabled();
  });

  it("continues an interrupted upload only with the same file", async () => {
    const { user } = renderApp("/admin/local-models", "admin");
    const pending = await screen.findByRole("listitem", { name: "Загрузка DeepSeek-R1-IQ1_S-00001-of-00003.gguf" });
    const picker = within(pending).getByLabelText("Выбрать файл DeepSeek-R1-IQ1_S-00001-of-00003.gguf, чтобы продолжить");
    await user.upload(picker, gguf("other.gguf", 10));
    expect(within(pending).getByText(/Это другой файл/)).toBeInTheDocument();
  });

  it("deletes an unused file after confirmation and shows why a used model stays", async () => {
    const { user } = renderApp("/admin/local-models", "admin");
    const table = await screen.findByRole("table", { name: "Мои модели" });
    await user.click(within(table).getByRole("button", { name: "Удалить модель Qwen3-235B-A22B-Q2_K" }));
    await user.click(screen.getByRole("button", { name: "Удалить" }));
    expect(await screen.findByText("Модель используется развёртываниями: qwen3-235b")).toBeInTheDocument();

    await user.click(within(table).getByRole("button", { name: "Удалить файл broken.gguf" }));
    expect(screen.getByText(/Удалить файл broken.gguf из модели gemma-3-270m-it/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Удалить" }));
    await waitFor(() => expect(screen.queryByRole("row", { name: "gemma-3-270m-it/broken.gguf" })).not.toBeInTheDocument());
  });

  it("looks into the folder on demand", async () => {
    let scanned = false;
    server.use(
      http.post("/api/admin/local-models/scan", () => {
        scanned = true;
        return HttpResponse.json(fixtures.localModels);
      }),
    );
    const { user } = renderApp("/admin/local-models", "admin");
    await user.click(await screen.findByRole("button", { name: "Найти файлы в каталоге" }));
    await waitFor(() => expect(scanned).toBe(true));
  });

  it("is in the admin menu only", async () => {
    renderApp("/models", "admin");
    expect(await screen.findByRole("link", { name: "Мои модели" })).toHaveAttribute("href", "/admin/local-models");
  });
});

describe("uploads of several files, pause and cancel", () => {
  /** every PUT waits a little, so a click lands while a part is on its way */
  function slowParts(log: string[]) {
    server.use(
      http.put("/api/admin/local-models/uploads/:id", async (ctx) => {
        log.push(String(ctx.params.id));
        await delay(25);
        return putUploadPart(ctx);
      }),
    );
  }

  it("a paused first file holds the queue: the next one waits and never starts on its own", async () => {
    const log: string[] = [];
    slowParts(log);
    const { user } = renderApp("/admin/local-models", "admin");
    await user.upload(await screen.findByLabelText("Файлы GGUF"), [
      gguf("Big-Q2_K-00001-of-00002.gguf", 40),
      gguf("Big-Q2_K-00002-of-00002.gguf", 40),
    ]);
    await user.click(screen.getByRole("button", { name: "Загрузить" }));
    const first = await screen.findByRole("listitem", { name: "Загрузка Big-Q2_K-00001-of-00002.gguf" });
    const second = await screen.findByRole("listitem", { name: "Загрузка Big-Q2_K-00002-of-00002.gguf" });
    expect(within(second).getByText(/Ждёт очереди/)).toBeInTheDocument();
    await waitFor(() => expect(log.length).toBeGreaterThan(0));
    await user.click(within(first).getByRole("button", { name: "Пауза" }));
    await within(first).findByRole("button", { name: "Продолжить" });
    const secondId = state.local.uploads.find((u) => u.file === "Big-Q2_K-00002-of-00002.gguf")!.id;
    await delay(150);
    expect(log).not.toContain(secondId);
    expect(within(second).getByText(/Ждёт очереди/)).toBeInTheDocument();

    await user.click(within(first).getByRole("button", { name: "Продолжить" }));
    await waitFor(() =>
      expect(state.local.uploads.filter((u) => u.model === "Big-Q2_K").map((u) => u.status)).toEqual([
        "verifying",
        "verifying",
      ]),
    );
    expect(log).toContain(secondId);
  });

  it("leaving the page stops the queue", async () => {
    const log: string[] = [];
    slowParts(log);
    const { user, unmount } = renderApp("/admin/local-models", "admin");
    await user.upload(await screen.findByLabelText("Файлы GGUF"), [
      gguf("q-00001-of-00002.gguf", 40),
      gguf("q-00002-of-00002.gguf", 40),
    ]);
    await user.click(screen.getByRole("button", { name: "Загрузить" }));
    await waitFor(() => expect(log.length).toBeGreaterThan(0));
    unmount();
    await delay(100);
    const sent = log.length;
    await delay(200);
    expect(log.length).toBe(sent);
    const second = state.local.uploads.find((u) => u.file === "q-00002-of-00002.gguf")!;
    expect(second.received_bytes).toBe(0);
  });

  it("a refused part leaves an upload the server still accepts: «Продолжить» goes on from there", async () => {
    let refused = false;
    server.use(
      http.put("/api/admin/local-models/uploads/:id", (ctx) => {
        if (!refused) {
          refused = true;
          return HttpResponse.json({ detail: "Часть файла больше 32 МБ" }, { status: 413 });
        }
        return putUploadPart(ctx);
      }),
    );
    const { user } = renderApp("/admin/local-models", "admin");
    await user.upload(await screen.findByLabelText("Файлы GGUF"), gguf("solo.gguf", 10));
    await user.click(screen.getByRole("button", { name: "Загрузить" }));
    const row = await screen.findByRole("listitem", { name: "Загрузка solo.gguf" });
    expect(await within(row).findByText("Часть файла больше 32 МБ")).toBeInTheDocument();
    await user.click(await within(row).findByRole("button", { name: "Продолжить" }));
    await waitFor(() => expect(state.local.uploads.find((u) => u.file === "solo.gguf")?.status).toBe("verifying"));
  });

  it("a file that fails verification after the upload stays in the list with the reason", async () => {
    server.use(
      http.post("/api/admin/local-models/uploads/:id/finish", ({ params }) => {
        const up = state.local.uploads.find((u) => u.id === params.id)!;
        up.status = "failed";
        up.error = "Файл не является GGUF: неверная сигнатура";
        return HttpResponse.json(up, { status: 202 });
      }),
    );
    const { user } = renderApp("/admin/local-models", "admin");
    await user.upload(await screen.findByLabelText("Файлы GGUF"), gguf("bad.gguf", 10));
    await user.click(screen.getByRole("button", { name: "Загрузить" }));
    expect(await screen.findByText("Файл не является GGUF: неверная сигнатура")).toBeInTheDocument();
    const row = screen.getByRole("listitem", { name: "Загрузка bad.gguf" });
    expect(within(row).getByRole("button", { name: "Убрать из списка" })).toBeInTheDocument();
  });

  it("cancel asks first, naming what is already on the server", async () => {
    const { user } = renderApp("/admin/local-models", "admin");
    const name = "DeepSeek-R1-IQ1_S-00001-of-00003.gguf";
    const pending = await screen.findByRole("listitem", { name: `Загрузка ${name}` });
    await user.click(within(pending).getByRole("button", { name: "Отменить" }));
    const dialog = screen.getByRole("dialog", { name: "Отменить загрузку" });
    expect(within(dialog).getByText(/11,5\sГБ из 46,0\sГБ/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    expect(state.local.uploads.some((u) => u.file === name)).toBe(true);
    await user.click(within(pending).getByRole("button", { name: "Отменить" }));
    const again = screen.getByRole("dialog", { name: "Отменить загрузку" });
    await user.click(within(again).getByRole("button", { name: "Отменить загрузку" }));
    await waitFor(() => expect(state.local.uploads.some((u) => u.file === name)).toBe(false));
  });
});

describe("ChunkedUpload", () => {
  function upload(size: number, chunk = 4): ModelUpload {
    const up = {
      ...fixtures.localModels.uploads[0],
      id: "00000000-0000-4000-8000-000000000999",
      model: "m",
      file: "m.gguf",
      size_bytes: size,
      received_bytes: 0,
      chunk_bytes: chunk,
      status: "uploading",
    } as ModelUpload;
    state.local.uploads.push(up);
    return up;
  }

  beforeEach(() => {
    state.role = "admin";
    setCsrfToken("csrf-test-token-admin");
  });

  it("pauses after the current part and resumes from the server's offset", async () => {
    const up = upload(12);
    let paused = false;
    const job: ChunkedUpload = new ChunkedUpload(gguf("m.gguf", 12), up, (s: UploadState) => {
      if (!paused && s.sent === 4 && s.phase === "uploading") {
        paused = true;
        job.pause();
      }
    });
    await job.start();
    expect(job.current.phase).toBe("paused");
    expect(job.current.sent).toBe(4);
    await job.resume();
    expect(job.current.phase).toBe("verifying");
    expect(state.local.uploads.at(-1)?.received_bytes).toBe(12);
  });

  it("follows the offset the server reports after a lost answer", async () => {
    const up = upload(8);
    state.local.uploads.at(-1)!.received_bytes = 4; // the first part arrived, its answer did not
    const job = new ChunkedUpload(gguf("m.gguf", 8), up, () => undefined);
    await job.start();
    expect(job.current.phase).toBe("verifying");
  });

  it("retries a network error, then pauses with a message", async () => {
    const up = upload(8);
    server.use(http.put("/api/admin/local-models/uploads/:id", () => HttpResponse.error()));
    const job = new ChunkedUpload(gguf("m.gguf", 8), up, () => undefined, [0, 0]);
    await job.start();
    expect(job.current.phase).toBe("paused");
    expect(job.current.error).toMatch(/Связь с сервером прервалась/);
  });

  it("retries a part another request still holds (409 without Upload-Offset)", async () => {
    const up = upload(8);
    let busy = 2;
    server.use(
      http.put("/api/admin/local-models/uploads/:id", (ctx) => {
        if (busy-- > 0)
          return HttpResponse.json({ detail: "Эта загрузка уже принимает часть файла в другом запросе" }, { status: 409 });
        return putUploadPart(ctx);
      }),
    );
    const job = new ChunkedUpload(gguf("m.gguf", 8), up, () => undefined, [0, 0, 0]);
    await job.start();
    expect(job.current.phase).toBe("verifying");
  });

  it("stops with the server's reason when a part is refused", async () => {
    const up = upload(8);
    server.use(
      http.put("/api/admin/local-models/uploads/:id", () =>
        HttpResponse.json({ detail: "Файл не является GGUF" }, { status: 422 }),
      ),
    );
    const job = new ChunkedUpload(gguf("m.gguf", 8), up, () => undefined);
    await job.start();
    expect(job.current).toMatchObject({ phase: "failed", error: "Файл не является GGUF" });
  });

  it("a full disk (507) pauses the upload with the server's reason and does not retry", async () => {
    const up = upload(8);
    let calls = 0;
    server.use(
      http.put("/api/admin/local-models/uploads/:id", () => {
        calls++;
        return HttpResponse.json({ detail: "Не удалось записать часть файла: на диске главного закончилось место" }, { status: 507 });
      }),
    );
    const job = new ChunkedUpload(gguf("m.gguf", 8), up, () => undefined, [0, 0, 0]);
    await job.start();
    expect(calls).toBe(1);
    expect(job.current.phase).toBe("paused");
    expect(job.current.error).toBe("Не удалось записать часть файла: на диске главного закончилось место");
  });

  it("a 409 from an upload the server no longer takes stops with its status, it is not waited out as busy", async () => {
    const up = upload(8);
    let calls = 0;
    server.use(
      http.put("/api/admin/local-models/uploads/:id", () => {
        calls++;
        const row = state.local.uploads.find((u) => u.id === up.id)!;
        row.status = "failed";
        row.error = "Файл не является GGUF";
        return HttpResponse.json({ detail: "Загрузка завершилась ошибкой: Файл не является GGUF" }, { status: 409 });
      }),
    );
    const job = new ChunkedUpload(gguf("m.gguf", 8), up, () => undefined, [0, 0, 0]);
    await job.start();
    expect(calls).toBe(1);
    expect(job.current.phase).toBe("failed");
    expect(job.current.error).toBe("Загрузка завершилась ошибкой: Файл не является GGUF");
  });

  it("a 409 while the server already verifies the file goes on to verification", async () => {
    const up = upload(8);
    server.use(
      http.put("/api/admin/local-models/uploads/:id", () => {
        state.local.uploads.find((u) => u.id === up.id)!.status = "verifying";
        return HttpResponse.json({ detail: "Файл уже загружен и проверяется" }, { status: 409 });
      }),
    );
    const job = new ChunkedUpload(gguf("m.gguf", 8), up, () => undefined, [0]);
    await job.start();
    expect(job.current.phase).toBe("verifying");
  });

  it("resume of an upload that is no longer uploading says why", async () => {
    const up = upload(8);
    const row = state.local.uploads.find((u) => u.id === up.id)!;
    row.status = "failed";
    row.error = null;
    const job = new ChunkedUpload(gguf("m.gguf", 8), up, () => undefined);
    await job.resume();
    expect(job.current.phase).toBe("failed");
    expect(job.current.error).toBeTruthy();
  });

  it("cancel is final only once the server removed the upload; a refused DELETE leaves it paused with the reason", async () => {
    const up = upload(8);
    server.use(
      http.delete("/api/admin/local-models/uploads/:id", () =>
        HttpResponse.json({ detail: "Загрузка занята, повторите позже" }, { status: 409 }),
      ),
    );
    const phases: string[] = [];
    const watched = new ChunkedUpload(gguf("m.gguf", 8), up, (s) => phases.push(s.phase));
    await expect(watched.cancel()).rejects.toMatchObject({ detail: "Загрузка занята, повторите позже" });
    expect(phases).not.toContain("cancelled");
    expect(watched.current.phase).not.toBe("cancelled");
    expect(state.local.uploads.some((u) => u.id === up.id)).toBe(true);
  });

  it("cancel removes the upload on the server", async () => {
    const up = upload(8);
    const job = new ChunkedUpload(gguf("m.gguf", 8), up, () => undefined);
    await job.cancel();
    expect(job.current.phase).toBe("cancelled");
    expect(state.local.uploads.some((u) => u.id === up.id)).toBe(false);
  });

  it("suggests the model name the portal would", () => {
    expect(suggestModelName("Qwen3-235B-A22B-Q2_K-00001-of-00003.gguf")).toBe("Qwen3-235B-A22B-Q2_K");
    expect(suggestModelName("gemma 3 (1).gguf")).toBe("gemma-3-1");
    expect(suggestModelName("___.gguf")).toBe("model");
  });
});

describe("deployment wizard, «Свои файлы»", () => {
  it("offers the ready own files and creates a deployment of local/<name>", async () => {
    let created: Record<string, unknown> | null = null;
    let estimated: string | null = null;
    server.use(
      http.post("/api/admin/deployments", async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...created, id: 9 }, { status: 201 });
      }),
    );
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/admin/hf/estimate")) estimated = new URL(request.url).searchParams.get("repo");
    });
    const { user } = renderApp("/admin/deployments/new", "admin");
    await user.click(await screen.findByRole("tab", { name: "Свои файлы" }));
    const group = await screen.findByRole("radiogroup", { name: "Свой файл модели" });
    const options = within(group).getAllByRole("radio");
    expect(options).toHaveLength(2); // the broken file is not offered
    expect(screen.queryByLabelText("Репозиторий Hugging Face")).not.toBeInTheDocument();
    await user.click(within(group).getByText("gemma-3-270m-it-Q8_0.gguf"));
    await waitFor(() => expect(estimated).toBe("local/gemma-3-270m-it"));
    server.events.removeAllListeners();
    expect(screen.getByLabelText("Имя развёртывания")).toHaveValue("gemma-3-270m-it");
    await user.click(screen.getByRole("checkbox", { name: "Назначать свободные ноды автоматически" }));
    await user.click(await screen.findByRole("button", { name: "Создать развёртывание" }));
    await waitFor(() => expect(created).not.toBeNull());
    expect(created).toMatchObject({ repo: "local/gemma-3-270m-it", file: "gemma-3-270m-it-Q8_0.gguf", engine: "llamacpp" });
  });
});
