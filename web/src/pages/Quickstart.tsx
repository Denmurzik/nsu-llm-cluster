import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { CopyButton } from "../components/CopyButton";
import { ErrorNotice, Loading, PageHead } from "../components/ui";
import { fmtInt } from "../lib/format";
import { useResource } from "../lib/useResource";

type Lang = "curl" | "powershell" | "python" | "js";

const LANGS: { id: Lang; label: string }[] = [
  { id: "curl", label: "curl" },
  { id: "powershell", label: "PowerShell" },
  { id: "python", label: "Python" },
  { id: "js", label: "JavaScript" },
];

export function snippet(lang: Lang, base: string, model: string): string {
  if (lang === "curl") {
    return `curl ${base}/chat/completions \\
  -H "Authorization: Bearer $LLM_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${model}",
    "messages": [{"role": "user", "content": "Привет"}]
  }'`;
  }
  if (lang === "powershell") {
    // PowerShell 5.1 drops the double quotes of a native argument: escape them for curl.exe
    return `$body = '{"model": "${model}", "messages": [{"role": "user", "content": "Привет"}]}'
curl.exe ${base}/chat/completions \`
  -H "Authorization: Bearer $env:LLM_API_KEY" \`
  -H "Content-Type: application/json" \`
  -d ($body -replace '"', '\\"')`;
  }
  if (lang === "python") {
    return `import os
from openai import OpenAI

client = OpenAI(
    base_url="${base}",
    api_key=os.environ["LLM_API_KEY"],
)

resp = client.chat.completions.create(
    model="${model}",
    messages=[{"role": "user", "content": "Привет"}],
)
print(resp.choices[0].message.content)`;
  }
  return `import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "${base}",
  apiKey: process.env.LLM_API_KEY,
});

const resp = await client.chat.completions.create({
  model: "${model}",
  messages: [{ role: "user", content: "Привет" }],
});
console.log(resp.choices[0].message.content);`;
}

export function Quickstart() {
  const models = useResource(() => api.models(), []);
  const [lang, setLang] = useState<Lang>("curl");
  const [picked, setPicked] = useState<string>("");
  const base = `https://${window.location.host}/v1`;

  const list = models.data ?? [];
  const model = list.find((m) => m.name === picked) ?? list.find((m) => m.status === "ready") ?? list[0];
  const name = model?.name ?? "имя-модели";
  const code = useMemo(() => snippet(lang, base, name), [lang, base, name]);

  return (
    <div className="page">
      <PageHead
        title="Быстрый старт"
        lead="Кластер говорит на протоколе OpenAI: подойдёт любая библиотека или приложение, где можно указать адрес API."
      />

      <section className="section">
        <h2>Адрес API</h2>
        <div className="code inline">
          <code>{base}</code>
          <CopyButton text={base} ariaLabel="Копировать адрес" />
        </div>
      </section>

      <section className="section">
        <h2>Ключ</h2>
        <p className="secondary">
          Выпустите ключ на странице <Link to="/keys">Ключи</Link> и сохраните его в переменной окружения{" "}
          <code>LLM_API_KEY</code>. Так он не попадёт в код и в репозиторий.
        </p>
      </section>

      <section className="section">
        <header>
          <h2>Запрос</h2>
        </header>
        <ErrorNotice error={models.error} />
        {models.loading && !models.data && <Loading />}
        {(models.data || models.error) && (
          <>
            <div className="row end">
              <div className="field fixed">
                <label htmlFor="qs-model">Модель</label>
                <select
                  id="qs-model"
                  className="select"
                  value={model?.name ?? ""}
                  onChange={(e) => setPicked(e.target.value)}
                >
                  {list.map((m) => (
                    <option key={m.name} value={m.name}>
                      {m.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="tabs" role="tablist" aria-label="Язык примера">
                {LANGS.map((l) => (
                  <button
                    key={l.id}
                    type="button"
                    role="tab"
                    className="tab"
                    aria-selected={lang === l.id}
                    onClick={() => setLang(l.id)}
                  >
                    {l.label}
                  </button>
                ))}
              </div>
            </div>
            {model && (
              <p className="hint">
                Контекст на запрос: {fmtInt(model.ctx_per_request)} токенов, параллельных запросов: {model.parallel}
              </p>
            )}
            <div className="code">
              <pre data-testid="snippet">
                <code>{code}</code>
              </pre>
              <span className="copy">
                <CopyButton text={code} ariaLabel="Копировать пример" />
              </span>
            </div>
            <p className="hint">
              Если curl или библиотека не доверяют сертификату кластера, скачайте корневой сертификат{" "}
              <a href="/ca.crt">ca.crt</a> и установите его или передайте в запросе (curl: <code>--cacert ca.crt</code>).
            </p>
          </>
        )}
      </section>
    </div>
  );
}
