"""Postgres dumps of the main server: the summary for «Состояние» and the files for the admin «Резервные копии».

The `postgres-backup` CronJob (main/manifests/common-20-postgres.yaml.tmpl, script main/backup/pg-backup.sh) writes
`<database>-<UTC time %Y%m%dT%H%M%SZ>.dump` per database into BACKUP_DIR. A set (one run) counts only when it holds
a dump of every database: a run that stopped half way is not a backup. `pg-restore.sh` keeps a safety copy of the
databases it replaces in `pre-restore-<time>/<database>.dump`.

Names the API accepts (everything else is refused, so `..`, slashes and absolute paths never reach the file system):
- an entry: `20261103T211700Z` (a set) or `pre-restore-20261103T211700Z` (a safety copy);
- a file:  `portal-20261103T211700Z.dump` (of a set) or `pre-restore-20261103T211700Z.portal.dump` (of a safety copy).
A `<file>.sha256` beside a dump (first token = the digest, as `sha256sum` writes it) shows up as the file's checksum.
"""

import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DATABASES = ("portal", "litellm")
KIND_SET = "set"
KIND_PRE_RESTORE = "pre-restore"
_TS = r"\d{8}T\d{6}Z"
_FILE = re.compile(rf"(?P<db>[a-z]+)-(?P<ts>{_TS})\.dump")
_PRE_DIR = re.compile(rf"pre-restore-(?P<ts>{_TS})")
_PRE_FILE = re.compile(rf"pre-restore-(?P<ts>{_TS})\.(?P<db>[a-z]+)\.dump")
_SET_ID = re.compile(_TS)
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
RESTORE_COMMAND = "docker exec -it llm-main /opt/llm/main/backup/pg-restore.sh {ts}"


def _at(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def summarize(directory: str | Path, now: datetime) -> dict[str, Any]:
    """{"available", "count" (complete sets), "last_at" (ISO, UTC), "last_age_s", "last_size_bytes" (the newest set)}."""
    directory = Path(directory)
    sets: dict[str, dict[str, int]] = {}
    try:
        for entry in directory.iterdir():
            m = _FILE.fullmatch(entry.name)
            if m and m["db"] in DATABASES and entry.is_file():
                sets.setdefault(m["ts"], {})[m["db"]] = entry.stat().st_size
    except OSError:
        return {"available": False, "count": 0, "last_at": None, "last_age_s": None, "last_size_bytes": None}
    complete = sorted(ts for ts, dbs in sets.items() if set(dbs) == set(DATABASES))
    if not complete:
        return {"available": True, "count": 0, "last_at": None, "last_age_s": None, "last_size_bytes": None}
    last = complete[-1]
    last_at = _at(last)
    return {
        "available": True,
        "count": len(complete),
        "last_at": last_at.isoformat(),
        "last_age_s": max(0, int((now - last_at).total_seconds())),
        "last_size_bytes": sum(sets[last].values()),
    }


@dataclass(frozen=True)
class BackupFile:
    name: str  # what the download takes
    db: str
    size_bytes: int
    sha256: str | None = None


@dataclass(frozen=True)
class BackupEntry:
    name: str  # what the delete takes
    kind: str  # KIND_SET or KIND_PRE_RESTORE
    at: datetime
    complete: bool  # a dump of every database is there
    files: list[BackupFile] = field(default_factory=list)

    @property
    def size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def restore_command(self) -> str | None:
        """The exact pg-restore.sh call; only a complete set of the backup directory can be restored by name."""
        if self.kind != KIND_SET or not self.complete:
            return None
        return RESTORE_COMMAND.format(ts=self.name)


def _checksum(path: Path) -> str | None:
    try:
        with open(f"{path}.sha256", encoding="utf-8", errors="replace") as fh:
            token = fh.read(256).split(None, 1)
    except OSError:
        return None
    return token[0].lower() if token and _SHA256.fullmatch(token[0]) else None


def _file(path: Path, name: str, db: str) -> BackupFile | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        return BackupFile(name=name, db=db, size_bytes=path.stat().st_size, sha256=_checksum(path))
    except OSError:
        return None


def list_entries(directory: str | Path) -> list[BackupEntry] | None:
    """Sets and safety copies, newest first; None when the directory cannot be read."""
    directory = Path(directory)
    sets: dict[str, list[BackupFile]] = {}
    pre: dict[str, list[BackupFile]] = {}
    try:
        children = list(directory.iterdir())
    except OSError:
        return None
    for child in children:
        m = _FILE.fullmatch(child.name)
        if m and m["db"] in DATABASES:
            f = _file(child, child.name, m["db"])
            if f:
                sets.setdefault(m["ts"], []).append(f)
            continue
        m = _PRE_DIR.fullmatch(child.name)
        if m and child.is_dir() and not child.is_symlink():
            for db in DATABASES:
                f = _file(child / f"{db}.dump", f"pre-restore-{m['ts']}.{db}.dump", db)
                if f:
                    pre.setdefault(m["ts"], []).append(f)
            pre.setdefault(m["ts"], [])
    entries = [
        BackupEntry(ts, KIND_SET, _at(ts), {f.db for f in files} == set(DATABASES), sorted(files, key=lambda f: f.db))
        for ts, files in sets.items()
    ] + [
        BackupEntry(
            f"pre-restore-{ts}", KIND_PRE_RESTORE, _at(ts), {f.db for f in files} == set(DATABASES),
            sorted(files, key=lambda f: f.db),
        )
        for ts, files in pre.items()
    ]
    return sorted(entries, key=lambda e: (e.at, e.name), reverse=True)


def disk_usage(directory: str | Path) -> dict[str, int] | None:
    """{"free_bytes", "total_bytes"} of the volume that holds the copies; None when it cannot be read."""
    try:
        usage = shutil.disk_usage(directory)
    except OSError:
        return None
    return {"free_bytes": usage.free, "total_bytes": usage.total}


def resolve_file(directory: str | Path, name: str) -> Path | None:
    """The path of the dump `name` (a file name, see the module doc); None for an unknown or malformed name."""
    directory = Path(directory)
    m = _FILE.fullmatch(name)
    if m and m["db"] in DATABASES:
        path = directory / name
    else:
        m = _PRE_FILE.fullmatch(name)
        if not (m and m["db"] in DATABASES):
            return None
        path = directory / f"pre-restore-{m['ts']}" / f"{m['db']}.dump"
    try:
        return path if path.is_file() and not path.is_symlink() else None
    except OSError:
        return None


def delete_entry(directory: str | Path, name: str) -> list[str] | None:
    """Deletes the set (all its dumps and checksums) or the safety copy `name`; the names of the removed files,
    None when there is no such entry or the name is malformed."""
    directory = Path(directory)
    if _SET_ID.fullmatch(name):
        dumps = [directory / f"{db}-{name}.dump" for db in DATABASES]
        dumps = [p for p in dumps if p.is_file() and not p.is_symlink()]
        if not dumps:
            return None
        for path in dumps:
            path.unlink()
            path.with_name(path.name + ".sha256").unlink(missing_ok=True)
        return [p.name for p in dumps]
    m = _PRE_DIR.fullmatch(name)
    if m:
        path = directory / name
        if not path.is_dir() or path.is_symlink():
            return None
        names = sorted(p.name for p in path.iterdir() if p.name.endswith(".dump"))
        shutil.rmtree(path)
        return [f"{name}.{n[: -len('.dump')]}.dump" for n in names]
    return None
