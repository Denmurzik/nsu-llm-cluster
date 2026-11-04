import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { checkRules } from "../lib/rules";
import { fixtures } from "../mocks/handlers";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

const REPO = "unsloth/gemma-4-E4B-it-GGUF";

async function splitWizard() {
  const view = renderApp("/admin/deployments/new", "admin");
  await view.user.type(await screen.findByLabelText("Репозиторий Hugging Face"), REPO);
  await view.user.click(screen.getByRole("button", { name: "Найти файлы" }));
  await view.user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
  await screen.findByRole("region", { name: "Оценка памяти" });
  await view.user.click(screen.getByRole("radio", { name: "Разделение по нодам" }));
  await view.user.selectOptions(screen.getByLabelText("Ускоритель"), "nvidia");
  return view;
}

describe("split over VRAM and RAM of the nodes", () => {
  it("shows the placement of the first group per node and warns about the speed", async () => {
    const calls: URLSearchParams[] = [];
    server.use(
      http.get("/api/admin/hf/estimate", ({ request }) => {
        const params = new URL(request.url).searchParams;
        calls.push(params);
        return HttpResponse.json(params.get("mode") === "split" ? fixtures.hfEstimateSplit : fixtures.hfEstimate);
      }),
    );
    await splitWizard();
    const placement = await screen.findByLabelText("Размещение по нодам");
    const leader = within(placement).getByRole("row", { name: /aud-204-gpu01/ });
    expect(within(leader).getByText("лидер")).toBeInTheDocument();
    expect(within(leader).getByText("20 / 9")).toBeInTheDocument();
    expect(within(leader).getByText("10,0 ГБ из 10,5 ГБ")).toBeInTheDocument();
    const last = within(placement).getByRole("row", { name: /aud-204-gpu02/ });
    expect(within(last).getByText("24 / 0")).toBeInTheDocument();
    expect(within(placement).getByText(/Часть слоёв ляжет в RAM нод \(2,2\sГБ\)/)).toBeInTheDocument();

    await waitFor(() => expect(calls.some((p) => p.get("mode") === "split")).toBe(true));
    const sent = calls[calls.length - 1];
    expect([sent.get("mode"), sent.get("use_node_ram"), sent.get("ram_budget_pct")]).toEqual(["split", "true", "70"]);
  });

  it("says when the model does not fit the group", async () => {
    server.use(
      http.get("/api/admin/hf/estimate", () =>
        HttpResponse.json({
          ...fixtures.hfEstimateSplit,
          placement: { ...fixtures.hfEstimateSplit.placement, fits: false, speed_warning: false },
        }),
      ),
    );
    await splitWizard();
    expect(
      await screen.findByText("Модель не помещается в память группы: увеличьте группу или долю RAM"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Часть слоёв ляжет в RAM нод/)).not.toBeInTheDocument();
  });

  it("posts the RAM settings and can leave the RAM out", async () => {
    let posted: Record<string, unknown> | null = null;
    const calls: URLSearchParams[] = [];
    server.use(
      http.get("/api/admin/hf/estimate", ({ request }) => {
        calls.push(new URL(request.url).searchParams);
        return HttpResponse.json(fixtures.hfEstimateSplit);
      }),
      http.post("/api/admin/deployments", async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...fixtures.deploymentDetail, id: 9 }, { status: 201 });
      }),
    );
    const { user } = await splitWizard();
    const share = screen.getByLabelText("Доля RAM ноды, %");
    expect(share).toHaveValue(70);
    await user.clear(share);
    await user.type(share, "95");
    expect(screen.getByText("Доля RAM ноды для модели от 10 до 90 %")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Создать развёртывание" })).toBeDisabled();
    await user.clear(share);
    await user.type(share, "60");
    await waitFor(() => expect(calls[calls.length - 1].get("ram_budget_pct")).toBe("60"));

    await user.click(screen.getByRole("checkbox", { name: "Использовать RAM нод, когда не хватает видеопамяти" }));
    expect(screen.queryByLabelText("Доля RAM ноды, %")).not.toBeInTheDocument();
    await waitFor(() => expect(calls[calls.length - 1].get("use_node_ram")).toBe("false"));

    await user.click(screen.getByRole("checkbox", { name: "Назначать свободные ноды автоматически" }));
    const submit = screen.getByRole("button", { name: "Создать развёртывание" });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({ mode: "split", accel: "nvidia", use_node_ram: false, ram_budget_pct: 60 });
  });

  it("cpu nodes always use their RAM: only the share is asked", async () => {
    const { user } = await splitWizard();
    await user.selectOptions(screen.getByLabelText("Ускоритель"), "cpu");
    expect(screen.queryByRole("checkbox", { name: /Использовать RAM нод/ })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Доля RAM ноды, %")).toBeInTheDocument();
  });

  it("the edit page changes the RAM share through PATCH and warns about the restart", async () => {
    let body: unknown = null;
    server.use(
      http.patch("/api/admin/deployments/:id", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ ...fixtures.deploymentDetail, ...fixtures.deployments[1] });
      }),
    );
    const { user } = renderApp("/admin/deployments/2", "admin");
    const share = await screen.findByLabelText("Доля RAM ноды, %");
    expect(share).toHaveValue(70);
    await user.clear(share);
    await user.type(share, "50");
    expect(screen.getByText("Поды перезапустятся по одной ноде")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(body).toEqual({ ram_budget_pct: 50 }));
  });
});

describe("split placement notes", () => {
  it("says when the placement is unknown and that the controller may group otherwise", async () => {
    server.use(http.get("/api/admin/hf/estimate", () => HttpResponse.json({ ...fixtures.hfEstimate, placement: null })));
    await splitWizard();
    expect(
      await screen.findByText("Размещение неизвестно: у нод нет меток памяти или их меньше, чем размер группы"),
    ).toBeInTheDocument();
  });

  it("captions the placement as an estimate", async () => {
    server.use(http.get("/api/admin/hf/estimate", () => HttpResponse.json(fixtures.hfEstimateSplit)));
    await splitWizard();
    expect(await screen.findByText("Оценка по текущим нодам; контроллер может собрать группы иначе")).toBeInTheDocument();
  });

  it("replicas say nothing about placement", async () => {
    const view = renderApp("/admin/deployments/new", "admin");
    await view.user.type(await screen.findByLabelText("Репозиторий Hugging Face"), REPO);
    await view.user.click(screen.getByRole("button", { name: "Найти файлы" }));
    await view.user.click(await screen.findByRole("radio", { name: /Q4_K_M/ }));
    await screen.findByRole("region", { name: "Оценка памяти" });
    expect(screen.queryByText(/Размещение неизвестно/)).not.toBeInTheDocument();
  });

  it("the deployment page shows a group the controller holds because the model does not fit", async () => {
    const problem = "Группа pc01, pc02 не запущена: модель не помещается в память нод (нужно 320,0 ГБ, есть 36,0 ГБ)";
    server.use(
      http.get("/api/admin/deployments/:id/status", () =>
        HttpResponse.json({ ...fixtures.deploymentStatus, placement_problems: [problem] }),
      ),
    );
    renderApp("/admin/deployments/2", "admin");
    expect(await screen.findByText(problem)).toBeInTheDocument();
  });
});

describe("split rules", () => {
  const base = { ctx: 8192, parallel: 1, mode: "split" as const, group: 2, accel: "nvidia" as const };

  it("limits a group to the devices llama.cpp can drive", () => {
    expect(checkRules({ ...base, group: 31 }).group).toBeUndefined();
    expect(checkRules({ ...base, group: 32 }).group).toBe(
      "В группе не больше 63 устройств: не больше 31 нод при использовании RAM нод",
    );
    expect(checkRules({ ...base, group: 63, useNodeRam: false }).group).toBeUndefined();
    expect(checkRules({ ...base, group: 64, useNodeRam: false }).group).toBe(
      "В группе не больше 63 устройств: не больше 63 нод",
    );
    expect(checkRules({ ...base, accel: "cpu", group: 63 }).group).toBeUndefined();
  });

  it("allows split on any accelerator, counting every member as a GPU node with its RAM", () => {
    expect(checkRules({ ...base, accel: "any" }).accel).toBeUndefined();
    expect(checkRules({ ...base, accel: "any", group: 31 }).group).toBeUndefined();
    expect(checkRules({ ...base, accel: "any", group: 32 }).group).toBe(
      "В группе не больше 63 устройств: не больше 31 нод при использовании RAM нод",
    );
  });

  it("checks the RAM share with the portal message", () => {
    expect(checkRules({ ...base, ramPct: 70 }).ramPct).toBeUndefined();
    expect(checkRules({ ...base, ramPct: 9 }).ramPct).toBe("Доля RAM ноды для модели от 10 до 90 %");
    expect(checkRules({ ...base, ramPct: 91 }).ramPct).toBe("Доля RAM ноды для модели от 10 до 90 %");
  });
});
