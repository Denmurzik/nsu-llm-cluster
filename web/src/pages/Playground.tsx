import { Paperclip, Send, Square, TriangleAlert, X } from "lucide-react";
import { useEffect, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent } from "react";
import { api, ApiError, streamChat, type ChatMessage } from "../api";
import { ErrorNotice, Ic, PageHead } from "../components/ui";
import {
  AttachmentError,
  MAX_IMAGES,
  MAX_MESSAGE_CHARS,
  MAX_TEXT_FILES,
  NOT_TEXT,
  NO_VISION,
  TOO_LONG,
  boxFor,
  TEXT_EXTENSIONS,
  composeContent,
  composeText,
  contextWarning,
  downscaleImage,
  isImageFile,
  isTextFile,
  readTextFile,
  IMAGE_TYPES,
  type Attachment,
} from "../lib/attachments";
import { useResource } from "../lib/useResource";
import "../styles/tools.css";

/** A turn of the thread. `shown` is what the user attached, kept for the screen (the text files are already in `content`). */
type ThreadMsg = ChatMessage & {
  shown?: { text: string; files: string[]; images: { url: string; width: number; height: number }[] };
};

/** height of a picture in the thread, px */
const THREAD_IMAGE_HEIGHT = 112;

const ACCEPT_TEXT = TEXT_EXTENSIONS.map((e) => `.${e}`).join(",");

let nextAttachmentId = 1;

function charsOf(m: ChatMessage): number {
  return typeof m.content === "string"
    ? m.content.length
    : m.content.reduce((n, p) => n + (p.type === "text" ? p.text.length : 0), 0);
}

export function Playground() {
  const models = useResource(() => api.models(), []);
  const [model, setModel] = useState("");
  const [maxTokens, setMaxTokens] = useState("1024");
  const [draft, setDraft] = useState("");
  const [thread, setThread] = useState<ThreadMsg[]>([]);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [attachError, setAttachError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const picker = useRef<HTMLInputElement>(null);

  const list = models.data ?? [];
  const chosen = list.find((m) => m.name === model) ?? list.find((m) => m.status === "ready") ?? list[0];
  const vision = chosen?.vision === true; // the catalog says which models take images (`vision`)
  const tokens = Number(maxTokens);
  const tokensValid = Number.isInteger(tokens) && tokens >= 1 && tokens <= 4096;

  const textFiles = attachments.filter((a) => a.kind === "text");
  const images = attachments.filter((a) => a.kind === "image");
  const composed = composeText(draft, textFiles);
  // the whole request is what the model reads: the earlier turns too
  const requestChars = thread.reduce((n, m) => n + charsOf(m), 0) + composed.length;
  const warning = contextWarning(requestChars, chosen?.ctx_per_request);
  const tooLong = composed.length > MAX_MESSAGE_CHARS;
  const canSend =
    !!chosen && (draft.trim().length > 0 || attachments.length > 0) && tokensValid && !tooLong;

  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [thread]);
  useEffect(() => () => abort.current?.abort(), []);
  // a model without images cannot take the ones already attached
  useEffect(() => {
    if (chosen && !vision && attachments.some((a) => a.kind === "image")) {
      setAttachments((all) => all.filter((a) => a.kind !== "image"));
      setAttachError("Изображения убраны: эта модель их не принимает");
    }
  }, [chosen?.name, vision]); // eslint-disable-line react-hooks/exhaustive-deps

  async function addFiles(files: File[]) {
    if (files.length === 0) return;
    const errors: string[] = [];
    const added: Attachment[] = [];
    let imageCount = images.length;
    let textCount = textFiles.length;
    for (const file of files) {
      try {
        if (isImageFile(file)) {
          if (!vision) throw new AttachmentError(NO_VISION);
          if (imageCount >= MAX_IMAGES) throw new AttachmentError(`Не больше ${MAX_IMAGES} изображений в сообщении`);
          const scaled = await downscaleImage(file);
          imageCount += 1;
          added.push({
            id: `a${nextAttachmentId++}`,
            kind: "image",
            name: file.name,
            url: scaled.url,
            width: scaled.width,
            height: scaled.height,
          });
        } else if (isTextFile(file)) {
          if (textCount >= MAX_TEXT_FILES) throw new AttachmentError(`Не больше ${MAX_TEXT_FILES} текстовых файлов в сообщении`);
          const text = await readTextFile(file);
          textCount += 1;
          added.push({ id: `a${nextAttachmentId++}`, kind: "text", ...text });
        } else {
          throw new AttachmentError(`Тип файла «${file.name}» не поддерживается`);
        }
      } catch (e) {
        if (e instanceof AttachmentError) errors.push(e.message === NOT_TEXT ? `${file.name}: ${e.message}` : e.message);
        else errors.push(`Не удалось прочитать «${file.name}»`);
      }
    }
    if (added.length > 0) setAttachments((all) => [...all, ...added]);
    setAttachError(errors.length > 0 ? errors.join(". ") : null);
  }

  const removeAttachment = (id: string) => {
    setAttachments((all) => all.filter((a) => a.id !== id));
    setAttachError(null);
  };

  const hasFiles = (e: DragEvent<HTMLDivElement>) => Array.from(e.dataTransfer?.types ?? []).includes("Files") || (e.dataTransfer?.files?.length ?? 0) > 0;

  function onDrop(e: DragEvent<HTMLDivElement>) {
    if (!hasFiles(e)) return; // dragged text goes into the textarea as usual
    e.preventDefault();
    setDragging(false);
    if (busy) return;
    void addFiles(Array.from(e.dataTransfer.files));
  }

  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    const pasted = Array.from(e.clipboardData.files).filter(isImageFile);
    if (pasted.length === 0) return;
    e.preventDefault();
    if (!busy) void addFiles(pasted);
  }

  async function send() {
    if (!canSend || busy || !chosen) return;
    const typed = draft.trim();
    const sentAttachments = attachments;
    const content = composeContent(
      composeText(typed, textFiles),
      images.map((a) => ({ url: a.url })),
    );
    const userTurn: ThreadMsg = {
      role: "user",
      content,
      shown:
        sentAttachments.length > 0
          ? {
              text: typed,
              files: textFiles.map((a) => a.name),
              images: images.map((a) => ({ url: a.url, width: a.width, height: a.height })),
            }
          : undefined,
    };
    const history: ThreadMsg[] = [...thread, userTurn];
    setThread([...history, { role: "assistant", content: "" }]);
    setDraft("");
    setAttachments([]);
    setAttachError(null);
    setError(null);
    setBusy(true);
    const ctrl = new AbortController();
    abort.current = ctrl;
    let received = false;
    let failed = false;
    try {
      await streamChat(
        {
          model: chosen.name,
          messages: history.map(({ role, content: c }) => ({ role, content: c })),
          max_tokens: tokens,
        },
        (delta) => {
          received = true;
          setThread((t) => {
            const next = t.slice();
            const last = next[next.length - 1];
            next[next.length - 1] = { ...last, content: (typeof last.content === "string" ? last.content : "") + delta };
            return next;
          });
        },
        ctrl.signal,
      );
    } catch (e) {
      failed = true;
      setError(e instanceof ApiError ? e.detail : "Не удалось получить ответ");
    } finally {
      if (!received) {
        // nothing came back: take the turn out of the history so a retry does not send two user turns in a row,
        // and give the text and the attachments back to the composer
        if (!failed && !ctrl.signal.aborted) setError("Модель вернула пустой ответ");
        setThread(thread);
        setDraft((d) => (d.trim() ? `${typed}\n${d}` : typed));
        setAttachments((now) => [...sentAttachments, ...now]);
      }
      setBusy(false);
      abort.current = null;
    }
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      void send();
    }
  }

  return (
    <div className="page">
      <PageHead title="Playground" lead="Проверьте модель прямо здесь. Запросы идут через служебный ключ, свой ключ создавать не нужно." />

      <div className="row end">
        <div className="field">
          <label htmlFor="pg-model">Модель</label>
          <select
            id="pg-model"
            className="select"
            value={chosen?.name ?? ""}
            disabled={busy || list.length === 0}
            onChange={(e) => setModel(e.target.value)}
          >
            {list.map((m) => (
              <option key={m.name} value={m.name}>
                {m.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="pg-max">Максимум токенов в ответе</label>
          <input
            id="pg-max"
            className="input"
            type="number"
            min={1}
            max={4096}
            value={maxTokens}
            aria-invalid={!tokensValid}
            onChange={(e) => setMaxTokens(e.target.value)}
          />
        </div>
        <button
          type="button"
          className="btn quiet"
          disabled={busy || thread.length === 0}
          onClick={() => {
            setThread([]);
            setError(null);
          }}
        >
          Очистить
        </button>
      </div>
      {!tokensValid && <p className="error-text">Максимум токенов: число от 1 до 4096</p>}
      {models.error && !models.data && <ErrorNotice error={models.error} />}

      <div className="chat">
        <div className="thread" aria-live="polite">
          {thread.length === 0 && <p className="placeholder">Напишите сообщение, ответ появится здесь по мере генерации</p>}
          {thread.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              <span className="who">{m.role === "user" ? "Вы" : "Модель"}</span>
              <div>
                <div className="text">{m.shown ? m.shown.text : typeof m.content === "string" ? m.content : ""}</div>
                {m.shown && (m.shown.images.length > 0 || m.shown.files.length > 0) && (
                  <div className="attached">
                    {m.shown.images.map((img, n) => (
                      <img
                        key={n}
                        src={img.url}
                        alt={`Изображение ${n + 1} в сообщении`}
                        {...boxFor(img.width, img.height, THREAD_IMAGE_HEIGHT)}
                      />
                    ))}
                    {m.shown.files.map((name, n) => (
                      <span key={n} className="file-chip">
                        <span className="name mono small">{name}</span>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
          <div ref={end} />
        </div>

        {error && <ErrorNotice error={error} />}

        <div className="composer">
          <div
            className={`dropzone${dragging ? " dragging" : ""}`}
            onDragOver={(e) => {
              if (!hasFiles(e)) return;
              e.preventDefault();
              if (!dragging) setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
          >
            <div className="field">
              <label htmlFor="pg-input">Сообщение</label>
              <textarea
                id="pg-input"
                className="textarea"
                value={draft}
                rows={3}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={onKey}
                onPaste={onPaste}
              />
            </div>
          </div>
          <div
            className={`composer-note${attachError || tooLong ? " error" : warning ? " warn" : ""}`}
            role={attachError || tooLong ? "alert" : "status"}
          >
            {attachError ? (
              attachError
            ) : tooLong ? (
              TOO_LONG
            ) : warning ? (
              <>
                <Ic icon={TriangleAlert} />
                {warning}
              </>
            ) : null}
          </div>
          <div className="bar">
            <span className="left">
              <input
                ref={picker}
                type="file"
                hidden
                multiple
                accept={vision ? `${IMAGE_TYPES.join(",")},${ACCEPT_TEXT}` : ACCEPT_TEXT}
                aria-label="Выбор файлов"
                onChange={(e) => {
                  void addFiles(Array.from(e.target.files ?? []));
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                className="btn quiet"
                aria-label="Прикрепить файл"
                title={vision ? "Прикрепить изображение или текстовый файл" : "Прикрепить текстовый файл"}
                disabled={busy}
                onClick={() => picker.current?.click()}
              >
                <Ic icon={Paperclip} />
              </button>
              {attachments.length > 0 ? (
                <div className="attach-tray" aria-label="Вложения">
                  {attachments.map((a) =>
                    a.kind === "image" ? (
                      <span key={a.id} className="thumb">
                        <img src={a.url} alt={a.name} width={36} height={36} />
                        <button type="button" className="btn quiet" aria-label={`Убрать ${a.name}`} onClick={() => removeAttachment(a.id)}>
                          <Ic icon={X} />
                        </button>
                      </span>
                    ) : (
                      <span key={a.id} className="file-chip">
                        <span className="name mono small">{a.name}</span>
                        <button type="button" className="btn quiet" aria-label={`Убрать ${a.name}`} onClick={() => removeAttachment(a.id)}>
                          <Ic icon={X} />
                        </button>
                      </span>
                    ),
                  )}
                </div>
              ) : (
                <span className="hint">
                  <span className="keycap">Ctrl</span> <span className="keycap">Enter</span> отправляет сообщение
                </span>
              )}
            </span>
            {busy ? (
              <button type="button" className="btn" onClick={() => abort.current?.abort()}>
                <Ic icon={Square} />
                Остановить
              </button>
            ) : (
              <button type="button" className="btn primary" disabled={!canSend} onClick={() => void send()}>
                <Ic icon={Send} />
                Отправить
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
