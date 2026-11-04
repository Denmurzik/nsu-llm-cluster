import { useLocation } from "react-router-dom";
import { api } from "../api";
import { Logo } from "../components/Logo";
import { Cells, StatusLabel, backendsText } from "../components/Status";
import { STATUS_LABEL } from "../lib/labels";
import { useResource } from "../lib/useResource";

export function Login() {
  const { pathname, search } = useLocation();
  const returnTo = encodeURIComponent(`${pathname}${search}`);
  const status = useResource(() => api.publicStatus(), [], { interval: 15000 });
  const host = typeof window === "undefined" ? "" : window.location.host;

  return (
    <div className="login">
      <section className="login-left">
        <span className="brand" style={{ padding: 0 }}>
          <Logo />
        </span>
        <h1>Языковые модели на машинах аудитории</h1>
        <p className="sub">
          Войдите через NSU ID, выпустите ключ и обращайтесь к моделям кластера НГУ так же, как к OpenAI API.
        </p>
        <div className="login-actions">
          <a className="btn primary lg" href={`/auth/login?return_to=${returnTo}`}>
            Войти через NSU ID
          </a>
          <span className="muted">Доступ для студентов и сотрудников НГУ</span>
        </div>
      </section>

      <section className="login-right" aria-label="Пример и статус">
        <div className="keycard" aria-hidden="true">
          <div className="top">
            <span className="brand" style={{ padding: 0, height: "auto", fontSize: 14 }}>
              <Logo />
            </span>
            <span className="tag">ключ доступа</span>
          </div>
          <div>
            <div className="muted small">лабораторная-3</div>
            <div className="key">sk-…a4F9</div>
          </div>
          <div className="meta">
            <div>
              <span className="muted small">Адрес</span>
              <span className="mono small">{host ? `https://${host}/v1` : "/v1"}</span>
            </div>
            <div>
              <span className="muted small">Модели</span>
              <span className="small">все из каталога</span>
            </div>
          </div>
        </div>

        <div className="section">
          <h2>Статус моделей</h2>
          {status.error && !status.data ? (
            <p className="muted">Статус моделей сейчас недоступен</p>
          ) : status.data && status.data.models.length === 0 ? (
            <p className="muted">Моделей пока нет</p>
          ) : !status.data ? (
            <>
              {/* Placeholder rows of the same height: the key card above stays put when the list arrives. */}
              <span className="visually-hidden" role="status">
                Загрузка
              </span>
              <ul className="status-list placeholder" aria-hidden="true">
                {[0, 1, 2].map((i) => (
                  <li key={i}>
                    <span className="bar" />
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <ul className="status-list" aria-label="Статус моделей">
              {status.data.models.map((m) => (
                <li key={m.name} aria-label={`${m.name}, ${STATUS_LABEL[m.status]}`}>
                  <span className="name">{m.name}</span>
                  <StatusLabel status={m.status} />
                  <Cells ready={m.ready_backends} status={m.status} />
                  <span className="count">{backendsText(m.ready_backends)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>
    </div>
  );
}
