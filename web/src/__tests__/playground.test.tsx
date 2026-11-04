import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

describe("playground", () => {
  it("fills the model select from the catalog", async () => {
    renderApp("/playground");
    const select = await screen.findByLabelText("Модель");
    await waitFor(() => expect(within(select).getAllByRole("option").length).toBe(3));
    expect(within(select).getByRole("option", { name: "gemma-4-e4b" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "qwen3-30b-a3b" })).toBeInTheDocument();
  });

  it("sends model and messages with CSRF and shows streamed chunks as they arrive", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    let sent: { csrf: string | null; body: unknown } | null = null;
    const enc = new TextEncoder();
    const chunk = (text: string) =>
      controller.enqueue(enc.encode(`data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\n`));
    server.use(
      http.post("/api/playground/chat", async ({ request }) => {
        sent = { csrf: request.headers.get("X-CSRF-Token"), body: await request.json() };
        const stream = new ReadableStream<Uint8Array>({ start: (c) => void (controller = c) });
        return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
      }),
    );
    const { user } = renderApp("/playground");
    await waitFor(() => expect(within(screen.getByLabelText("Модель")).getAllByRole("option").length).toBe(3));

    await user.type(screen.getByLabelText("Сообщение"), "Привет");
    await user.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => expect(sent).not.toBeNull());
    expect(sent).toEqual({
      csrf: "csrf-test-token-user",
      body: { model: "gemma-4-e4b", messages: [{ role: "user", content: "Привет" }], max_tokens: 1024 },
    });
    expect(screen.getByRole("button", { name: "Остановить" })).toBeInTheDocument();

    chunk("Здравствуйте");
    expect(await screen.findByText("Здравствуйте")).toBeInTheDocument();
    chunk(", чем помочь");
    expect(await screen.findByText("Здравствуйте, чем помочь")).toBeInTheDocument();
    controller.enqueue(enc.encode("data: [DONE]\n\n"));
    controller.close();

    expect(await screen.findByRole("button", { name: "Отправить" })).toBeInTheDocument();
    expect(screen.getByText("Привет")).toBeInTheDocument();
    expect(screen.getByLabelText("Сообщение")).toHaveValue("");
  });

  it("keeps the history for the next request", async () => {
    const bodies: { messages: unknown[] }[] = [];
    server.use(
      http.post("/api/playground/chat", async ({ request }) => {
        bodies.push((await request.json()) as { messages: unknown[] });
        const enc = new TextEncoder();
        const text = `data: ${JSON.stringify({ choices: [{ delta: { content: "ответ" } }] })}\n\ndata: [DONE]\n\n`;
        return new HttpResponse(enc.encode(text), { headers: { "Content-Type": "text/event-stream" } });
      }),
    );
    const { user } = renderApp("/playground");
    await waitFor(() => expect(within(screen.getByLabelText("Модель")).getAllByRole("option").length).toBe(3));
    await user.type(screen.getByLabelText("Сообщение"), "первый");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(bodies.length).toBe(1));
    await screen.findByRole("button", { name: "Отправить" });
    await user.type(screen.getByLabelText("Сообщение"), "второй");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(bodies.length).toBe(2));
    expect(bodies[1].messages).toEqual([
      { role: "user", content: "первый" },
      { role: "assistant", content: "ответ" },
      { role: "user", content: "второй" },
    ]);
  });

  it("shows the gateway error on 503", async () => {
    server.use(
      http.post("/api/playground/chat", () => HttpResponse.json({ detail: "Шлюз моделей недоступен" }, { status: 503 })),
    );
    const { user } = renderApp("/playground");
    await waitFor(() => expect(within(screen.getByLabelText("Модель")).getAllByRole("option").length).toBe(3));
    await user.type(screen.getByLabelText("Сообщение"), "Привет");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Шлюз моделей недоступен");
    expect(screen.queryByRole("button", { name: "Остановить" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отправить" })).toBeInTheDocument();
    // nothing was answered: no orphan user turn, the text is back in the composer
    expect(screen.queryByText("Вы")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Сообщение")).toHaveValue("Привет");
  });

  it("a retry after an error sends one user turn, not two", async () => {
    const bodies: { messages: { role: string; content: string }[] }[] = [];
    let calls = 0;
    server.use(
      http.post("/api/playground/chat", async ({ request }) => {
        calls += 1;
        bodies.push((await request.json()) as { messages: { role: string; content: string }[] });
        if (calls === 1) return HttpResponse.json({ detail: "Шлюз моделей недоступен" }, { status: 503 });
        const text = `data: ${JSON.stringify({ choices: [{ delta: { content: "ок" } }] })}\n\ndata: [DONE]\n\n`;
        return new HttpResponse(new TextEncoder().encode(text), { headers: { "Content-Type": "text/event-stream" } });
      }),
    );
    const { user } = renderApp("/playground");
    await waitFor(() => expect(within(screen.getByLabelText("Модель")).getAllByRole("option").length).toBe(3));
    await user.type(screen.getByLabelText("Сообщение"), "вопрос");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await waitFor(() => expect(bodies.length).toBe(2));
    expect(bodies[1].messages).toEqual([{ role: "user", content: "вопрос" }]);
    expect(await screen.findByText("ок")).toBeInTheDocument();
  });

  it("stopping before the first token rolls the turn back and restores the draft", async () => {
    server.use(
      http.post("/api/playground/chat", () => {
        const stream = new ReadableStream<Uint8Array>({ start: () => undefined });
        return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
      }),
    );
    const { user } = renderApp("/playground");
    await waitFor(() => expect(within(screen.getByLabelText("Модель")).getAllByRole("option").length).toBe(3));
    await user.type(screen.getByLabelText("Сообщение"), "долго думать");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    await user.click(await screen.findByRole("button", { name: "Остановить" }));
    expect(await screen.findByRole("button", { name: "Отправить" })).toBeInTheDocument();
    expect(screen.queryByText("Вы")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Сообщение")).toHaveValue("долго думать");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("an empty successful stream is reported and rolled back", async () => {
    server.use(
      http.post("/api/playground/chat", () =>
        new HttpResponse(new TextEncoder().encode("data: [DONE]\n\n"), { headers: { "Content-Type": "text/event-stream" } }),
      ),
    );
    const { user } = renderApp("/playground");
    await waitFor(() => expect(within(screen.getByLabelText("Модель")).getAllByRole("option").length).toBe(3));
    await user.type(screen.getByLabelText("Сообщение"), "пусто");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Модель вернула пустой ответ");
    expect(screen.queryByText("Вы")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Сообщение")).toHaveValue("пусто");
  });

  it("keeps a partial answer when the stream breaks after the first token", async () => {
    server.use(
      http.post("/api/playground/chat", () => {
        const enc = new TextEncoder();
        const stream = new ReadableStream<Uint8Array>({
          start(c) {
            c.enqueue(enc.encode(`data: ${JSON.stringify({ choices: [{ delta: { content: "начало" } }] })}\n\n`));
            c.enqueue(enc.encode(`data: ${JSON.stringify({ error: { message: "Сбой модели" } })}\n\n`));
            c.close();
          },
        });
        return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
      }),
    );
    const { user } = renderApp("/playground");
    await waitFor(() => expect(within(screen.getByLabelText("Модель")).getAllByRole("option").length).toBe(3));
    await user.type(screen.getByLabelText("Сообщение"), "вопрос");
    await user.click(screen.getByRole("button", { name: "Отправить" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Сбой модели");
    expect(screen.getByText("начало")).toBeInTheDocument();
    expect(screen.getByText("вопрос")).toBeInTheDocument();
    expect(screen.getByLabelText("Сообщение")).toHaveValue("");
  });

  it("does not send an empty message", async () => {
    renderApp("/playground");
    await waitFor(() => expect(within(screen.getByLabelText("Модель")).getAllByRole("option").length).toBe(3));
    expect(screen.getByRole("button", { name: "Отправить" })).toBeDisabled();
  });
});
