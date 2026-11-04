import { screen } from "@testing-library/react";
import { renderApp } from "../test/render";

const BASE = () => `https://${window.location.host}/v1`;

describe("quickstart", () => {
  it("shows the base URL and a curl snippet for the first catalog model", async () => {
    renderApp("/quickstart");
    expect(await screen.findByText(BASE())).toBeInTheDocument();
    const code = await screen.findByTestId("snippet");
    expect(code).toHaveTextContent(`${BASE()}/chat/completions`);
    expect(code).toHaveTextContent('"model": "gemma-4-e4b"');
    expect(code).toHaveTextContent("Authorization: Bearer $LLM_API_KEY");
  });

  it("switches snippets by language and by chosen model", async () => {
    const { user } = renderApp("/quickstart");
    await screen.findByTestId("snippet");
    await user.selectOptions(screen.getByLabelText("Модель"), "qwen3-30b-a3b");

    await user.click(screen.getByRole("tab", { name: "Python" }));
    let code = screen.getByTestId("snippet");
    expect(code).toHaveTextContent(`base_url="${BASE()}"`);
    expect(code).toHaveTextContent('model="qwen3-30b-a3b"');
    expect(code).toHaveTextContent("from openai import OpenAI");

    await user.click(screen.getByRole("tab", { name: "JavaScript" }));
    code = screen.getByTestId("snippet");
    expect(code).toHaveTextContent(`baseURL: "${BASE()}"`);
    expect(code).toHaveTextContent('model: "qwen3-30b-a3b"');
  });

  it("has a PowerShell example with curl.exe, backticks and $env", async () => {
    const { user } = renderApp("/quickstart");
    await screen.findByTestId("snippet");
    await user.selectOptions(screen.getByLabelText("Модель"), "qwen3-30b-a3b");
    await user.click(screen.getByRole("tab", { name: "PowerShell" }));
    const code = screen.getByTestId("snippet");
    expect(code).toHaveTextContent(`curl.exe ${BASE()}/chat/completions`);
    expect(code).toHaveTextContent("`");
    expect(code).toHaveTextContent("Authorization: Bearer $env:LLM_API_KEY");
    expect(code).toHaveTextContent('"model": "qwen3-30b-a3b"');
    expect(code.textContent).not.toContain("\\\n");
    expect(code.textContent).not.toContain("$LLM_API_KEY");
  });

  it("links the CA certificate for clients that do not trust the cluster", async () => {
    renderApp("/quickstart");
    await screen.findByTestId("snippet");
    expect(screen.getByRole("link", { name: "ca.crt" })).toHaveAttribute("href", "/ca.crt");
  });

  it("copies the snippet", async () => {
    const { user } = renderApp("/quickstart");
    await screen.findByTestId("snippet");
    await user.click(screen.getByRole("button", { name: "Копировать пример" }));
    const copied = await navigator.clipboard.readText();
    expect(copied).toContain(`curl ${BASE()}/chat/completions`);
    expect(copied).toContain("gemma-4-e4b");
  });

  it("copies the base URL", async () => {
    const { user } = renderApp("/quickstart");
    await screen.findByText(BASE());
    await user.click(screen.getByRole("button", { name: "Копировать адрес" }));
    expect(await navigator.clipboard.readText()).toBe(BASE());
  });
});
