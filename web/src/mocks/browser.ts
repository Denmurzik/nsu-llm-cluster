import { setupWorker } from "msw/browser";
import { handlers, state } from "./handlers";

/** `?as=user` or `?as=anon` in the URL switches the mocked session; admin is the default. */
const as = new URLSearchParams(window.location.search).get("as");
state.role = as === "user" ? "user" : as === "anon" ? null : "admin";

export const worker = setupWorker(...handlers);
