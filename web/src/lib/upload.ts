import { api, ApiError, OffsetMismatch } from "../api";
import type { ModelUpload } from "../types";

/** queued: created on the server, waiting for the files before it (a multi-file upload sends one file at a time) */
export type UploadPhase = "queued" | "uploading" | "paused" | "verifying" | "failed" | "cancelled";

export interface UploadState {
  upload: ModelUpload;
  /** bytes the server has */
  sent: number;
  phase: UploadPhase;
  error: string | null;
}

/** Same rule as portal/app/localmodels/store.py `suggest_name`: the file name without shard suffix and `.gguf`. */
export function suggestModelName(file: string): string {
  const stem = file
    .replace(/-\d{5}-of-\d{5}\.gguf$/i, "")
    .replace(/\.gguf$/i, "")
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "")
    .replace(/\.{2,}/g, ".")
    .slice(0, 96)
    .replace(/[-._]+$/g, "");
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/.test(stem) ? stem : "model";
}

const NOT_UPLOADING: Partial<Record<ModelUpload["status"], string>> = {
  done: "Загрузка уже завершена",
  cancelled: "Загрузка отменена",
  failed: "Загрузка завершилась ошибкой",
};

export const MODEL_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;

/**
 * Sends a file to an upload part by part. Each part goes to the offset the server already has: after a pause, a
 * broken connection or a reload (a new `ChunkedUpload` for the same upload) it continues from there. A network error
 * is retried a few times, then the upload pauses with a message.
 */
export class ChunkedUpload {
  private state: UploadState;
  private ctrl: AbortController | null = null;
  private running = false;

  constructor(
    private readonly file: Blob,
    upload: ModelUpload,
    private readonly onChange: (s: UploadState) => void,
    private readonly retryDelaysMs: number[] = [1000, 3000, 10000],
  ) {
    this.state = { upload, sent: upload.received_bytes, phase: "queued", error: null };
  }

  get current(): UploadState {
    return this.state;
  }

  private set(patch: Partial<UploadState>) {
    this.state = { ...this.state, ...patch };
    this.onChange(this.state);
  }

  /** Starts or continues; resolves when the upload pauses, fails or reaches verification. */
  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    this.set({ phase: "uploading", error: null });
    try {
      await this.loop();
    } finally {
      this.running = false;
      this.ctrl = null;
    }
  }

  private async loop(): Promise<void> {
    const { id, size_bytes: size, chunk_bytes: chunk } = this.state.upload;
    let failures = 0;
    while (this.state.sent < size) {
      if (this.state.phase !== "uploading") return;
      const from = this.state.sent;
      this.ctrl = new AbortController();
      try {
        const up = await api.putUploadPart(id, from, this.file.slice(from, Math.min(size, from + chunk)), this.ctrl.signal);
        failures = 0;
        this.set({ upload: up, sent: up.received_bytes });
      } catch (e) {
        if ((e as Error).name === "AbortError") return; // paused or cancelled
        if (e instanceof OffsetMismatch) {
          this.set({ sent: e.offset });
          continue;
        }
        if (e instanceof ApiError && e.status === 507) {
          // the server's disk is full or gone: waiting does not help, the admin frees space and continues
          this.set({ phase: "paused", error: e.detail });
          return;
        }
        // 409 without Upload-Offset: either a part of this upload is still arriving in another request (a broken one
        // the server has not noticed yet) - that is waited out like a network error - or the upload no longer takes
        // data at all (it is being verified, failed or gone), which the server's own state tells
        let busy = false;
        if (e instanceof ApiError && e.status === 409) {
          const now = await this.serverState();
          if (now && now.status !== "uploading") {
            if (now.status === "verifying") this.set({ upload: now, phase: "verifying" });
            else this.set({ upload: now, phase: "failed", error: e.detail });
            return;
          }
          if (now) this.set({ upload: now, sent: now.received_bytes });
          busy = true;
        }
        if (e instanceof ApiError && e.status !== 0 && e.status < 500 && !busy) {
          await this.fail(e.detail);
          return;
        }
        if (failures >= this.retryDelaysMs.length) {
          this.set({
            phase: "paused",
            error: busy
              ? "Сервер ещё принимает прежнюю часть этой загрузки. Нажмите «Продолжить» через минуту"
              : "Связь с сервером прервалась. Нажмите «Продолжить», загрузка пойдёт с того же места",
          });
          return;
        }
        await new Promise((r) => setTimeout(r, this.retryDelaysMs[failures++]));
        if (this.state.phase !== "uploading") return;
        try {
          const up = await api.upload(id); // the broken part may have partly arrived
          this.set({ upload: up, sent: up.received_bytes });
        } catch {
          /* the next attempt tells */
        }
      }
    }
    try {
      const up = await api.finishUpload(id);
      this.set({ upload: up, phase: "verifying" });
    } catch (e) {
      await this.fail(e instanceof ApiError ? e.detail : "Не удалось завершить загрузку");
    }
  }

  /** The upload as the server has it now; null when that cannot be read (the caller then treats it as a hiccup). */
  private async serverState(): Promise<ModelUpload | null> {
    try {
      return await api.upload(this.state.upload.id);
    } catch {
      return null;
    }
  }

  /** Stops with a reason; the server's state is read again, so the page knows whether «Продолжить» can still work. */
  private async fail(error: string) {
    let upload = this.state.upload;
    try {
      upload = await api.upload(upload.id);
    } catch {
      /* the last known state stays */
    }
    this.set({ upload, sent: upload.status === "uploading" ? upload.received_bytes : this.state.sent, phase: "failed", error });
  }

  /** Failed, but the server still takes parts of this upload (the refusal was about one request, not the file). */
  get resumable(): boolean {
    return this.state.phase === "failed" && this.state.upload.status === "uploading";
  }

  pause() {
    if (this.state.phase !== "uploading") return;
    this.set({ phase: "paused" });
    this.ctrl?.abort();
  }

  async resume(): Promise<void> {
    if (this.running) return;
    try {
      const up = await api.upload(this.state.upload.id);
      if (up.status !== "uploading") {
        this.set({
          upload: up,
          phase: up.status === "verifying" ? "verifying" : "failed",
          error: up.status === "verifying" ? null : (up.error ?? NOT_UPLOADING[up.status] ?? "Загрузка не принимает данные"),
        });
        return;
      }
      this.set({ upload: up, sent: up.received_bytes });
    } catch (e) {
      this.set({ error: e instanceof ApiError ? e.detail : "Нет связи с сервером" });
      return;
    }
    await this.start();
  }

  /** Stops sending and removes the upload on the server. The upload counts as cancelled only once the server did
   * it; if that fails the upload stays (paused, or as it was) and the error goes to the caller. */
  async cancel(): Promise<void> {
    const before = this.state.phase;
    if (before === "uploading") this.set({ phase: "paused" });
    this.ctrl?.abort();
    try {
      await api.cancelUpload(this.state.upload.id);
    } catch (e) {
      if (before !== "uploading") this.set({ phase: before });
      throw e;
    }
    this.set({ phase: "cancelled" });
  }
}
