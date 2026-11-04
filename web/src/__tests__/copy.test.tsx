import { screen, waitFor } from "@testing-library/react";
import { renderApp, pageText } from "../test/render";

const EMOJI = /\p{Extended_Pictographic}|\p{Emoji_Presentation}|️/u;

const pages: { path: string; role: "user" | "admin" | null; ready: () => Promise<unknown> }[] = [
  { path: "/", role: null, ready: () => screen.findByRole("list", { name: "Статус моделей" }) },
  { path: "/models", role: "user", ready: () => screen.findByRole("row", { name: /gemma-4-e4b/ }) },
  { path: "/keys", role: "user", ready: () => screen.findByRole("row", { name: /курсовая/ }) },
  { path: "/quickstart", role: "user", ready: () => screen.findByTestId("snippet") },
  { path: "/playground", role: "user", ready: () => screen.findByLabelText("Сообщение") },
  { path: "/usage", role: "user", ready: () => screen.findByText("128 450") },
  { path: "/admin/deployments", role: "admin", ready: () => screen.findByRole("row", { name: /gemma-4-e4b/ }) },
  { path: "/admin/deployments/new", role: "admin", ready: () => screen.findByLabelText("Репозиторий Hugging Face") },
  { path: "/admin/deployments/1", role: "admin", ready: () => screen.findByRole("table", { name: "Поды развёртывания" }) },
  { path: "/admin/nodes", role: "admin", ready: () => screen.findByTestId("join-linux") },
  { path: "/admin/status", role: "admin", ready: () => screen.findByRole("table", { name: "Состояние нод" }) },
  { path: "/admin/users", role: "admin", ready: () => screen.findByRole("row", { name: /a\.petrova/ }) },
  { path: "/admin/nodes", role: "user", ready: () => screen.findByText("Недостаточно прав") },
];

describe("copy rules", () => {
  it.each(pages)("$path as $role has no exclamation marks and no emoji", async ({ path, role, ready }) => {
    renderApp(path, role);
    await ready();
    await waitFor(() => expect(screen.queryByText("Загрузка")).not.toBeInTheDocument());
    const text = pageText();
    expect(text).not.toContain("!");
    expect(text).not.toMatch(EMOJI);
  });

  it("the wizard estimate panel and the logs dialog keep the rules", async () => {
    const { user } = renderApp("/admin/deployments/new", "admin");
    await user.type(await screen.findByLabelText("Репозиторий Hugging Face"), "unsloth/gemma-4-E4B-it-GGUF");
    await user.click(screen.getByRole("button", { name: "Найти файлы" }));
    await user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("row", { name: /aud-101-pc02/ });
    expect(pageText()).not.toContain("!");
    expect(pageText()).not.toMatch(EMOJI);
  });

  it("short interface strings carry no trailing period", async () => {
    renderApp("/keys", "user");
    await screen.findByRole("row", { name: /курсовая/ });
    const labels = Array.from(document.querySelectorAll("button, label, h1, h2, h3, th, a, legend")).map(
      (el) => (el.textContent ?? "").trim(),
    );
    expect(labels.filter((t) => t.endsWith("."))).toEqual([]);
  });
});
