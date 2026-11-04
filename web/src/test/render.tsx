import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { App } from "../app";
import { state } from "../mocks/handlers";

export function renderApp(path = "/", role: "user" | "admin" | null = "user") {
  state.role = role;
  const user = userEvent.setup();
  const view = render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
  return { user, ...view };
}

/** All visible text plus text-bearing attributes, for copy rules. */
export function pageText(root: HTMLElement = document.body): string {
  const parts: string[] = [root.textContent ?? ""];
  root.querySelectorAll("[placeholder],[aria-label],[title],[alt]").forEach((el) => {
    for (const a of ["placeholder", "aria-label", "title", "alt"]) {
      const v = el.getAttribute(a);
      if (v) parts.push(v);
    }
  });
  return parts.join("\n");
}
