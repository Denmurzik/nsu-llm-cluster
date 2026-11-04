import { http, HttpResponse } from "msw";
import { api, ApiError, setCsrfToken } from "../api";
import { fixtures, state } from "../mocks/handlers";
import { server } from "../mocks/server";

describe("CSRF recovery", () => {
  it("re-fetches /api/me once and repeats the request when the token went stale", async () => {
    state.role = "user";
    setCsrfToken("stale-token");
    let me = 0;
    server.use(http.get("/api/me", () => (me++, HttpResponse.json(fixtures.meUser))));
    const created = await api.createKey("после-ротации", null);
    expect(created.alias).toBe("после-ротации");
    expect(me).toBe(1);
  });

  it("gives a Russian message instead of the raw 403 when the retry fails too", async () => {
    state.role = "user";
    let posts = 0;
    server.use(
      http.post("/api/keys", () => {
        posts += 1;
        return HttpResponse.json({ detail: "CSRF" }, { status: 403 });
      }),
    );
    const error = await api.createKey("x", null).catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(403);
    expect(error.detail).toBe("Сессия устарела. Обновите страницу и повторите действие");
    expect(posts).toBe(2);
  });

  it("gives the same message when the session itself is gone", async () => {
    state.role = "user";
    setCsrfToken("stale-token");
    server.use(http.get("/api/me", () => HttpResponse.json({ detail: "Требуется вход" }, { status: 401 })));
    const error = await api.createKey("x", null).catch((e) => e);
    expect(error.detail).toBe("Сессия устарела. Обновите страницу и повторите действие");
  });

  it("does not touch other 403 answers", async () => {
    state.role = "user";
    let me = 0;
    server.use(
      http.post("/api/keys", () => HttpResponse.json({ detail: "Недостаточно прав" }, { status: 403 })),
      http.get("/api/me", () => (me++, HttpResponse.json(fixtures.meUser))),
    );
    const error = await api.createKey("x", null).catch((e) => e);
    expect(error.detail).toBe("Недостаточно прав");
    expect(me).toBe(0);
  });
});
