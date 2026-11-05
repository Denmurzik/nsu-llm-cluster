import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { server } from "../mocks/server";
import { fixtures, state } from "../mocks/handlers";
import { renderApp } from "../test/render";

const MURUEV = "d.muruev@g.nsu.ru";
const PETROVA = "a.petrova@g.nsu.ru";
const SIDOROV = "i.sidorov@g.nsu.ru";

async function openUsers() {
  const view = renderApp("/admin/users", "admin");
  await screen.findByRole("row", { name: new RegExp(PETROVA.replace(".", "\\.")) });
  return view;
}

const rowOf = (email: string) => screen.getByRole("row", { name: new RegExp(email.replace(/\./g, "\\.")) });
const menuOf = (email: string) => within(rowOf(email)).getByRole("button", { name: `Действия: ${email}` });
const stored = (email: string) => state.users.find((u) => u.email === email)!;

async function pick(user: ReturnType<typeof renderApp>["user"], email: string, item: string) {
  await user.click(menuOf(email));
  await user.click(screen.getByRole("menuitem", { name: item }));
}

describe("admin users: the table", () => {
  it("shows role, keys, limits, usage and the block", async () => {
    await openUsers();
    const petrova = rowOf(PETROVA);
    expect(within(petrova).getByText("Пользователь")).toBeInTheDocument();
    expect(within(petrova).getByText("2 из 3")).toBeInTheDocument(); // the user's own key limit
    expect(within(petrova).getByText(/30 запр\.\/мин, 50\s000 ток\.\/мин/)).toBeInTheDocument();
    expect(within(petrova).getByText("238 190")).toBeInTheDocument();

    const boss = rowOf(MURUEV);
    expect(within(boss).getByText("Администратор")).toBeInTheDocument();
    expect(within(boss).getByText("общие")).toBeInTheDocument();

    const banned = rowOf(SIDOROV);
    expect(within(banned).getByText("заблокирован")).toBeInTheDocument();
    expect(within(banned).getByText("Причина: Передал ключ посторонним")).toBeInTheDocument();
    expect(within(banned).getByText("назначена вручную")).toBeInTheDocument();
    expect(within(banned).getAllByText("—").length).toBeGreaterThan(0);
    expect(screen.getByText("Показано 1–3 из 3")).toBeInTheDocument();
  });

  it("keeps the first and the last login in separate columns", async () => {
    await openUsers();
    expect(screen.getByRole("columnheader", { name: "Последний вход" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Первый вход" })).toBeInTheDocument();
  });

  it("is for admins only", async () => {
    renderApp("/admin/users", "user");
    expect(await screen.findByText("Недостаточно прав")).toBeInTheDocument();
  });

  it("searches by name or e-mail and filters by role and access", async () => {
    const { user } = await openUsers();
    await user.type(screen.getByRole("searchbox", { name: "Поиск" }), "петрова");
    await waitFor(() => {
      expect(screen.queryByRole("row", { name: /sidorov/ })).not.toBeInTheDocument();
      expect(rowOf(PETROVA)).toBeInTheDocument();
    });
    expect(screen.getByText("Показано 1–1 из 1")).toBeInTheDocument();

    await user.clear(screen.getByRole("searchbox", { name: "Поиск" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Роль" }), "admin");
    await waitFor(() => {
      expect(screen.queryByRole("row", { name: /petrova/ })).not.toBeInTheDocument();
      expect(rowOf(MURUEV)).toBeInTheDocument();
    });

    await user.selectOptions(screen.getByRole("combobox", { name: "Роль" }), "");
    await user.selectOptions(screen.getByRole("combobox", { name: "Доступ" }), "blocked");
    await waitFor(() => {
      expect(screen.queryByRole("row", { name: /petrova/ })).not.toBeInTheDocument();
      expect(rowOf(SIDOROV)).toBeInTheDocument();
    });

    await user.type(screen.getByRole("searchbox", { name: "Поиск" }), "никого");
    expect(await screen.findByText("Никого не нашли")).toBeInTheDocument();
    expect(screen.getByText("Нет записей")).toBeInTheDocument();
  });

  it("pages: the next page asks the portal for the next offset", async () => {
    const offsets: string[] = [];
    server.use(
      http.get("/api/admin/users", ({ request }) => {
        offsets.push(new URL(request.url).searchParams.get("offset") ?? "");
        return HttpResponse.json({ items: fixtures.users.items, total: 60 });
      }),
    );
    const { user } = await openUsers();
    expect(screen.getByText("Показано 1–3 из 60")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Назад" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Дальше" }));
    await waitFor(() => expect(offsets).toContain("25"));
    expect(await screen.findByText("Показано 26–28 из 60")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Назад" })).toBeEnabled();
  });

  it("keeps the pager and the table on screen while a filter reloads", async () => {
    const { user } = await openUsers();
    await user.selectOptions(screen.getByRole("combobox", { name: "Роль" }), "admin");
    // the old rows stay until the new ones arrive: no «Загрузка» in their place
    expect(screen.queryByText("Загрузка")).not.toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Пользователи" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Дальше" })).toBeInTheDocument();
  });
});

describe("admin users: the row menu", () => {
  it("offers what fits the user and never blocks or demotes oneself", async () => {
    const { user } = await openUsers();
    const names = async (email: string) => {
      await user.click(menuOf(email));
      const items = within(screen.getByRole("menu")).getAllByRole("menuitem").map((i) => i.textContent);
      await user.keyboard("{Escape}");
      return items;
    };
    expect(await names(MURUEV)).toEqual(["Лимиты…", "Ключи"]);
    expect(await names(PETROVA)).toEqual(["Сделать администратором", "Заблокировать…", "Лимиты…", "Ключи"]);
    expect(await names(SIDOROV)).toEqual([
      "Сделать администратором",
      "Вернуть роль по списку ADMIN_EMAILS",
      "Разблокировать",
      "Лимиты…",
      "Ключи",
    ]);
  });

  it("makes an administrator after a confirmation, and says the role is manual", async () => {
    const { user } = await openUsers();
    await pick(user, PETROVA, "Сделать администратором");
    const dialog = screen.getByRole("dialog", { name: "Назначение администратором" });
    await user.click(within(dialog).getByRole("button", { name: "Отмена" }));
    expect(stored(PETROVA).role).toBe("user"); // nothing was sent

    await pick(user, PETROVA, "Сделать администратором");
    await user.click(
      within(screen.getByRole("dialog", { name: "Назначение администратором" })).getByRole("button", {
        name: "Сделать администратором",
      }),
    );
    await waitFor(() => expect(within(rowOf(PETROVA)).getByText("Администратор")).toBeInTheDocument());
    expect(within(rowOf(PETROVA)).getByText("назначена вручную")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(stored(PETROVA)).toMatchObject({ role: "admin", role_source: "manual" });
  });

  it("gives the role back to the ADMIN_EMAILS list", async () => {
    const { user } = await openUsers();
    await pick(user, SIDOROV, "Вернуть роль по списку ADMIN_EMAILS");
    await user.click(
      within(screen.getByRole("dialog", { name: "Роль по списку" })).getByRole("button", { name: "Вернуть по списку" }),
    );
    await waitFor(() => expect(within(rowOf(SIDOROV)).queryByText("назначена вручную")).not.toBeInTheDocument());
    expect(stored(SIDOROV).role_source).toBe("auto");
  });

  it("blocks with a reason and unblocks at once", async () => {
    const { user } = await openUsers();
    await pick(user, PETROVA, "Заблокировать…");
    const dialog = screen.getByRole("dialog", { name: "Блокировка пользователя" });
    expect(within(dialog).getByText(/ключи API перестанут работать/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Причина"), "Спам в чате");
    await user.click(within(dialog).getByRole("button", { name: "Заблокировать" }));
    await waitFor(() => expect(within(rowOf(PETROVA)).getByText("заблокирован")).toBeInTheDocument());
    expect(within(rowOf(PETROVA)).getByText("Причина: Спам в чате")).toBeInTheDocument();
    expect(stored(PETROVA)).toMatchObject({ blocked: true, blocked_reason: "Спам в чате" });

    await pick(user, PETROVA, "Разблокировать");
    await waitFor(() => expect(within(rowOf(PETROVA)).queryByText("заблокирован")).not.toBeInTheDocument());
    expect(stored(PETROVA)).toMatchObject({ blocked: false, blocked_reason: null });
  });

  it("blocks without a reason too", async () => {
    const { user } = await openUsers();
    await pick(user, PETROVA, "Заблокировать…");
    await user.click(screen.getByRole("button", { name: "Заблокировать" }));
    await waitFor(() => expect(within(rowOf(PETROVA)).getByText("заблокирован")).toBeInTheDocument());
    expect(within(rowOf(PETROVA)).queryByText(/Причина/)).not.toBeInTheDocument();
  });

  it("shows the refusal of the portal in the dialog and keeps it open", async () => {
    server.use(http.patch("/api/admin/users/:id", () => HttpResponse.json({ detail: "Шлюз моделей недоступен" }, { status: 503 })));
    const { user } = await openUsers();
    await pick(user, PETROVA, "Заблокировать…");
    await user.click(screen.getByRole("button", { name: "Заблокировать" }));
    const dialog = screen.getByRole("dialog", { name: "Блокировка пользователя" });
    expect(await within(dialog).findByText("Шлюз моделей недоступен")).toBeInTheDocument();
    expect(within(rowOf(PETROVA)).queryByText("заблокирован")).not.toBeInTheDocument();
  });

  it("shows a refused role change above the table", async () => {
    server.use(
      http.patch("/api/admin/users/:id", () =>
        HttpResponse.json({ detail: "Это последний администратор: сначала назначьте другого" }, { status: 409 }),
      ),
    );
    const { user } = await openUsers();
    await pick(user, PETROVA, "Сделать администратором");
    await user.click(screen.getByRole("button", { name: "Сделать администратором" }));
    expect(await screen.findByText("Это последний администратор: сначала назначьте другого")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("admin users: limits", () => {
  it("starts from the user's values, sets numbers and clears with an empty field", async () => {
    const { user } = await openUsers();
    await pick(user, PETROVA, "Лимиты…");
    const dialog = screen.getByRole("dialog", { name: "Лимиты пользователя" });
    const rpm = within(dialog).getByLabelText("Запросов в минуту на ключ");
    const keys = within(dialog).getByLabelText("Сколько ключей можно создать");
    expect(rpm).toHaveValue("30");
    expect(within(dialog).getByLabelText("Токенов в минуту на ключ")).toHaveValue("50000");
    expect(keys).toHaveValue("3");

    await user.clear(rpm);
    await user.type(rpm, "10");
    await user.clear(keys);
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(stored(PETROVA)).toMatchObject({ rpm_limit: 10, tpm_limit: 50000, max_keys: null });
    expect(within(rowOf(PETROVA)).getByText(/10 запр\.\/мин/)).toBeInTheDocument();
  });

  it("refuses text and zero before it asks the portal", async () => {
    let calls = 0;
    server.use(
      http.patch("/api/admin/users/:id", () => {
        calls += 1;
        return HttpResponse.json({});
      }),
    );
    const { user } = await openUsers();
    await pick(user, MURUEV, "Лимиты…");
    const dialog = screen.getByRole("dialog", { name: "Лимиты пользователя" });
    await user.type(within(dialog).getByLabelText("Токенов в минуту на ключ"), "0");
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));
    expect(
      await within(dialog).findByText("«Токенов в минуту на ключ»: нужно целое число больше нуля или пустое поле"),
    ).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText("Токенов в минуту на ключ"));
    await user.type(within(dialog).getByLabelText("Запросов в минуту на ключ"), "abc");
    await user.click(within(dialog).getByRole("button", { name: "Сохранить" }));
    expect(await within(dialog).findByText(/«Запросов в минуту на ключ»/)).toBeInTheDocument();
    expect(calls).toBe(0);
  });
});

describe("admin users: keys", () => {
  it("lists the masked keys of a user and revokes one after a confirmation", async () => {
    const { user } = await openUsers();
    await pick(user, PETROVA, "Ключи");
    const dialog = await screen.findByRole("dialog", { name: `Ключи: ${PETROVA}` });
    const row = await within(dialog).findByRole("row", { name: /курсовая/ });
    expect(within(row).getByText("sk-…a1b2")).toBeInTheDocument();
    expect(within(row).getByText("182 400")).toBeInTheDocument();
    expect(dialog.textContent).not.toMatch(/sk-[A-Za-z0-9_-]{8,}/); // only the masked value

    await user.click(within(row).getByRole("button", { name: "Отозвать ключ курсовая" }));
    const confirm = screen.getByRole("dialog", { name: "Отзыв ключа" });
    expect(within(confirm).getByText(/Ключ перестанет работать сразу/)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Отмена" }));
    expect(await screen.findByRole("row", { name: /курсовая/ })).toBeInTheDocument(); // back to the list

    await user.click(screen.getByRole("button", { name: "Отозвать ключ курсовая" }));
    await user.click(within(screen.getByRole("dialog", { name: "Отзыв ключа" })).getByRole("button", { name: "Отозвать" }));
    expect(await screen.findByText("У пользователя нет ключей")).toBeInTheDocument();
    expect(state.userKeys[stored(PETROVA).id]).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Закрыть" }));
    await waitFor(() => expect(within(rowOf(PETROVA)).getByText("1 из 3")).toBeInTheDocument()); // the count follows
  });

  it("says so when the user has no keys, and when the usage is not available", async () => {
    server.use(
      http.get("/api/admin/users/:id/keys", ({ params }) =>
        HttpResponse.json(params.id === stored(PETROVA).id ? fixtures.userKeys : [], { headers: { "X-Usage-Unavailable": "1" } }),
      ),
    );
    const { user } = await openUsers();
    await pick(user, PETROVA, "Ключи");
    expect(await screen.findByText("Шлюз не отдал расход, поэтому токены и запросы не показаны.")).toBeInTheDocument();
    expect(screen.queryByText("182 400")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Закрыть" }));

    await pick(user, SIDOROV, "Ключи");
    expect(await screen.findByText("У пользователя нет ключей")).toBeInTheDocument();
  });

  it("shows why a revoke failed and keeps the key in the list", async () => {
    server.use(
      http.delete("/api/admin/users/:id/keys/:keyId", () => HttpResponse.json({ detail: "Шлюз моделей недоступен" }, { status: 503 })),
    );
    const { user } = await openUsers();
    await pick(user, PETROVA, "Ключи");
    await user.click(await screen.findByRole("button", { name: "Отозвать ключ курсовая" }));
    await user.click(within(screen.getByRole("dialog", { name: "Отзыв ключа" })).getByRole("button", { name: "Отозвать" }));
    expect(await screen.findByText("Шлюз моделей недоступен")).toBeInTheDocument();
    expect(screen.getByRole("row", { name: /курсовая/ })).toBeInTheDocument();
  });
});
