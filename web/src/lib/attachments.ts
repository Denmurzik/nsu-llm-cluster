/** Playground attachments: images (downscaled in the browser) and text files (inserted into the message text). */

import type { ContentPart } from "../api";
import { fmtInt } from "./format";

export const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp"] as const;
export const MAX_IMAGES = 4;
/** long side of a sent image, px */
export const MAX_IMAGE_SIDE = 1536;
export const JPEG_QUALITY = 0.85;

export const TEXT_EXTENSIONS = [
  "txt", "md", "csv", "tsv", "json", "yaml", "yml", "xml", "log", "ini", "toml", "sql", "py", "js", "ts", "tsx", "jsx",
  "java", "c", "h", "cpp", "hpp", "cs", "go", "rs", "rb", "php", "sh", "ps1", "html", "css",
] as const;
export const MAX_TEXT_BYTES = 512 * 1024;
export const MAX_TEXT_FILES = 5;
/** characters per token of mixed Russian and English text, a rough figure */
export const CHARS_PER_TOKEN = 3.5;
/** the portal's limit of one message: five files of 512 KB and the typed text (MAX_CONTENT_CHARS in playground.py) */
export const MAX_MESSAGE_CHARS = 2_700_000;
export const TOO_LONG = "Сообщение длиннее 2 700 000 символов: уберите часть файлов или текста";

export const NOT_TEXT = "Файл не похож на текст";
export const NO_VISION = "Модель не принимает изображения";

/** A rejected file: the message is the Russian text shown to the user. */
export class AttachmentError extends Error {}

export interface ImageAttachment {
  id: string;
  kind: "image";
  name: string;
  /** data:image/jpeg;base64,... */
  url: string;
  /** size of the sent picture, px: its box is known before the browser decodes it */
  width: number;
  height: number;
}

/** The box of a thumbnail of the given height: width by the proportions, both whole pixels. */
export function boxFor(width: number, height: number, maxHeight: number): { width: number; height: number } {
  const h = Math.min(height, maxHeight);
  return { width: Math.max(1, Math.round((width * h) / height)), height: h };
}

export interface TextAttachment {
  id: string;
  kind: "text";
  name: string;
  content: string;
}

export type Attachment = ImageAttachment | TextAttachment;

const TEXT_SET = new Set<string>(TEXT_EXTENSIONS);

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot + 1).toLowerCase();
}

export function isImageFile(file: { type: string }): boolean {
  return (IMAGE_TYPES as readonly string[]).includes(file.type);
}

export function isTextFile(file: { name: string }): boolean {
  return TEXT_SET.has(extensionOf(file.name));
}

/** Size of an image whose long side is at most `max` (never scaled up), proportions kept. */
export function fitSize(width: number, height: number, max = MAX_IMAGE_SIDE): { width: number; height: number } {
  const long = Math.max(width, height);
  if (long <= max) return { width, height };
  const k = max / long;
  return { width: Math.max(1, Math.round(width * k)), height: Math.max(1, Math.round(height * k)) };
}

const FENCE_LANG: Record<string, string> = {
  md: "markdown", py: "python", js: "javascript", ts: "typescript", tsx: "tsx", jsx: "jsx", yml: "yaml",
  sh: "bash", ps1: "powershell", cs: "csharp", rs: "rust", rb: "ruby", hpp: "cpp", h: "c", txt: "", log: "", csv: "",
  tsv: "",
};

/** Info string of the code fence: the extension, with the usual long names for the common ones. */
export function fenceLang(name: string): string {
  const ext = extensionOf(name);
  return ext in FENCE_LANG ? FENCE_LANG[ext] : ext;
}

/** The block put into the message text: the name, then the content in a code fence. */
export function textBlock(name: string, content: string): string {
  // a file that has ``` inside gets a longer fence, so its own fence cannot end the block early
  const longest = Math.max(0, ...(content.match(/`+/g) ?? []).map((m) => m.length));
  const fence = "`".repeat(Math.max(3, longest + 1));
  const body = content.endsWith("\n") ? content.slice(0, -1) : content;
  return `Файл: ${name}\n${fence}${fenceLang(name)}\n${body}\n${fence}`;
}

/** Message text: the file blocks, then what the user typed. */
export function composeText(typed: string, files: { name: string; content: string }[]): string {
  return [...files.map((f) => textBlock(f.name, f.content)), typed.trim()].filter(Boolean).join("\n\n");
}

/** OpenAI content: a plain string, or parts when the message carries images. */
export function composeContent(text: string, images: { url: string }[]): string | ContentPart[] {
  if (images.length === 0) return text;
  const parts: ContentPart[] = text ? [{ type: "text", text }] : [];
  for (const image of images) parts.push({ type: "image_url", image_url: { url: image.url } });
  return parts;
}

export function estimateTokens(chars: number): number {
  return Math.ceil(chars / CHARS_PER_TOKEN);
}

/** The warning under the composer when the request is not likely to fit the model's context. */
export function contextWarning(chars: number, ctx: number | null | undefined): string | null {
  if (!ctx) return null;
  const tokens = estimateTokens(chars);
  if (tokens <= ctx) return null;
  return `Похоже, не поместится в контекст модели (~${fmtInt(tokens)} токенов из ${fmtInt(ctx)})`;
}

function readBytes(file: Blob): Promise<ArrayBuffer> {
  if (typeof file.arrayBuffer === "function") return file.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(file);
  });
}

/** Decodes bytes as UTF-8; null when they are not text (invalid UTF-8 or a NUL byte). */
export function decodeText(bytes: ArrayBuffer): string | null {
  const view = new Uint8Array(bytes);
  if (view.includes(0)) return null;
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(view);
  } catch {
    return null;
  }
}

/** Reads a text file for the message; rejects a wrong extension, a size over 512 KB, a file that is not UTF-8. */
export async function readTextFile(file: File): Promise<{ name: string; content: string }> {
  if (!isTextFile(file)) throw new AttachmentError(`Тип файла «${file.name}» не поддерживается`);
  if (file.size > MAX_TEXT_BYTES) throw new AttachmentError(`Файл «${file.name}» больше 512 КБ`);
  const content = decodeText(await readBytes(file));
  if (content === null) throw new AttachmentError(NOT_TEXT);
  return { name: file.name, content };
}

interface Decoded {
  width: number;
  height: number;
  source: CanvasImageSource;
  release: () => void;
}

async function decodeImage(file: Blob): Promise<Decoded> {
  if (typeof createImageBitmap === "function") {
    const bitmap = await createImageBitmap(file);
    return { width: bitmap.width, height: bitmap.height, source: bitmap, release: () => bitmap.close?.() };
  }
  const url = URL.createObjectURL(file);
  const img = new Image();
  try {
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new AttachmentError("Изображение не удалось прочитать"));
      img.src = url;
    });
  } catch (e) {
    URL.revokeObjectURL(url);
    throw e;
  }
  return {
    width: img.naturalWidth,
    height: img.naturalHeight,
    source: img,
    release: () => URL.revokeObjectURL(url),
  };
}

/** Scales an image to at most 1536 px on the long side and encodes it as JPEG (quality 0.85) in a data URL. */
export async function downscaleImage(file: File): Promise<{ url: string; width: number; height: number }> {
  if (!isImageFile(file)) throw new AttachmentError(`Тип файла «${file.name}» не поддерживается`);
  let decoded: Decoded;
  try {
    decoded = await decodeImage(file);
  } catch (e) {
    if (e instanceof AttachmentError) throw e;
    throw new AttachmentError(`Изображение «${file.name}» не удалось прочитать`);
  }
  try {
    const { width, height } = fitSize(decoded.width, decoded.height);
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new AttachmentError("Браузер не может обработать изображение");
    // JPEG has no alpha: a transparent PNG would turn black
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, width, height);
    ctx.drawImage(decoded.source, 0, 0, width, height);
    return { url: canvas.toDataURL("image/jpeg", JPEG_QUALITY), width, height };
  } finally {
    decoded.release();
  }
}
