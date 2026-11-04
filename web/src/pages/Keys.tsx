import { Plus, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api, ApiError } from "../api";
import { CopyButton } from "../components/CopyButton";
import { Confirm, Modal } from "../components/Modal";
import { ErrorNotice, Ic, Loading, Notice, PageHead } from "../components/ui";
import { ALIAS_RE, ALIAS_RESERVED_PREFIX, fmtDate, fmtInt } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { ApiKey, CreatedKey } from "../types";

export function Keys() {
  const keys = useResource(() => api.keys(), []);
  const [alias, setAlias] = useState("");
  const [days, setDays] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedKey | null>(null);
  const [toDelete, setToDelete] = useState<ApiKey | null>(null);
  const [deleting, setDeleting] = useState(false);

  const trimmed = alias.trim();
  const reserved = trimmed.startsWith(ALIAS_RESERVED_PREFIX);
  const aliasValid = ALIAS_RE.test(trimmed) && !reserved;
  const showAliasError = alias.length > 0 && !aliasValid;

  async function create(e: FormEvent) {
    e.preventDefault();
    if (!aliasValid || busy) return;
    setBusy(true);
    setError(null);
    try {
      const key = await api.createKey(trimmed, days ? Number(days) : null);
      setCreated(key);
      setAlias("");
      setDays("");
      await keys.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Не удалось создать ключ");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!toDelete) return;
    setDeleting(true);
    try {
      await api.deleteKey(toDelete.id);
      setToDelete(null);
      await keys.reload();
    } catch (err) {
      setToDelete(null);
      setError(err instanceof ApiError ? err.detail : "Не удалось отозвать ключ");
    } finally {
      setDeleting(false);
    }
  }

  const list = keys.data?.keys;

  return (
    <div className="page">
      <PageHead
        title="Ключи"
        lead="Ключ действует для всех моделей. Значение ключа показывается один раз при создании, дальше в списке виден только его конец."
      />

      <form className="card stack" onSubmit={create}>
        <h2>Новый ключ</h2>
        <div className="row end">
          <div className="field grow">
            <label htmlFor="key-alias">Название ключа</label>
            <input
              id="key-alias"
              className="input"
              value={alias}
              maxLength={40}
              autoComplete="off"
              aria-invalid={showAliasError}
              aria-describedby={showAliasError ? "key-alias-error" : undefined}
              onChange={(e) => setAlias(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="key-days">Срок действия</label>
            <select id="key-days" className="select" value={days} onChange={(e) => setDays(e.target.value)}>
              <option value="">Без срока</option>
              <option value="30">30 дней</option>
              <option value="90">90 дней</option>
            </select>
          </div>
          <button type="submit" className="btn primary" disabled={!aliasValid || busy}>
            <Ic icon={Plus} />
            Создать ключ
          </button>
        </div>
        {showAliasError && (
          <p id="key-alias-error" className="error-text">
            {reserved
              ? `Название не может начинаться с «${ALIAS_RESERVED_PREFIX}»`
              : "Допустимы буквы, цифры, пробел, дефис и подчёркивание"}
          </p>
        )}
        {error && <ErrorNotice error={error} />}
      </form>

      {keys.data?.usageUnavailable && <Notice kind="warn">Статистика использования сейчас недоступна</Notice>}
      {keys.error && !keys.data && <ErrorNotice error={keys.error} />}
      {keys.loading && !keys.data && <Loading />}

      {list && (
        <div className="table-wrap">
          <table className="data" aria-label="Ключи доступа">
            <thead>
              <tr>
                <th>Название</th>
                <th>Ключ</th>
                <th>Создан</th>
                <th>Действует до</th>
                <th className="num">Токены за 30 дней</th>
                <th className="num">Запросы за 30 дней</th>
                <th aria-label="Действия" />
              </tr>
            </thead>
            <tbody>
              {list.map((k) => (
                <tr key={k.id}>
                  <td>{k.alias}</td>
                  <td className="mono">{k.masked}</td>
                  <td>{fmtDate(k.created_at)}</td>
                  <td>{k.expires_at ? fmtDate(k.expires_at) : <span className="muted">Без срока</span>}</td>
                  <td className="num">{fmtInt(k.tokens_30d)}</td>
                  <td className="num">{fmtInt(k.requests_30d)}</td>
                  <td className="actions">
                    <button
                      type="button"
                      className="btn quiet sm"
                      aria-label={`Отозвать ключ ${k.alias}`}
                      onClick={() => setToDelete(k)}
                    >
                      <Ic icon={Trash2} />
                      Отозвать
                    </button>
                  </td>
                </tr>
              ))}
              {list.length === 0 && (
                <tr>
                  <td colSpan={7} className="empty-row">
                    Ключей пока нет
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {created && <CreatedKeyDialog created={created} onDone={() => setCreated(null)} />}

      {toDelete && (
        <Confirm
          title="Отзыв ключа"
          confirmLabel="Отозвать"
          busy={deleting}
          onCancel={() => setToDelete(null)}
          onConfirm={remove}
        >
          <p>Отозвать ключ «{toDelete.alias}»? Приложения, которые его используют, перестанут работать в течение ~15 с.</p>
        </Confirm>
      )}
    </div>
  );
}

/** The only place the key is shown: Escape (or any other way out but the button) asks first. */
function CreatedKeyDialog({ created, onDone }: { created: CreatedKey; onDone: () => void }) {
  const [asking, setAsking] = useState(false);
  return (
    <Modal
      title={`Ключ «${created.alias}» создан`}
      onClose={() => setAsking(true)}
      hideClose
      footer={
        asking ? (
          <>
            <button type="button" className="btn" onClick={() => setAsking(false)} data-autofocus>
              Вернуться к ключу
            </button>
            <button type="button" className="btn primary" onClick={onDone}>
              Закрыть
            </button>
          </>
        ) : (
          <button type="button" className="btn primary" onClick={onDone}>
            Я сохранил ключ
          </button>
        )
      }
    >
      {asking && (
        <Notice kind="warn">
          <span className="title">Ключ больше не будет показан. Закрыть?</span>
        </Notice>
      )}
      <Notice kind="warn">
        <span className="title">Ключ показывается один раз</span>
        <br />
        Скопируйте его сейчас, потом посмотреть значение будет нельзя.
      </Notice>
      <div className="code inline">
        <code>{created.key}</code>
        <CopyButton text={created.key} autoFocus={!asking} />
      </div>
    </Modal>
  );
}
