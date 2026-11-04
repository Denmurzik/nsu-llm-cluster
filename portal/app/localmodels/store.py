"""Own model files on main, beside Hugging Face: the `local/<name>` namespace on the volume of the Hugging Face mirror.

The mirror (main/hf_mirror.py) and the portal see the same directory: the volume llm-main-hf, mounted at
HF_MIRROR_DATA_DIR in the main container and as a hostPath in the mirror's pod. Layout:
  models/local/<name>/<file>.gguf      own models; the mirror serves them to the nodes as the repository local/<name>
  models/*.gguf, models/local/*.gguf   files the admin dropped into the folder: moved into models/local/<stem>/
  .uploads/<id>.part                   uploads in progress, on the same file system: the finished file is renamed
                                       into place
  models/local/<name>/.manifest.json   the files of the model the catalog calls ready, with size, mtime and sha256,
                                       written by the portal after every scan, upload and deletion. The mirror serves
                                       only those (unchanged since), and so does this store to deployments and the
                                       inspector: a file copied in by hand counts once a scan has checked it.
Symbolic links are skipped everywhere: a link out of the volume must not become a model.

`LocalStore` answers the same calls as `HfClient` for a `local/<name>` repository (`app.catalog.sources`), so the
deployments, the inspector and the estimator work on local files unchanged. Everything here is synchronous file
system work except those calls, which run it in a thread.
"""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.catalog.gguf import (
    MAGIC,
    GgufHeader,
    GgufInvalid,
    GgufLayout,
    GgufMeta,
    GgufTruncated,
    gguf_layout,
    parse_gguf,
    parse_gguf_header,
)
from app.catalog.hf import FIRST_RANGE, GgufChoice, HfFile, HfNotFound, group_gguf

LOCAL_OWNER = "local"
LOCAL_PREFIX = LOCAL_OWNER + "/"
# a part of a repository name for the mirror (main/hf_mirror.py `_REPO_PART`), so local/<name> is a valid repository
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,250}\.gguf", re.IGNORECASE)
_SHARD = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)
# one part of a file path the mirror and the nodes take as it is: no spaces, quoting or control characters in URLs
_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,250}")
MANIFEST = ".manifest.json"  # same name in main/hf_mirror.py
MTIME_SLACK = 1e-3
GiB = 2**30
DISK_RESERVE = 2 * GiB  # kept free on the volume at least: the mirror, Postgres backups and the logs live beside it
# and at least this share of it: kubelet on llm-main evicts Postgres, LiteLLM and the mirror below 5 % free (same in hf_mirror.py)
DISK_RESERVE_PCT = 15
HEADER_MAX = 256 * 2**20  # local reads are cheap: a header with a large tokenizer is still read whole


class LocalNotFound(HfNotFound):
    """No such own model or file (a subclass of HfNotFound: callers of the Hugging Face client handle it already)."""


def is_local(repo: str | None) -> bool:
    return bool(repo) and repo.startswith(LOCAL_PREFIX)


def local_repo(name: str) -> str:
    return LOCAL_PREFIX + name


def name_of(repo: str) -> str:
    return repo[len(LOCAL_PREFIX):]


def valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name)) and ".." not in name


def valid_file(file: str) -> bool:
    return bool(_FILE.fullmatch(file)) and ".." not in file


def shard_index(path: str) -> tuple[int, int] | None:
    """(index, total) of a `-0000N-of-0000M.gguf` shard, else None."""
    m = _SHARD.search(posixpath.basename(path))
    return (int(m.group(1)), int(m.group(2))) if m else None


def suggest_name(file: str) -> str:
    """Model name for a file: its name without `.gguf` and the shard suffix, other characters as `-`."""
    stem = _SHARD.sub("", posixpath.basename(file))
    stem = re.sub(r"\.gguf$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    stem = re.sub(r"\.{2,}", ".", stem)[:96].rstrip("-._")
    return stem if valid_name(stem) else "model"


@dataclass(frozen=True)
class FileCheck:
    """GGUF check of one file: the metadata of a first (or only) file, or the Russian reason it cannot be used."""

    meta: GgufMeta | None
    error: str | None


@dataclass(frozen=True)
class FoundFile:
    model: str
    path: str  # inside the model directory, posix
    size: int
    mtime: float
    problem: str | None = None  # a name the mirror cannot serve, in Russian: the file is listed as invalid


def name_problem(model: str, path: str) -> str | None:
    if not valid_name(model):
        return (
            f"Недопустимое имя каталога модели «{model}»: только латинские буквы, цифры, точка, дефис и подчёркивание. "
            "Переименуйте каталог"
        )
    if any(not _PART.fullmatch(p) or ".." in p for p in path.split("/")):
        return (
            f"Недопустимое имя файла «{path}»: только латинские буквы, цифры, точка, дефис и подчёркивание. "
            "Переименуйте файл"
        )
    return None


def _read_header(path: Path, parse):
    """Runs `parse` over a growing prefix of the file until the header fits (GgufTruncated otherwise)."""
    size = path.stat().st_size
    want = FIRST_RANGE
    with path.open("rb") as f:
        while True:
            f.seek(0)
            data = f.read(want)
            try:
                return parse(data)
            except GgufTruncated as e:
                if len(data) >= size or want >= HEADER_MAX:
                    raise GgufInvalid("the file ends inside the GGUF header") from e
                want = min(HEADER_MAX, max(e.needed, 2 * want))


def signature_error(head: bytes) -> str | None:
    """Why the first 8 bytes of a file are not those of a GGUF v2/v3 file, or None."""
    if len(head) < 8 or head[:4] != MAGIC:
        return "Файл не является GGUF"
    version = int.from_bytes(head[4:8], "little")
    if version not in (2, 3):
        return f"Неподдерживаемая версия GGUF: {version}"
    return None


def check_gguf(path: Path, name: str | None = None) -> FileCheck:
    """Signature, version and, for a first or only file, the architecture keys the estimator needs. `name` is the
    file name when `path` is not it (an upload in progress): a later shard has no architecture keys."""
    with path.open("rb") as f:
        error = signature_error(f.read(8))
    if error:
        return FileCheck(None, error)
    shard = shard_index(name or path.name)
    if shard is not None and shard[0] > 1:
        return FileCheck(None, None)  # a later shard carries no architecture keys
    try:
        return FileCheck(_read_header(path, parse_gguf_header), None)
    except GgufInvalid as e:
        if "ends inside" in str(e):
            return FileCheck(None, "Файл обрывается внутри заголовка GGUF")
        return FileCheck(None, "В заголовке GGUF нет ключей архитектуры модели")
    except GgufTruncated:
        return FileCheck(None, "Файл обрывается внутри заголовка GGUF")


def _hidden(part: str) -> bool:
    return part.startswith(".")


def _matches(entry: dict | None, size: int, mtime: float) -> bool:
    return (
        isinstance(entry, dict) and entry.get("size") == size
        and isinstance(entry.get("mtime"), int | float) and abs(entry["mtime"] - mtime) < MTIME_SLACK
    )


class LocalStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def local_dir(self) -> Path:
        return self.models_dir / LOCAL_OWNER

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / ".uploads"

    # ---- paths ----------------------------------------------------------------------

    def model_dir(self, name: str) -> Path:
        if not valid_name(name):
            raise LocalNotFound(name)
        return self.local_dir / name

    def path_of(self, name: str, file: str) -> Path:
        parts = file.split("/")
        if not file or any(p in ("", ".", "..") or _hidden(p) for p in parts):
            raise LocalNotFound(f"{name}/{file}")
        return self.model_dir(name).joinpath(*parts)

    def part_path(self, upload_id: str) -> Path:
        return self.uploads_dir / f"{upload_id}.part"

    def _repo_dir(self, repo: str) -> Path:
        if not is_local(repo):
            raise LocalNotFound(repo)
        d = self.model_dir(name_of(repo))
        if not d.is_dir():
            raise LocalNotFound(repo)
        return d

    # ---- listing --------------------------------------------------------------------

    @staticmethod
    def _files_in(base: Path, name: str) -> list[FoundFile]:
        """Every visible regular file under `base` (dot files, dot directories and symbolic links skipped)."""
        out: list[FoundFile] = []
        if base.is_symlink() or not base.is_dir():
            return out
        for dirpath, dirnames, names in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not _hidden(d) and not (Path(dirpath) / d).is_symlink())
            for n in sorted(names):
                if _hidden(n):
                    continue
                f = Path(dirpath) / n
                try:
                    st = f.lstat()
                except OSError:
                    continue
                if f.is_symlink() or not f.is_file():
                    continue
                rel = f.relative_to(base).as_posix()
                out.append(FoundFile(name, rel, st.st_size, st.st_mtime, name_problem(name, rel)))
        return out

    def files(self, name: str) -> list[FoundFile]:
        """Every visible file of one model directory (all kinds)."""
        return self._files_in(self.model_dir(name), name)

    def model_dirs(self) -> list[Path]:
        """Directories under models/local/ (no dot directories, no links), valid names or not."""
        if not self.local_dir.is_dir():
            return []
        return [d for d in sorted(self.local_dir.iterdir()) if not _hidden(d.name) and not d.is_symlink() and d.is_dir()]

    def walk(self) -> list[FoundFile]:
        """GGUF files of every model directory under models/local/; a name the mirror cannot serve has a `problem`.
        A directory name too long for the catalog (over 96 characters) is left out."""
        out: list[FoundFile] = []
        for d in self.model_dirs():
            if len(d.name) <= 96:
                out += [f for f in self._files_in(d, d.name) if f.path.lower().endswith(".gguf")]
        return out

    def _loose(self) -> list[Path]:
        out = []
        for folder in (self.models_dir, self.local_dir):
            if folder.is_dir():
                out += [
                    f for f in sorted(folder.iterdir())
                    if not _hidden(f.name) and f.name.lower().endswith(".gguf") and not f.is_symlink() and f.is_file()
                ]
        return out

    def used_bytes(self) -> int:
        """Own models plus files dropped into the folder and not moved yet: all of them take the volume."""
        loose = 0
        for f in self._loose():
            try:
                loose += f.lstat().st_size
            except OSError:
                pass
        return sum(f.size for f in self.walk()) + loose

    # ---- manifests (what is ready) -------------------------------------------------

    def manifest(self, name: str) -> dict[str, dict]:
        try:
            data = json.loads((self.model_dir(name) / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError, LocalNotFound):
            return {}
        files = data.get("files") if isinstance(data, dict) else None
        return files if isinstance(files, dict) else {}

    def write_manifests(self, ready: dict[str, dict[str, dict]]) -> None:
        """Writes models/local/<name>/.manifest.json for every model with ready files (only when it changes) and
        removes it from the other model directories."""
        for d in self.model_dirs():
            path = d / MANIFEST
            files = ready.get(d.name) if valid_name(d.name) else None
            if not files:
                path.unlink(missing_ok=True)
                continue
            body = json.dumps({"files": files}, sort_keys=True, indent=1)
            try:
                if path.read_text(encoding="utf-8") == body:
                    continue
            except OSError:
                pass
            tmp = d / (MANIFEST + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, path)

    def ready_files(self, name: str) -> list[FoundFile]:
        """Files of the model that are in its manifest and unchanged since."""
        listed = self.manifest(name)
        return [f for f in self.files(name) if _matches(listed.get(f.path), f.size, f.mtime)]

    def _disk(self):
        """Usage of the volume (of the nearest existing directory up the path)."""
        p = self.data_dir
        while not p.exists() and p.parent != p:
            p = p.parent
        return shutil.disk_usage(p)

    def free_bytes(self) -> int:
        return self._disk().free

    def reserve_bytes(self) -> int:
        """Bytes uploads leave free on the volume."""
        return max(DISK_RESERVE, self._disk().total * DISK_RESERVE_PCT // 100)

    # ---- the drop folder ------------------------------------------------------------

    def adopt_loose(self, settle_s: float, now: float) -> list[tuple[Path, Path]]:
        """Moves GGUF files dropped straight into models/ or models/local/ into models/local/<stem>/, once they
        have not changed for `settle_s` (a copy in progress is left alone). A file whose target exists stays."""
        moved = []
        for folder in (self.models_dir, self.local_dir):
            if not folder.is_dir():
                continue
            for f in sorted(folder.iterdir()):
                if _hidden(f.name) or f.is_symlink() or not f.is_file() or not f.name.lower().endswith(".gguf"):
                    continue
                try:
                    if now - f.stat().st_mtime < settle_s:
                        continue
                except OSError:
                    continue
                target = self.local_dir / suggest_name(f.name) / f.name
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(f, target)
                except FileNotFoundError:  # a scan in another process moved it first
                    continue
                moved.append((f, target))
        return moved

    def remove(self, name: str, paths: list[str]) -> None:
        """Deletes files of a model; an emptied model directory goes too."""
        for p in paths:
            self.path_of(name, p).unlink(missing_ok=True)
        base = self.model_dir(name)
        for dirpath, _, _ in sorted(os.walk(base, topdown=False), key=lambda w: -len(w[0])):
            try:
                Path(dirpath).rmdir()  # only empty ones
            except OSError:
                pass

    # ---- the HfClient calls (app.catalog.sources) -------------------------------------

    async def list_files(self, repo: str) -> list[HfFile]:
        d = self._repo_dir(repo)
        return [HfFile(f.path, f.size) for f in await asyncio.to_thread(self.ready_files, d.name)]

    async def gguf_files(self, repo: str) -> list[GgufChoice]:
        return group_gguf(await self.list_files(repo))

    def _file(self, repo: str, file: str) -> Path:
        self._repo_dir(repo)
        path = self.path_of(name_of(repo), file)
        try:
            st = path.lstat()
        except OSError as e:
            raise LocalNotFound(f"{repo}/{file}") from e
        if path.is_symlink() or not _matches(self.manifest(name_of(repo)).get(file), st.st_size, st.st_mtime):
            raise LocalNotFound(f"{repo}/{file}: not ready")
        return path

    async def gguf_meta(self, repo: str, file: str) -> GgufMeta:
        path = self._file(repo, file)
        return await asyncio.to_thread(_read_header, path, parse_gguf_header)

    async def gguf_inspect(self, repo: str, shards: list[HfFile]) -> tuple[GgufMeta, GgufLayout | None]:
        paths = [self._file(repo, s.path) for s in shards]

        def read() -> tuple[GgufMeta, GgufLayout]:
            headers: list[GgufHeader] = [_read_header(p, parse_gguf) for p in paths]
            meta = headers[0].meta
            if meta is None:
                raise GgufInvalid("GGUF header lacks required architecture keys")
            return meta, gguf_layout([(h, p.stat().st_size) for h, p in zip(headers, paths, strict=True)], meta.n_layers)

        return await asyncio.to_thread(read)

    async def vllm_info(self, repo: str):
        raise LocalNotFound(f"{repo}: own model files run on llama.cpp only")
