import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { fixtures } from "../mocks/handlers";
import { renderApp } from "../test/render";

describe("usage", () => {
  it("shows totals and a bar per day", async () => {
    renderApp("/usage");
    expect(await screen.findByText("128 450")).toBeInTheDocument(); // prompt
    expect(screen.getByText("96 210")).toBeInTheDocument(); // completion
    expect(screen.getByText("318")).toBeInTheDocument(); // requests
    const chart = screen.getByRole("img", { name: /Токены по дням/ });
    expect(within(chart).getAllByTestId("bar")).toHaveLength(7);
  });

  it("requests the chosen period", async () => {
    const seen: string[] = [];
    server.use(
      http.get("/api/usage", ({ request }) => {
        const days = new URL(request.url).searchParams.get("days") ?? "";
        seen.push(days);
        return HttpResponse.json({ ...fixtures.usage, days: Number(days) });
      }),
    );
    const { user } = renderApp("/usage");
    await screen.findByText("128 450");
    expect(seen[0]).toBe("7");
    await user.click(screen.getByRole("tab", { name: "30 дней" }));
    await waitFor(() => expect(seen).toContain("30"));
    await user.click(screen.getByRole("tab", { name: "День" }));
    await waitFor(() => expect(seen).toContain("1"));
  });

  it("explains an empty period", async () => {
    server.use(
      http.get("/api/usage", () =>
        HttpResponse.json({ days: 7, prompt_tokens: 0, completion_tokens: 0, requests: 0, by_day: [] }),
      ),
    );
    renderApp("/usage");
    expect(await screen.findByText("За выбранный период запросов не было")).toBeInTheDocument();
  });
});
