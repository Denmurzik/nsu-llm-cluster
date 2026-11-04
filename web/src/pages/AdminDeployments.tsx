import { Plus } from "lucide-react";
import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { DeploymentWizard } from "../components/DeploymentWizard";
import { DeploymentNodes } from "../components/DeploymentNodes";
import { DeploymentRowActions } from "../components/DeploymentRowActions";
import { Cells, StatusLabel } from "../components/Status";
import { ErrorNotice, Ic, Loading, PageHead } from "../components/ui";
import "../styles/tools.css";
import { fmtInt } from "../lib/format";
import { ACCEL_LABEL, ENGINE_LABEL, modeLabel } from "../lib/labels";
import { useResource } from "../lib/useResource";
import type { CatalogModel } from "../types";

type Kind = "all" | "main" | "experimental";
const KINDS: { kind: Kind; label: string }[] = [
  { kind: "all", label: "Все" },
  { kind: "main", label: "Основные" },
  { kind: "experimental", label: "Экспериментальные" },
];

export function AdminDeployments() {
  const [kind, setKind] = useState<Kind>("all");
  const deployments = useResource(() => api.deployments(), [], { interval: 10000 });
  // the catalog carries the live status of every enabled deployment
  const catalog = useResource(() => api.models(), [], { interval: 10000 });
  // the nodes of every deployment and their state: one call for the whole table
  const nodes = useResource(() => api.nodes(), [], { interval: 10000 });
  // a failed «Выключить» / «Включить» / «Удалить» of a row, shown above the table until the next action
  const [actionError, setActionError] = useState<string | null>(null);
  const byName = new Map<string, CatalogModel>((catalog.data ?? []).map((m) => [m.name, m]));
  const shown = (deployments.data ?? []).filter(
    (d) => kind === "all" || (kind === "experimental") === d.experimental,
  );

  return (
    <div className="page">
      <PageHead
        title="Развёртывания"
        lead="Модели, которые кластер держит запущенными. Контекст, режим и ноды меняются здесь же."
        actions={
          <Link className="btn primary" to="/admin/deployments/new">
            <Ic icon={Plus} />
            Создать развёртывание
          </Link>
        }
      />
      <ErrorNotice error={deployments.error} />
      <ErrorNotice error={actionError} />
      {nodes.error && !nodes.data && (
        <p className="muted small" role="status">
          Ноды не загрузились
        </p>
      )}
      {deployments.loading && !deployments.data && <Loading />}
      {deployments.data && (
        <div className="tabs" role="tablist" aria-label="Тип развёртываний" style={{ marginBottom: "var(--s3)" }}>
          {KINDS.map((k) => (
            <button
              key={k.kind}
              type="button"
              role="tab"
              className="tab"
              aria-selected={kind === k.kind}
              onClick={() => setKind(k.kind)}
            >
              {k.label}
            </button>
          ))}
        </div>
      )}
      {deployments.data && (
        <div className="table-wrap">
          <table className="data" aria-label="Развёртывания">
            <thead>
              <tr>
                <th>Название</th>
                <th>Модель</th>
                <th>Режим</th>
                <th>Ускоритель</th>
                <th className="num">Контекст на запрос</th>
                <th className="num">Параллельность</th>
                <th>Статус</th>
                <th className="actions">Действия</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((d) => {
                const live = byName.get(d.name);
                return (
                  <Fragment key={d.id}>
                    <tr className="dep-row">
                      <td>
                        <Link to={`/admin/deployments/${d.id}`} className="mono">
                          {d.name}
                        </Link>
                        {d.experimental && (
                          <>
                            {" "}
                            <span className="tag" title="Видна и доступна только администраторам">
                              экспериментальная
                            </span>
                          </>
                        )}
                      </td>
                      <td>
                        <div className="stackcell">
                          <span className="mono small">{d.repo}</span>
                          <span className="mono small muted">{d.file ?? ENGINE_LABEL[d.engine]}</span>
                        </div>
                      </td>
                      <td>{modeLabel(d.mode, d.group_size)}</td>
                      <td>{ACCEL_LABEL[d.accel]}</td>
                      <td className="num">{fmtInt(d.ctx_per_request)}</td>
                      <td className="num">{d.parallel}</td>
                      <td>
                        <span className="status">
                          {d.enabled ? (
                            <StatusLabel status={live?.status ?? "unknown"} />
                          ) : (
                            <StatusLabel status="unknown" off />
                          )}
                          {d.enabled && live && <Cells ready={live.ready_backends} status={live.status} />}
                        </span>
                      </td>
                      <td>
                        <DeploymentRowActions
                          dep={d}
                          onError={setActionError}
                          onChanged={() => {
                            void deployments.reload();
                            void catalog.reload();
                            void nodes.reload();
                          }}
                        />
                      </td>
                    </tr>
                    {/* the nodes sit in a line of their own: a column would not fit next to the eight others */}
                    <tr className="dep-nodes-row">
                      <td colSpan={8}>
                        <DeploymentNodes dep={d} nodes={nodes.data?.nodes} />
                      </td>
                    </tr>
                  </Fragment>
                );
              })}
              {shown.length === 0 && (
                <tr>
                  <td colSpan={8} className="empty-row">
                    {deployments.data.length === 0 ? "Развёртываний пока нет" : "Таких развёртываний нет"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function AdminDeploymentNew() {
  return (
    <div className="page">
      <PageHead
        title="Новое развёртывание"
        lead="Выберите репозиторий на Hugging Face, файл модели и параметры. Расчёт памяти для каждой ноды обновляется по мере ввода."
        actions={
          <Link className="btn quiet" to="/admin/deployments">
            К списку
          </Link>
        }
      />
      <DeploymentWizard />
    </div>
  );
}
