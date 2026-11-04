import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { vi } from "vitest";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

type Sent = { model: string; messages: { role: string; content: unknown }[]; max_tokens: number };

const JPEG = "data:image/jpeg;base64,QUJD";

/** answers every chat request and keeps what was sent */
function captureChat() {
  const sent: Sent[] = [];
  server.use(
    http.post("/api/playground/chat", async ({ request }) => {
      sent.push((await request.json()) as Sent);
      const text = `data: ${JSON.stringify({ choices: [{ delta: { content: "ок" } }] })}\n\ndata: [DONE]\n\n`;
      return new HttpResponse(new TextEncoder().encode(text), { headers: { "Content-Type": "text/event-stream" } });
    }),
  );
  return sent;
}

async function open(model = "gemma-4-e4b") {
  const view = renderApp("/playground");
  const select = (await screen.findByLabelText("Модель")) as HTMLSelectElement;
  await waitFor(() => expect(within(select).getAllByRole("option").length).toBe(3));
  if (model !== "gemma-4-e4b") await view.user.selectOptions(select, model);
  return view;
}

const picker = () => screen.getByLabelText("Выбор файлов") as HTMLInputElement;
const textFile = (name: string, body: string) => new File([body], name, { type: "text/plain" });
const imageFile = (name = "photo.png", type = "image/png") => new File(["x"], name, { type });

/** a canvas and a decoder jsdom does not have */
function stubImages(width = 4032, height = 3024) {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    drawImage: vi.fn(),
    fillRect: vi.fn(),
    fillStyle: "",
  } as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue(JPEG);
  vi.stubGlobal("createImageBitmap", vi.fn(async () => ({ width, height, close: vi.fn() })));
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("playground: attach button", () => {
  it("has a paperclip button that opens the file picker", async () => {
    const { user } = await open();
    const click = vi.spyOn(picker(), "click");
    await user.click(screen.getByRole("button", { name: "Прикрепить файл" }));
    expect(click).toHaveBeenCalled();
  });

  it("offers images only for a model with vision", async () => {
    const { user } = await open();
    expect(picker().accept).toContain("image/png");
    expect(picker().accept).toContain(".py");
    await user.selectOptions(screen.getByLabelText("Модель"), "qwen3-30b-a3b");
    await waitFor(() => expect(picker().accept).not.toContain("image/"));
    expect(picker().accept).toContain(".py");
  });
});

describe("playground: text files", () => {
  it("puts the file into the message text as a block and sends it as a string", async () => {
    const sent = captureChat();
    const { user } = await open();
    await user.upload(picker(), textFile("main.py", "print('привет')\n"));
    const tray = await screen.findByLabelText("Вложения");
    expect(within(tray).getByText("main.py")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Сообщение"), "Что делает код?");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].messages).toEqual([
      { role: "user", content: "Файл: main.py\n```python\nprint('привет')\n```\n\nЧто делает код?" },
    ]);
    // the thread shows what was typed and the file name, not the whole file
    await screen.findByText("ок");
    expect(screen.getByText("Что делает код?")).toBeInTheDocument();
    expect(screen.queryByText(/print\('привет'\)/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Вложения")).not.toBeInTheDocument();
  });

  it("can send a file without any typed text", async () => {
    const sent = captureChat();
    const { user } = await open();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
    await user.upload(picker(), textFile("a.txt", "строка"));
    await user.click(await screen.findByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].messages[0].content).toBe("Файл: a.txt\n```\nстрока\n```");
  });

  it("removes an attachment before sending", async () => {
    const sent = captureChat();
    const { user } = await open();
    await user.upload(picker(), [textFile("a.txt", "один"), textFile("b.txt", "два")]);
    const tray = await screen.findByLabelText("Вложения");
    expect(within(tray).getByText("a.txt")).toBeInTheDocument();
    await user.click(within(tray).getByRole("button", { name: "Убрать a.txt" }));
    expect(within(screen.getByLabelText("Вложения")).queryByText("a.txt")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Сообщение"), "q");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].messages[0].content).toBe("Файл: b.txt\n```\nдва\n```\n\nq");
  });

  it("says a file is not text when it is not UTF-8", async () => {
    const { user } = await open();
    await user.upload(picker(), new File([new Uint8Array([0xcf, 0xf0, 0xe8, 0xe2, 0xe5, 0xf2])], "old.txt"));
    expect(await screen.findByRole("alert")).toHaveTextContent("old.txt: Файл не похож на текст");
    expect(screen.queryByLabelText("Вложения")).not.toBeInTheDocument();
  });

  it("refuses a file over 512 KB and a type that is not on the list", async () => {
    const { user } = await open();
    await user.upload(picker(), new File([new Uint8Array(512 * 1024 + 1).fill(0x61)], "huge.log"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Файл «huge.log» больше 512 КБ");
    // fireEvent: userEvent.upload honours the `accept` filter and would drop the pdf by itself
    fireEvent.change(picker(), { target: { files: [new File(["%PDF"], "doc.pdf", { type: "application/pdf" })] } });
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Тип файла «doc.pdf» не поддерживается"));
  });

  it("takes at most five text files", async () => {
    const { user } = await open();
    await user.upload(
      picker(),
      Array.from({ length: 6 }, (_, i) => textFile(`f${i}.txt`, String(i))),
    );
    const tray = await screen.findByLabelText("Вложения");
    expect(within(tray).getAllByRole("button", { name: /^Убрать / })).toHaveLength(5);
    expect(screen.getByRole("alert")).toHaveTextContent("Не больше 5 текстовых файлов в сообщении");
  });

  it("takes files dropped on the composer", async () => {
    await open();
    const zone = screen.getByLabelText("Сообщение").closest(".dropzone") as HTMLElement;
    fireEvent.drop(zone, { dataTransfer: { files: [textFile("dropped.md", "# Привет")] } });
    const tray = await screen.findByLabelText("Вложения");
    expect(within(tray).getByText("dropped.md")).toBeInTheDocument();
  });

  it("gives the file back to the composer when the model does not answer", async () => {
    server.use(http.post("/api/playground/chat", () => HttpResponse.json({ detail: "Шлюз моделей недоступен" }, { status: 503 })));
    const { user } = await open();
    await user.upload(picker(), textFile("a.txt", "x"));
    await user.type(screen.getByLabelText("Сообщение"), "вопрос");
    await user.click(await screen.findByRole("button", { name: "Отправить" }));
    await screen.findAllByRole("alert");
    expect(within(await screen.findByLabelText("Вложения")).getByText("a.txt")).toBeInTheDocument();
    expect(screen.getByLabelText("Сообщение")).toHaveValue("вопрос");
  });
});

describe("playground: size of a message", () => {
  it("sends five files of 500 KB (the portal takes up to 2 700 000 characters in a message)", async () => {
    const sent = captureChat();
    const { user } = await open();
    const body = "a".repeat(500 * 1024);
    await user.upload(
      picker(),
      Array.from({ length: 5 }, (_, i) => textFile(`part${i}.txt`, body)),
    );
    await screen.findByLabelText("Вложения");
    await user.type(screen.getByLabelText("Сообщение"), "q");
    const send = screen.getByRole("button", { name: "Отправить" });
    expect(send).toBeEnabled();
    await user.click(send);
    await waitFor(() => expect(sent).toHaveLength(1));
    const content = sent[0].messages[0].content as string;
    expect(content.length).toBeGreaterThan(2_560_000);
    expect(content.length).toBeLessThanOrEqual(2_700_000);
  }, 30000);

  it("stops a message the portal would refuse and says why", async () => {
    const sent = captureChat();
    const { user } = await open();
    await user.upload(
      picker(),
      Array.from({ length: 5 }, (_, i) => textFile(`part${i}.txt`, "a".repeat(512 * 1024))),
    );
    await screen.findByLabelText("Вложения");
    fireEvent.change(screen.getByLabelText("Сообщение"), { target: { value: "b".repeat(100_000) } });
    expect(await screen.findByRole("alert")).toHaveTextContent(/Сообщение длиннее 2.700.000 символов/);
    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
    expect(sent).toHaveLength(0);
  }, 30000);
});

describe("playground: the composer does not move", () => {
  it("the attachments take the place of the hint in the bar, not a new row", async () => {
    await open();
    const bar = document.querySelector(".composer .bar") as HTMLElement;
    expect(within(bar).getByText("отправляет сообщение")).toBeInTheDocument();
    const { user } = { user: (await import("@testing-library/user-event")).default.setup() };
    await user.upload(picker(), textFile("a.txt", "x"));
    const tray = await screen.findByLabelText("Вложения");
    expect(bar).toContainElement(tray);
    expect(within(bar).queryByText("отправляет сообщение")).not.toBeInTheDocument();
    await user.click(within(tray).getByRole("button", { name: "Убрать a.txt" }));
    expect(within(bar).getByText("отправляет сообщение")).toBeInTheDocument();
  });

  it("a dragged text is left to the textarea, only files are taken by the drop zone", async () => {
    await open();
    const zone = screen.getByLabelText("Сообщение").closest(".dropzone") as HTMLElement;
    const over = fireEvent.dragOver(zone, { dataTransfer: { types: ["text/plain"], files: [] } });
    expect(over).toBe(true); // not default-prevented
    expect(fireEvent.dragOver(zone, { dataTransfer: { types: ["Files"], files: [] } })).toBe(false);
  });

  it("the picture in the thread has its box set before it is decoded", async () => {
    stubImages(1000, 500);
    captureChat();
    const { user } = await open();
    await user.upload(picker(), imageFile());
    await user.click(await screen.findByRole("button", { name: "Отправить" }));
    const img = await screen.findByAltText("Изображение 1 в сообщении");
    expect(img).toHaveAttribute("width", "224");
    expect(img).toHaveAttribute("height", "112");
  });
});

describe("playground: context warning", () => {
  it("warns that a large file does not fit the context, with the estimate and the model's limit", async () => {
    const { user } = await open(); // gemma-4-e4b: 8192 tokens per request
    expect(screen.queryByText(/не поместится в контекст/)).not.toBeInTheDocument();
    await user.upload(picker(), textFile("big.txt", "а".repeat(70_000)));
    // 70 000 characters of the file plus its frame: 70 022 characters, 3,5 per token
    expect(await screen.findByText(/^Похоже, не поместится в контекст модели \(~20.007 токенов из 8.192\)$/)).toBeInTheDocument();
  });

  it("takes the history into account and goes away when the file is removed", async () => {
    const { user } = await open();
    await user.upload(picker(), textFile("big.txt", "а".repeat(70_000)));
    await screen.findByText(/не поместится в контекст/);
    await user.click(screen.getByRole("button", { name: "Убрать big.txt" }));
    await waitFor(() => expect(screen.queryByText(/не поместится в контекст/)).not.toBeInTheDocument());
  });

  it("does not block sending", async () => {
    const sent = captureChat();
    const { user } = await open();
    await user.upload(picker(), textFile("big.txt", "а".repeat(70_000)));
    await screen.findByText(/не поместится в контекст/);
    expect(screen.getByRole("button", { name: "Отправить" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(sent).toHaveLength(1));
  });
});

describe("playground: images", () => {
  it("downscales a picture, shows a thumbnail and sends it as an image_url part", async () => {
    stubImages();
    const sent = captureChat();
    const { user } = await open();
    await user.upload(picker(), imageFile("cat.png"));
    const tray = await screen.findByLabelText("Вложения");
    expect(within(tray).getByAltText("cat.png")).toHaveAttribute("src", JPEG);
    await user.type(screen.getByLabelText("Сообщение"), "Что на фото?");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].messages[0].content).toEqual([
      { type: "text", text: "Что на фото?" },
      { type: "image_url", image_url: { url: JPEG } },
    ]);
    // the history shows the thumbnail
    await screen.findByText("ок");
    expect(screen.getByAltText("Изображение 1 в сообщении")).toHaveAttribute("src", JPEG);
    expect(screen.queryByLabelText("Вложения")).not.toBeInTheDocument();
  });

  it("keeps an earlier picture in the history that is sent with the next question", async () => {
    stubImages();
    const sent = captureChat();
    const { user } = await open();
    await user.upload(picker(), imageFile());
    await user.type(await screen.findByLabelText("Сообщение"), "первый");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await screen.findByText("ок");
    await user.type(screen.getByLabelText("Сообщение"), "второй");
    await user.click(await screen.findByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[1].messages[0].content).toEqual([
      { type: "text", text: "первый" },
      { type: "image_url", image_url: { url: JPEG } },
    ]);
    expect(sent[1].messages[2].content).toBe("второй");
  });

  it("takes at most four pictures in one message", async () => {
    stubImages();
    const { user } = await open();
    await user.upload(
      picker(),
      Array.from({ length: 5 }, (_, i) => imageFile(`p${i}.png`)),
    );
    const tray = await screen.findByLabelText("Вложения");
    expect(within(tray).getAllByRole("img")).toHaveLength(4);
    expect(screen.getByRole("alert")).toHaveTextContent("Не больше 4 изображений в сообщении");
  });

  it("removes a picture before sending", async () => {
    stubImages();
    const { user } = await open();
    await user.upload(picker(), imageFile("cat.png"));
    await user.click(await screen.findByRole("button", { name: "Убрать cat.png" }));
    expect(screen.queryByLabelText("Вложения")).not.toBeInTheDocument();
  });

  it("takes a picture pasted from the clipboard", async () => {
    stubImages();
    await open();
    fireEvent.paste(screen.getByLabelText("Сообщение"), { clipboardData: { files: [imageFile("clip.png")], getData: () => "" } });
    const tray = await screen.findByLabelText("Вложения");
    expect(within(tray).getByAltText("clip.png")).toBeInTheDocument();
  });

  it("takes a picture dropped on the composer", async () => {
    stubImages();
    await open();
    const zone = screen.getByLabelText("Сообщение").closest(".dropzone") as HTMLElement;
    fireEvent.drop(zone, { dataTransfer: { files: [imageFile("dropped.webp", "image/webp")] } });
    expect(within(await screen.findByLabelText("Вложения")).getByAltText("dropped.webp")).toBeInTheDocument();
  });

  it("a model without vision refuses pictures with the portal's wording and keeps them out of the tray", async () => {
    stubImages();
    await open("qwen3-30b-a3b");
    fireEvent.drop(screen.getByLabelText("Сообщение").closest(".dropzone") as HTMLElement, {
      dataTransfer: { files: [imageFile()] },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Модель не принимает изображения");
    expect(screen.queryByLabelText("Вложения")).not.toBeInTheDocument();
    // a pasted picture is refused the same way
    fireEvent.paste(screen.getByLabelText("Сообщение"), { clipboardData: { files: [imageFile()], getData: () => "" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("Модель не принимает изображения");
  });

  it("drops the pictures already attached when the model is changed to one without vision", async () => {
    stubImages();
    const { user } = await open();
    await user.upload(picker(), imageFile("cat.png"));
    await screen.findByLabelText("Вложения");
    await user.selectOptions(screen.getByLabelText("Модель"), "qwen3-30b-a3b");
    await waitFor(() => expect(screen.queryByLabelText("Вложения")).not.toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent("Изображения убраны: эта модель их не принимает");
  });

  it("reports a picture the browser cannot read", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn(async () => Promise.reject(new Error("bad"))));
    const { user } = await open();
    await user.upload(picker(), imageFile("broken.png"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Изображение «broken.png» не удалось прочитать");
  });
});
