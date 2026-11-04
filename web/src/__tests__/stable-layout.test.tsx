import { screen, within } from "@testing-library/react";
import { delay, http, HttpResponse } from "msw";
import { fixtures } from "../mocks/handlers";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

const gguf = (name: string) => new File([new Uint8Array(4)], name);

describe("file picker on «Мои модели»", () => {
  it("says no file is chosen, shows one name, then the first name and how many more", async () => {
    const { user } = renderApp("/admin/local-models", "admin");
    await screen.findByRole("table", { name: "Мои модели" });
    const form = screen.getByRole("form", { name: "Загрузка файла" });
    const input = screen.getByLabelText("Файлы GGUF");
    expect(within(form).getByText("Файлы не выбраны")).toBeInTheDocument();

    await user.upload(input, gguf("Alpha-Q4_K_M.gguf"));
    expect(within(form).getByText("Alpha-Q4_K_M.gguf")).toBeInTheDocument();
    expect(within(form).queryByText("Файлы не выбраны")).not.toBeInTheDocument();

    await user.upload(input, [gguf("Beta-00001-of-00003.gguf"), gguf("Beta-00002-of-00003.gguf"), gguf("Beta-00003-of-00003.gguf")]);
    expect(within(form).getByText("Beta-00001-of-00003.gguf и ещё 2")).toBeInTheDocument();
  });
});

describe("node page while loading", () => {
  it("shows the join commands only together with the table", async () => {
    server.use(
      http.get("/api/admin/deployments", async () => {
        await delay(250);
        return HttpResponse.json(fixtures.deployments);
      }),
    );
    renderApp("/admin/nodes", "admin");
    expect(await screen.findByRole("status")).toHaveTextContent("Загрузка");
    await new Promise((r) => setTimeout(r, 80));
    expect(screen.queryByRole("table", { name: "Ноды" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Подключение ноды" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("join-linux")).not.toBeInTheDocument();

    expect(await screen.findByRole("table", { name: "Ноды" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Подключение ноды" })).toBeInTheDocument();
    expect(await screen.findByTestId("join-linux")).toBeInTheDocument();
  });

  it("explains the «Ёмкость» column in a visible note under the table", async () => {
    renderApp("/admin/nodes", "admin");
    await screen.findByRole("table", { name: "Ноды" });
    expect(screen.getByText("Ёмкость: сколько памяти ноды остаётся под модель")).toBeVisible();
  });
});

describe("login screen while the status loads", () => {
  it("keeps three placeholder rows and an announced «Загрузка»", async () => {
    server.use(
      http.get("/api/public/status", async () => {
        await delay(250);
        return HttpResponse.json({ models: [] });
      }),
    );
    const { container } = renderApp("/", null);
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Загрузка");
    const placeholder = container.querySelector("ul.status-list.placeholder");
    expect(placeholder).not.toBeNull();
    expect(placeholder).toHaveAttribute("aria-hidden", "true");
    expect(placeholder!.querySelectorAll("li")).toHaveLength(3);
    expect(screen.queryByRole("list", { name: "Статус моделей" })).not.toBeInTheDocument();
  });
});
