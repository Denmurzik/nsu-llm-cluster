"""Summary of the daily Postgres dumps for the admin «Состояние» page.

The `postgres-backup` CronJob (main/manifests/common-20-postgres.yaml.tmpl, script main/backup/pg-backup.sh) writes
`<database>-<UTC time %Y%m%dT%H%M%SZ>.dump` per database into BACKUP_DIR. A set (one run) counts only when it holds
a dump of every database: a run that stopped half way is not a backup.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DATABASES = ("portal", "litellm")
_FILE = re.compile(r"(?P<db>[a-z]+)-(?P<ts>\d{8}T\d{6}Z)\.dump")


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
    last_at = datetime.strptime(last, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return {
        "available": True,
        "count": len(complete),
        "last_at": last_at.isoformat(),
        "last_age_s": max(0, int((now - last_at).total_seconds())),
        "last_size_bytes": sum(sets[last].values()),
    }
