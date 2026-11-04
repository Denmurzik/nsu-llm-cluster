import { vi } from "vitest";
import {
  AttachmentError,
  MAX_TEXT_BYTES,
  composeContent,
  composeText,
  contextWarning,
  decodeText,
  downscaleImage,
  estimateTokens,
  fenceLang,
  fitSize,
  isImageFile,
  isTextFile,
  readTextFile,
  textBlock,
} from "../lib/attachments";

const file = (bytes: BlobPart[], name: string, type = "") => new File(bytes, name, { type });

describe("fitSize", () => {
  it("keeps a small image as it is", () => {
    expect(fitSize(800, 600)).toEqual({ width: 800, height: 600 });
    expect(fitSize(1536, 1536)).toEqual({ width: 1536, height: 1536 });
  });

  it("scales the long side down to 1536 and keeps the proportions", () => {
    expect(fitSize(3072, 2048)).toEqual({ width: 1536, height: 1024 });
    expect(fitSize(1000, 4000)).toEqual({ width: 384, height: 1536 });
    expect(fitSize(4032, 3024)).toEqual({ width: 1536, height: 1152 });
  });

  it("never returns a zero side", () => {
    expect(fitSize(30000, 1)).toEqual({ width: 1536, height: 1 });
  });
});

describe("file kinds", () => {
  it("knows png, jpeg and webp images only", () => {
    expect(isImageFile({ type: "image/png" })).toBe(true);
    expect(isImageFile({ type: "image/jpeg" })).toBe(true);
    expect(isImageFile({ type: "image/webp" })).toBe(true);
    expect(isImageFile({ type: "image/gif" })).toBe(false);
    expect(isImageFile({ type: "image/svg+xml" })).toBe(false);
  });

  it("knows the text extensions in any case and rejects the rest", () => {
    for (const name of ["a.txt", "A.MD", "x.tsv", "m.yaml", "s.ps1", "q.sql", "h.hpp", "App.tsx", "Main.java", "x.go"]) {
      expect(isTextFile({ name }), name).toBe(true);
    }
    for (const name of ["a.pdf", "a.docx", "a.exe", "noext", "a.png", "archive.zip.txt.bak"]) {
      expect(isTextFile({ name }), name).toBe(false);
    }
  });
});

describe("text block", () => {
  it("is the name, then the content in a fenced block with the language", () => {
    expect(textBlock("main.py", "print(1)\n")).toBe("Файл: main.py\n```python\nprint(1)\n```");
    expect(textBlock("notes.txt", "строка")).toBe("Файл: notes.txt\n```\nстрока\n```");
    expect(textBlock("a.json", '{"a": 1}')).toBe('Файл: a.json\n```json\n{"a": 1}\n```');
  });

  it("uses a longer fence when the file has a fence inside", () => {
    const md = "# Заголовок\n```js\nx\n```\n";
    const block = textBlock("README.md", md);
    expect(block.startsWith("Файл: README.md\n````markdown\n")).toBe(true);
    expect(block.endsWith("\n````")).toBe(true);
  });

  it("names the language of the common extensions", () => {
    expect(fenceLang("a.ts")).toBe("typescript");
    expect(fenceLang("a.yml")).toBe("yaml");
    expect(fenceLang("a.sh")).toBe("bash");
    expect(fenceLang("a.go")).toBe("go");
    expect(fenceLang("a.log")).toBe("");
  });

  it("puts the file blocks before what the user typed", () => {
    const text = composeText("  Что тут не так?  ", [
      { name: "a.py", content: "x = 1" },
      { name: "b.txt", content: "y" },
    ]);
    expect(text).toBe("Файл: a.py\n```python\nx = 1\n```\n\nФайл: b.txt\n```\ny\n```\n\nЧто тут не так?");
    expect(composeText("просто", [])).toBe("просто");
    expect(composeText("", [])).toBe("");
  });
});

describe("content parts", () => {
  it("stays a string without images", () => {
    expect(composeContent("привет", [])).toBe("привет");
  });

  it("is a list of parts with images: the text first, then every image", () => {
    expect(composeContent("что это?", [{ url: "data:image/jpeg;base64,AAA" }, { url: "data:image/jpeg;base64,BBB" }])).toEqual([
      { type: "text", text: "что это?" },
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,AAA" } },
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,BBB" } },
    ]);
  });

  it("leaves out the text part of an image-only message", () => {
    expect(composeContent("", [{ url: "data:image/jpeg;base64,AAA" }])).toEqual([
      { type: "image_url", image_url: { url: "data:image/jpeg;base64,AAA" } },
    ]);
  });
});

describe("context warning", () => {
  it("estimates a token as 3,5 characters", () => {
    expect(estimateTokens(350)).toBe(100);
    expect(estimateTokens(351)).toBe(101);
    expect(estimateTokens(0)).toBe(0);
  });

  it("is silent while it fits, and tells the estimate and the limit when it does not", () => {
    expect(contextWarning(35_000, 10_000)).toBeNull();
    const text = contextWarning(70_000, 8192);
    expect(text).toBe("Похоже, не поместится в контекст модели (~20 000 токенов из 8 192)");
    expect(contextWarning(70_000, null)).toBeNull();
  });
});

describe("reading a text file", () => {
  it("reads UTF-8, Russian included, and strips a byte order mark", async () => {
    const r = await readTextFile(file(["﻿Привет, мир\n"], "hello.txt"));
    expect(r).toEqual({ name: "hello.txt", content: "Привет, мир\n" });
  });

  it("refuses a file that is not valid UTF-8 with «Файл не похож на текст»", async () => {
    const cp1251 = new Uint8Array([0xcf, 0xf0, 0xe8, 0xe2, 0xe5, 0xf2]); // «Привет» in windows-1251
    await expect(readTextFile(file([cp1251], "old.txt"))).rejects.toThrow("Файл не похож на текст");
    await expect(readTextFile(file([cp1251], "old.txt"))).rejects.toBeInstanceOf(AttachmentError);
  });

  it("refuses a binary file that happens to decode (NUL bytes)", async () => {
    await expect(readTextFile(file([new Uint8Array([0x61, 0, 0x62])], "bin.txt"))).rejects.toThrow("Файл не похож на текст");
  });

  it("refuses an extension that is not in the list", async () => {
    await expect(readTextFile(file(["x"], "doc.pdf"))).rejects.toThrow("Тип файла «doc.pdf» не поддерживается");
  });

  it("takes 512 KB and refuses one byte more", async () => {
    const ok = file([new Uint8Array(MAX_TEXT_BYTES).fill(0x61)], "big.log");
    await expect(readTextFile(ok)).resolves.toMatchObject({ name: "big.log" });
    const over = file([new Uint8Array(MAX_TEXT_BYTES + 1).fill(0x61)], "big.log");
    await expect(readTextFile(over)).rejects.toThrow("Файл «big.log» больше 512 КБ");
  });

  it("decodeText reports null for bytes that are not text", () => {
    expect(decodeText(new Uint8Array([0xff, 0xfe, 0xfd]).buffer)).toBeNull();
    expect(decodeText(new TextEncoder().encode("ok").buffer as ArrayBuffer)).toBe("ok");
  });
});

describe("downscaleImage", () => {
  const draw = vi.fn();
  const fillRect = vi.fn();
  let toDataURL: ReturnType<typeof vi.fn>;
  let canvas: { width: number; height: number };

  beforeEach(() => {
    draw.mockClear();
    fillRect.mockClear();
    toDataURL = vi.fn(() => "data:image/jpeg;base64,QUJD");
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(function (this: HTMLCanvasElement) {
      canvas = this;
      return { drawImage: draw, fillRect, fillStyle: "" } as unknown as CanvasRenderingContext2D;
    } as never);
    vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockImplementation(toDataURL as never);
    vi.stubGlobal("createImageBitmap", vi.fn(async () => ({ width: 4032, height: 3024, close: vi.fn() })));
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("draws the picture at 1536 px on the long side and encodes JPEG with quality 0,85", async () => {
    const r = await downscaleImage(file(["x"], "photo.png", "image/png"));
    expect(r).toEqual({ url: "data:image/jpeg;base64,QUJD", width: 1536, height: 1152 });
    expect(canvas).toMatchObject({ width: 1536, height: 1152 });
    expect(draw).toHaveBeenCalledWith(expect.anything(), 0, 0, 1536, 1152);
    expect(toDataURL).toHaveBeenCalledWith("image/jpeg", 0.85);
    expect(fillRect).toHaveBeenCalled(); // a transparent PNG gets a white background
  });

  it("does not enlarge a small picture", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn(async () => ({ width: 640, height: 480, close: vi.fn() })));
    const r = await downscaleImage(file(["x"], "small.webp", "image/webp"));
    expect(r).toMatchObject({ width: 640, height: 480 });
  });

  it("refuses a type that is not an image the portal takes", async () => {
    await expect(downscaleImage(file(["x"], "a.gif", "image/gif"))).rejects.toThrow("Тип файла «a.gif» не поддерживается");
  });

  it("reports a picture the browser cannot decode", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn(async () => Promise.reject(new Error("bad"))));
    await expect(downscaleImage(file(["x"], "broken.png", "image/png"))).rejects.toThrow("Изображение «broken.png» не удалось прочитать");
  });
});
