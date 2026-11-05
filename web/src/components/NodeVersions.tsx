import { Check, CircleAlert } from "lucide-react";
import { useState } from "react";
import { ApiError } from "../api";
import {
  cancelNodeUpdate,
  fetchVersions,
  requestNodeUpdate,
  requestNodesUpdate,
  versionText,
  whenText,
  type NodeVersion,
  type UpdateResult,
  type UpdateWhen,
} from "../lib/nodeVersions";
import { useResource } from "../lib/useResource";
import { Confirm, Modal } from "./Modal";
import { Ic } from "./ui";

/** a node that has not asked main about updates for this long has no working updater (node-boot.ps1 of this version, a timer) */
const SILENT_MS = 5 * 60 * 1000;

/** The versions of the nodes, refreshed with the page; `byName` is what the «Ноды» table looks the nodes up in. */
export function useNodeVersions() {
  const res = useResource(fetchVersions, [], { interval: 10000 });
  const byName: Record<string, NodeVersion> = {};
  for (const n of res.data?.nodes ?? []) byName[n.name] = n;
  return { ...res, byName };
}

const updaterSilent = (info: NodeVersion): boolean =>
  !info.updater_seen_at || Date.now() - Date.parse(info.updater_seen_at) > SILENT_MS;

/** A quiet version tag of a node («0.1.0 · abc1234») with «устарела» when main's registry offers another image for it,
 * and the state of a requested update under it. */
export function NodeVersionTag({ info }: { info: NodeVersion | undefined }) {
  if (!info) return <span className="muted small">—</span>;
  return (
    <div className="stackcell" data-testid={`version-${info.name}`}>
      <span className="nowrap">
        <span className="tag" title={info.digest ?? undefined}>
          {versionText(info)}
        </span>
        {info.outdated === true && (
          <>
            {" "}
            <span className="tag warn">устарела</span>
          </>
        )}
      </span>
      {info.update && (
        <span className="muted small">
          Обновление запрошено: {whenText(info.update.when)}
          {info.update.waiting ? ", ждёт выключения развёртывания" : ""}
        </span>
      )}
      {info.update && updaterSilent(info) && (
        <span className="muted small">
          Нода не спрашивает главного об обновлениях: на ней нет node-boot.ps1 этой версии (docs/release.md, «Обновление нод»)
        </span>
      )}
      {info.update_error && (
        <span className="small error-text" role="alert">
          Обновление не удалось: {info.update_error}
        </span>
      )}
    </div>
  );
}

/** «После выключения развёртывания» / «Сейчас»: what the admin chooses for an update (the same words in the bulk toolbar). */
export function UpdateWhenFields({ when, onChange }: { when: UpdateWhen; onChange: (w: UpdateWhen) => void }) {
  return (
    <fieldset className="field">
      <legend className="label">Когда обновлять</legend>
      <label className="check">
        <input type="radio" name="update-when" checked={when === "idle"} onChange={() => onChange("idle")} /> После выключения развёртывания
      </label>
      <p className="secondary">Нода обновится, когда перестанет обслуживать развёртывание: его выключат или снимут с ноды.</p>
      <label className="check">
        <input type="radio" name="update-when" checked={when === "now"} onChange={() => onChange("now")} /> Сейчас
      </label>
      <p className="secondary">Контейнер ноды пересоздаётся в течение минуты. Модель на ней остановится и загрузится заново.</p>
    </fieldset>
  );
}

const errorOf = (e: unknown) => (e instanceof ApiError ? e.detail : "Не удалось отправить запрос на обновление");

/** «Обновить» of one node: asks when, then records the request on main; the node replaces its container itself. */
export function NodeUpdateButton({ node, info, onDone }: { node: string; info: NodeVersion | undefined; onDone: () => void | Promise<void> }) {
  const [asking, setAsking] = useState(false);
  const [when, setWhen] = useState<UpdateWhen>("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // nothing to offer for a node that runs the offered image, or whose version is not known at all
  if (!info || info.outdated === false) return null;

  async function send() {
    setBusy(true);
    setError(null);
    try {
      await requestNodeUpdate(node, when);
      setAsking(false);
      await onDone();
    } catch (e) {
      setError(errorOf(e));
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    setBusy(true);
    try {
      await cancelNodeUpdate(node);
    } catch (e) {
      setError(errorOf(e));
    } finally {
      setBusy(false);
      await onDone();
    }
  }

  return (
    <>
      {info.update ? (
        <button type="button" className="btn quiet sm" aria-label={`Отменить обновление ${node}`} disabled={busy} onClick={() => void cancel()}>
          Отменить обновление
        </button>
      ) : (
        <button type="button" className="btn quiet sm" aria-label={`Обновить ${node}`} disabled={busy} onClick={() => setAsking(true)}>
          Обновить
        </button>
      )}
      {error && !asking && (
        <span className="small error-text" role="alert">
          {error}
        </span>
      )}
      {asking && (
        <Confirm title={`Обновить ${node}`} confirmLabel="Обновить" busy={busy} onCancel={() => setAsking(false)} onConfirm={() => void send()}>
          <p>
            Нода скачает новый образ с главного сервера и заново создаст свой контейнер с теми же параметрами. Данные и скачанные модели
            остаются.
          </p>
          <UpdateWhenFields when={when} onChange={setWhen} />
          {error && (
            <p className="error-text" role="alert">
              {error}
            </p>
          )}
        </Confirm>
      )}
    </>
  );
}

/** The result of a bulk «Обновить» for the list of the toolbar: one line per node. */
export function updateResultText(r: UpdateResult): string {
  if (!r.ok) return r.error ?? "Не удалось отправить запрос на обновление";
  return r.waiting ? "Запрошено, ждёт выключения развёртывания" : r.when === "now" ? "Запрошено, нода обновится сейчас" : "Запрошено";
}

/** «Обновить образ» for the ticked nodes: one request for all, one line of result per node. A separate bar under the
 * actions of BulkNodeActions (its space is reserved too, the table does not move when a selection appears). */
export function BulkUpdateNodes({
  selected,
  byName,
  onFinished,
}: {
  /** names of the ticked nodes */
  selected: string[];
  byName: Record<string, NodeVersion>;
  /** called with the names that could not be asked, to keep them ticked and to refresh the table */
  onFinished: (failed: string[]) => void | Promise<void>;
}) {
  const [confirm, setConfirm] = useState(false);
  const [when, setWhen] = useState<UpdateWhen>("idle");
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<UpdateResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // a node that runs the offered image is left out; one whose version is unknown is asked (the admin knows better)
  const targets = selected.filter((n) => byName[n] && byName[n].outdated !== false);
  const count = selected.length;

  async function send() {
    setBusy(true);
    setError(null);
    try {
      const r = await requestNodesUpdate(targets, when);
      setConfirm(false);
      setResults(r.results);
      await onFinished(r.results.filter((x) => !x.ok).map((x) => x.name));
    } catch (e) {
      setError(errorOf(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="bulkbar" role="group" aria-label="Обновление образа выбранных нод">
      {count === 0 ? (
        <span className="muted small">Отметьте ноды, чтобы обновить их образ</span>
      ) : (
        <>
          <span className="bulk-count">Образ: {targets.length} из {count} требует обновления</span>
          <button type="button" className="btn quiet sm" disabled={targets.length === 0} onClick={() => setConfirm(true)}>
            Обновить образ
          </button>
        </>
      )}

      {confirm && (
        <Confirm
          title="Обновить образ нод"
          confirmLabel="Обновить"
          busy={busy}
          onCancel={() => setConfirm(false)}
          onConfirm={() => void send()}
        >
          <p>Нода скачает новый образ с главного сервера и заново создаст свой контейнер. Данные и скачанные модели остаются.</p>
          <ul className="node-list" aria-label="Ноды обновления">
            {targets.map((n) => (
              <li key={n} className="mono">
                {n}
              </li>
            ))}
          </ul>
          <UpdateWhenFields when={when} onChange={setWhen} />
          {error && (
            <p className="error-text" role="alert">
              {error}
            </p>
          )}
        </Confirm>
      )}

      {results && (
        <Modal
          title="Результат: обновление образа"
          onClose={() => setResults(null)}
          hideClose
          footer={
            <button type="button" className="btn primary" onClick={() => setResults(null)} data-autofocus>
              Закрыть
            </button>
          }
        >
          <ul className="node-results" aria-label="Результат по нодам" aria-live="polite">
            {results.map((r) => (
              <li key={r.name} className={`node-result ${r.ok ? "ok" : "error"}`}>
                <span className="mark" aria-hidden="true">
                  {r.ok ? <Ic icon={Check} /> : <Ic icon={CircleAlert} />}
                </span>
                <span className="mono">{r.name}</span>
                <span className={r.ok ? "muted" : "error-text"}>{updateResultText(r)}</span>
              </li>
            ))}
          </ul>
        </Modal>
      )}
    </div>
  );
}
