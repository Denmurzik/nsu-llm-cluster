import { Search } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api";
import { fmtBytes } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { HfFile, LocalModel } from "../types";
import { ErrorNotice, Ic } from "./ui";

/** «Свои файлы»: the ready files of «Мои модели»; a choice is the repository local/<name> and its file. */
export function LocalSourcePicker({
  repo,
  file,
  onPick,
  name = "wz-local-file",
  legend = "Файл модели",
  label = "Свой файл модели",
}: {
  repo: string;
  file: string | null;
  onPick: (repo: string, file: string) => void;
  name?: string;
  legend?: string;
  label?: string;
}) {
  const catalog = useResource(() => api.localModels(), []);
  if (catalog.loading && !catalog.data) return <p className="muted">Загрузка списка своих моделей</p>;
  if (catalog.error && !catalog.data) return <ErrorNotice error={catalog.error} />;
  const models: LocalModel[] = catalog.data?.models ?? [];
  const choices = models.flatMap((m) => m.files.filter((f) => f.status === "ready").map((f) => ({ m, f })));
  if (choices.length === 0) {
    return (
      <p className="muted">
        Готовых своих моделей нет. Загрузите файл GGUF на странице <Link to="/admin/local-models">«Мои модели»</Link>
      </p>
    );
  }
  return (
    <fieldset>
      <legend>{legend}</legend>
      <div className="choices" role="radiogroup" aria-label={label}>
        {choices.map(({ m, f }) => (
          <label key={`${m.name}/${f.file}`} className="choice">
            <input type="radio" name={name} checked={repo === m.repo && file === f.file} onChange={() => onPick(m.repo, f.file)} />
            <span className="tag">{f.quant ?? "без метки"}</span> <span className="mono">{fmtBytes(f.size_bytes)}</span>{" "}
            <span className="grow mono small">
              {m.name}/<span className="muted">{f.file}</span>
            </span>
            {f.shards.length > 1 && <span className="muted small">{f.shards.length} части</span>}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/** A model file picked like the main one: a Hugging Face repository and its GGUF files, or «Свои файлы».
 * The choice is `repo` (own files: local/<name>) and `file`; changing the repository clears the file. */
export function ModelFilePicker({
  idPrefix,
  legend,
  repo,
  file,
  onChange,
}: {
  idPrefix: string;
  legend: string;
  repo: string;
  file: string | null;
  onChange: (repo: string, file: string | null) => void;
}) {
  const [source, setSource] = useState<"hf" | "local">(repo.startsWith("local/") ? "local" : "hf");
  const [files, setFiles] = useState<HfFile[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function changeSource(next: "hf" | "local") {
    if (next === source) return;
    setSource(next);
    setFiles(null);
    setError(null);
    onChange("", null);
  }

  async function search(e: FormEvent) {
    e.preventDefault();
    e.stopPropagation();
    const target = repo.trim();
    if (!target) return;
    setSearching(true);
    setError(null);
    setFiles(null);
    try {
      const list = await api.hfFiles(target);
      setFiles(list);
      if (list.length === 1) onChange(target, list[0].file);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Не удалось обратиться к Hugging Face");
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className="section">
      <div className="tabs" role="tablist" aria-label={`Источник: ${legend}`}>
        <button type="button" role="tab" className="tab" aria-selected={source === "hf"} onClick={() => changeSource("hf")}>
          Hugging Face
        </button>
        <button type="button" role="tab" className="tab" aria-selected={source === "local"} onClick={() => changeSource("local")}>
          Свои файлы
        </button>
      </div>
      {source === "local" ? (
        <LocalSourcePicker
          repo={repo}
          file={file}
          onPick={(r, f) => onChange(r, f)}
          name={`${idPrefix}-local-file`}
          legend={legend}
          label={`${legend}, свои файлы`}
        />
      ) : (
        <>
          <div className="row end">
            <div className="field grow">
              <label htmlFor={`${idPrefix}-repo`}>{legend}: репозиторий Hugging Face</label>
              <input
                id={`${idPrefix}-repo`}
                className="input mono"
                value={repo}
                placeholder="владелец/репозиторий"
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => {
                  onChange(e.target.value, null);
                  setFiles(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void search(e);
                }}
              />
            </div>
            <button type="button" className="btn" disabled={!repo.trim() || searching} onClick={search}>
              <Ic icon={Search} />
              Найти файлы
            </button>
          </div>
          {error && <ErrorNotice error={error} />}
          {files && files.length === 0 && <p className="muted">В репозитории нет файлов GGUF</p>}
          {files && files.length > 0 && (
            <div className="choices" role="radiogroup" aria-label={legend}>
              {files.map((f) => (
                <label key={f.file} className="choice">
                  <input type="radio" name={`${idPrefix}-file`} checked={file === f.file} onChange={() => onChange(repo.trim(), f.file)} />
                  <span className="tag">{f.quant ?? "без метки"}</span> <span className="mono">{fmtBytes(f.size_bytes)}</span>{" "}
                  <span className="grow mono muted small">{f.file}</span>
                  {f.shards.length > 1 && <span className="muted small">{f.shards.length} части</span>}
                </label>
              ))}
            </div>
          )}
          {!files && file && (
            <p className="secondary">
              Выбрано: <span className="mono">{file}</span>
            </p>
          )}
        </>
      )}
    </div>
  );
}
