import { Fragment } from "react";
import { api } from "../api";
import { plural } from "../lib/labels";
import { useResource } from "../lib/useResource";
import type { Deployment, DeploymentStatus, NodeInfo } from "../types";
import "../styles/deployment-nodes.css";

/** nodes shown in a row; the rest is «+N». A split group is never cut in the middle. */
const MAX_SHOWN = 4;

type Dot = "ready" | "starting" | "down" | "unknown";

interface NodeView {
  name: string;
  dot: Dot;
  /** what the dot means, for the title and for a screen reader */
  note: string;
}

/** one node of a replicas deployment or a spare node (one entry), or a split group with its leader first */
type Unit = NodeView[];

type StatusNodes = DeploymentStatus["nodes"];

/** Node names of a deployment: its assignment from the list (the portal sends it) and the nodes that say they run it. */
export function memberNames(dep: Deployment, nodes: NodeInfo[] | undefined): string[] {
  const names = new Set<string>(dep.assigned_nodes ?? []);
  for (const n of nodes ?? []) if (n.deployment === dep.name) names.add(n.name);
  return [...names].sort((a, b) => a.localeCompare(b));
}

function viewOf(
  name: string,
  dep: Deployment,
  info: NodeInfo | undefined,
  opts: { split: boolean; grouped: boolean; status: StatusNodes | null; failed: boolean },
): NodeView {
  if (info && !info.ready) return { name, dot: "down", note: "не в сети" };
  if (info?.maintenance) return { name, dot: "starting", note: "на обслуживании" };
  if (!dep.enabled) return { name, dot: "unknown", note: "развёртывание выключено" };
  if (!opts.split) {
    if (!info) return { name, dot: "unknown", note: "нет данных" };
    return info.backends > 0
      ? { name, dot: "ready", note: "готова" }
      : { name, dot: "starting", note: "загружает модель" };
  }
  if (!opts.status) return { name, dot: "unknown", note: opts.failed ? "нет данных" : "состояние загружается" };
  if (!opts.grouped) return { name, dot: "unknown", note: "в резерве" };
  const pods = opts.status.find((n) => n.name === name)?.pods ?? [];
  return pods.length > 0 && pods.every((p) => p.ready)
    ? { name, dot: "ready", note: "готова" }
    : { name, dot: "starting", note: "загружает модель" };
}

/** the units to draw: split groups (leader first) from the status, then every other node on its own */
export function unitsOf(
  dep: Deployment,
  names: string[],
  byName: Map<string, NodeInfo>,
  status: DeploymentStatus | undefined,
  failed = false,
): Unit[] {
  const split = dep.mode === "split";
  const groups = split && dep.enabled ? (status?.groups ?? []).filter((g) => g.length > 0) : [];
  const inGroup = new Set(groups.flat());
  const view = (name: string, grouped: boolean) =>
    viewOf(name, dep, byName.get(name), { split, grouped, status: status ? status.nodes : null, failed });
  return [
    ...groups.map((g) => g.map((name) => view(name, true))),
    ...names.filter((n) => !inGroup.has(n)).map((n) => [view(n, false)]),
  ];
}

export function nodesCount(n: number): string {
  return `${n} ${plural(n, ["нода", "ноды", "нод"])}`;
}

function NodeTag({ node }: { node: NodeView }) {
  return (
    <span className="tag dep-node-tag" title={`${node.name}, ${node.note}`}>
      <i className={`dot ${node.dot}`} aria-hidden="true" />
      <span>{node.name}</span>
      <span className="visually-hidden">, {node.note}</span>
    </span>
  );
}

/** the units that fit, and how many nodes are left out (a group is shown whole or not at all, the first always is) */
function fit(units: Unit[]): { shown: Unit[]; hidden: NodeView[] } {
  const shown: Unit[] = [];
  let count = 0;
  let cut = units.length;
  for (const [i, u] of units.entries()) {
    if (shown.length > 0 && count + u.length > MAX_SHOWN) {
      cut = i;
      break;
    }
    shown.push(u);
    count += u.length;
  }
  return { shown, hidden: units.slice(cut).flat() };
}

function NodeUnits({ units }: { units: Unit[] }) {
  const total = units.reduce((sum, u) => sum + u.length, 0);
  if (total === 0) return <span className="muted small">Нет нод</span>;
  const { shown, hidden } = fit(units);
  return (
    <>
      <span className="dep-node-list">
        {shown.map((u) =>
          u.length > 1 ? (
            <span key={u[0].name} className="dep-node-group" role="group" aria-label={`Группа разделения: ${u.map((n) => n.name).join(" + ")}`}>
              {u.map((n, i) => (
                <Fragment key={n.name}>
                  {i > 0 && (
                    <span className="dep-node-plus" aria-hidden="true">
                      +
                    </span>
                  )}
                  <NodeTag node={n} />
                </Fragment>
              ))}
            </span>
          ) : (
            <NodeTag key={u[0].name} node={u[0]} />
          ),
        )}
        {hidden.length > 0 && (
          <span className="tag dep-node-more" title={hidden.map((n) => n.name).join(", ")}>
            +{hidden.length}
            <span className="visually-hidden"> ещё: {hidden.map((n) => n.name).join(", ")}</span>
          </span>
        )}
      </span>
      {/* the narrow screen shows this instead of the list */}
      <span className="dep-node-count">{nodesCount(total)}</span>
    </>
  );
}

/** a split group is known to the status of the deployment only: read it for these rows alone, the replicas ones need none */
function SplitNodes({ dep, names, byName }: { dep: Deployment; names: string[]; byName: Map<string, NodeInfo> }) {
  const status = useResource(() => api.deploymentStatus(dep.id), [dep.id, names.join(",")], { interval: 10000 });
  return <NodeUnits units={unitsOf(dep, names, byName, status.data, status.error !== null)} />;
}

/**
 * The «Ноды» line under a deployment row: the nodes with a state dot (ready, loading, down), split groups as «a + b» with
 * the leader first, «+N» for the rest and a bare count on a narrow screen. The names and the states come from the
 * list of nodes (one request for the whole page); the groups from the status of a split deployment only.
 */
export function DeploymentNodes({ dep, nodes }: { dep: Deployment; nodes: NodeInfo[] | undefined }) {
  const names = memberNames(dep, nodes);
  const byName = new Map((nodes ?? []).map((n) => [n.name, n]));
  return (
    <div className="dep-nodes" role="group" aria-label="Ноды">
      {nodes === undefined && names.length === 0 ? (
        <span className="muted">—</span>
      ) : dep.mode === "split" && dep.enabled && names.length > 0 ? (
        <SplitNodes dep={dep} names={names} byName={byName} />
      ) : (
        <NodeUnits units={unitsOf(dep, names, byName, undefined)} />
      )}
    </div>
  );
}
