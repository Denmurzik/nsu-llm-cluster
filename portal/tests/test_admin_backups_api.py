"""Admin «Резервные копии»: list, run now (one at a time), download (audited), delete, and who may do any of it."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.backup_jobs import AlreadyRunning, BackupJobError, JobInfo, Schedule
from app.k8s import K8sUnavailable
from app.models import AuditLog
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}
T1, T2, T3 = "20261101T031700Z", "20261102T031700Z", "20261103T211700Z"
WHEN = datetime(2026, 11, 3, 21, 17, tzinfo=UTC)


class FakeJobs:
    """BackupJobs without Kubernetes: one unfinished run at a time, like the real one."""

    def __init__(self):
        self.running = False
        self.down = False
        self.error: Exception | None = None
        self.started: list[str] = []
        self.keep = 14
        self.last: JobInfo | None = JobInfo("postgres-backup-29", "failed", WHEN, WHEN, False)
        self.delay = 0.0

    async def state(self):
        if self.down:
            return None
        jobs = ([JobInfo("postgres-backup-manual-x", "running", WHEN, None, True)] if self.running else []) + (
            [self.last] if self.last else []
        )
        return Schedule("17 21 * * *", self.keep, False, WHEN), jobs

    async def run_now(self):
        await asyncio.sleep(self.delay)
        if self.down:
            raise K8sUnavailable("down")
        if self.error:
            raise self.error
        if self.running:
            raise AlreadyRunning
        self.running = True
        self.started.append("postgres-backup-manual-20261104-100000")
        return self.started[-1]


@pytest.fixture
def settings(settings, tmp_path):
    settings.backup_dir = str(tmp_path / "backups")
    return settings


@pytest.fixture(autouse=True)
def jobs(app):
    app.state.backup_jobs = FakeJobs()
    return app.state.backup_jobs


@pytest.fixture
def directory(settings):
    from pathlib import Path

    d = Path(settings.backup_dir)
    d.mkdir()
    return d


def put(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def admin(client, app):
    return csrf_headers(await login(client, app, ADMIN))


async def audit_rows(app, action):
    async with app.state.sessionmaker() as db:
        return list((await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


async def test_every_endpoint_is_for_admins_only(client, app, directory):
    put(directory / f"portal-{T1}.dump")
    calls = [
        ("GET", "/api/admin/backups"),
        ("POST", "/api/admin/backups"),
        ("GET", f"/api/admin/backups/portal-{T1}.dump/download"),
        ("DELETE", f"/api/admin/backups/{T1}"),
    ]
    for method, url in calls:
        assert (await client.request(method, url)).status_code == 401, (method, url)
    h = csrf_headers(await login(client, app, USER))
    for method, url in calls:
        r = await client.request(method, url, headers=h)
        assert r.status_code == 403, (method, url)
    assert (directory / f"portal-{T1}.dump").exists()
    assert await audit_rows(app, "backup.download") == []


async def test_list_has_sets_disk_schedule_retention_and_the_job_state(client, app, directory, jobs):
    put(directory / f"portal-{T1}.dump", b"a" * 10)
    put(directory / f"litellm-{T1}.dump", b"b" * 20)
    put(directory / f"portal-{T2}.dump", b"c" * 5)  # half a set
    put(directory / f"pre-restore-{T3}" / "portal.dump", b"d" * 7)
    put(directory / f"pre-restore-{T3}" / "litellm.dump", b"e" * 8)
    h = await admin(client, app)
    r = await client.get("/api/admin/backups", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert [(e["name"], e["kind"], e["size_bytes"], e["complete"]) for e in body["items"]] == [
        (f"pre-restore-{T3}", "pre-restore", 15, True),
        (T2, "set", 5, False),
        (T1, "set", 30, True),
    ]
    first = body["items"][2]
    assert first["restore_command"] == f"docker exec -it llm-main /opt/llm/main/backup/pg-restore.sh {T1}"
    assert body["items"][0]["restore_command"] is None and body["items"][1]["restore_command"] is None
    assert [(f["name"], f["db"], f["size_bytes"], f["sha256"]) for f in first["files"]] == [
        (f"litellm-{T1}.dump", "litellm", 20, None),
        (f"portal-{T1}.dump", "portal", 10, None),
    ]
    assert body["disk"]["free_bytes"] > 0 and body["disk"]["total_bytes"] >= body["disk"]["free_bytes"]
    assert body["schedule"] == {
        "cron": "17 21 * * *",
        "timezone": "UTC",
        "suspended": False,
        "last_schedule_at": WHEN.isoformat(),
    }
    assert body["retention_keep"] == 14
    assert body["running"] is False
    assert body["last_run"]["state"] == "failed" and body["last_run"]["name"] == "postgres-backup-29"


async def test_list_says_when_kubernetes_or_the_directory_is_missing(client, app, jobs):
    jobs.down = True
    h = await admin(client, app)
    body = (await client.get("/api/admin/backups", headers=h)).json()
    assert body["available"] is False and body["items"] == [] and body["disk"] is None
    assert (body["schedule"], body["retention_keep"], body["running"], body["last_run"]) == (None, None, None, None)


async def test_run_now_starts_a_job_and_audits_it(client, app, directory, jobs):
    h = await admin(client, app)
    r = await client.post("/api/admin/backups", headers=h)
    assert r.status_code == 202 and r.json() == {"job": "postgres-backup-manual-20261104-100000"}
    (row,) = await audit_rows(app, "backup.run")
    assert (row.target_type, row.target, row.actor_email) == ("backup", "postgres-backup-manual-20261104-100000", "admin@g.nsu.ru")
    body = (await client.get("/api/admin/backups", headers=h)).json()
    assert body["running"] is True


async def test_a_second_run_while_one_is_in_progress_is_409(client, app, directory, jobs):
    h = await admin(client, app)
    assert (await client.post("/api/admin/backups", headers=h)).status_code == 202
    r = await client.post("/api/admin/backups", headers=h)
    assert r.status_code == 409 and r.json()["detail"] == "Копия уже делается: дождитесь её окончания"
    assert len(jobs.started) == 1
    assert len(await audit_rows(app, "backup.run")) == 1


async def test_concurrent_run_requests_start_one_backup(client, app, directory, jobs):
    jobs.delay = 0.05
    h = await admin(client, app)
    replies = await asyncio.gather(*(client.post("/api/admin/backups", headers=h) for _ in range(4)))
    assert sorted(r.status_code for r in replies) == [202, 409, 409, 409]
    assert len(jobs.started) == 1


async def test_run_now_failures_are_told_and_not_audited(client, app, directory, jobs):
    h = await admin(client, app)
    jobs.error = BackupJobError("Kubernetes отказал: 403 Forbidden")
    r = await client.post("/api/admin/backups", headers=h)
    assert r.status_code == 502 and "403" in r.json()["detail"]
    jobs.error = None
    jobs.down = True
    r = await client.post("/api/admin/backups", headers=h)
    assert r.status_code == 503 and "Kubernetes недоступен" in r.json()["detail"]
    assert await audit_rows(app, "backup.run") == []


async def test_run_now_needs_the_csrf_token(client, app, directory, jobs):
    await login(client, app, ADMIN)
    r = await client.post("/api/admin/backups")
    assert r.status_code == 403 and jobs.started == []


async def test_download_streams_the_dump_and_audits_it(client, app, directory):
    data = bytes(range(256)) * 400
    put(directory / f"portal-{T1}.dump", data)
    h = await admin(client, app)
    r = await client.get(f"/api/admin/backups/portal-{T1}.dump/download", headers=h)
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"] == "application/octet-stream"
    assert f"portal-{T1}.dump" in r.headers["content-disposition"] and "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    (row,) = await audit_rows(app, "backup.download")
    assert (row.target_type, row.target, row.details) == ("backup", f"portal-{T1}.dump", {"size_bytes": len(data)})
    assert row.actor_email == "admin@g.nsu.ru"


async def test_download_of_a_safety_copy(client, app, directory):
    put(directory / f"pre-restore-{T3}" / "litellm.dump", b"safety")
    h = await admin(client, app)
    r = await client.get(f"/api/admin/backups/pre-restore-{T3}.litellm.dump/download", headers=h)
    assert r.status_code == 200 and r.content == b"safety"


@pytest.mark.parametrize(
    "name",
    [
        f"portal-{T2}.dump",  # well-formed, not there
        "..%2f..%2fetc%2fpasswd",
        "%2e%2e%2fportal-20261101T031700Z.dump",
        f"..%2fportal-{T1}.dump",
        f"portal-{T1}.dump.sha256",
        "secret.txt",
        "pre-restore-20261103T211700Z%2fportal.dump",
    ],
)
async def test_download_refuses_names_that_are_not_dumps_and_audits_nothing(client, app, directory, name):
    put(directory / f"portal-{T1}.dump")
    put(directory / "secret.txt", b"secret")
    put(directory.parent / "outside.dump", b"outside")
    h = await admin(client, app)
    r = await client.get(f"/api/admin/backups/{name}/download", headers=h)
    assert r.status_code == 404, r.text
    assert b"secret" not in r.content and b"outside" not in r.content
    assert await audit_rows(app, "backup.download") == []


async def test_download_does_not_follow_a_symlink_out_of_the_directory(client, app, directory):
    import os

    outside = directory.parent / "outside.dump"
    put(outside, b"outside")
    try:
        os.symlink(outside, directory / f"portal-{T1}.dump")
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    h = await admin(client, app)
    r = await client.get(f"/api/admin/backups/portal-{T1}.dump/download", headers=h)
    assert r.status_code == 404


async def test_delete_removes_the_set_and_audits_it(client, app, directory):
    for ts in (T1, T2):
        put(directory / f"portal-{ts}.dump")
        put(directory / f"litellm-{ts}.dump")
    h = await admin(client, app)
    r = await client.delete(f"/api/admin/backups/{T1}", headers=h)
    assert r.status_code == 204
    assert sorted(p.name for p in directory.iterdir()) == [f"litellm-{T2}.dump", f"portal-{T2}.dump"]
    (row,) = await audit_rows(app, "backup.delete")
    assert (row.target, sorted(row.details["files"])) == (T1, [f"litellm-{T1}.dump", f"portal-{T1}.dump"])


async def test_delete_of_a_safety_copy(client, app, directory):
    put(directory / f"pre-restore-{T3}" / "portal.dump")
    h = await admin(client, app)
    assert (await client.delete(f"/api/admin/backups/pre-restore-{T3}", headers=h)).status_code == 204
    assert not (directory / f"pre-restore-{T3}").exists()


@pytest.mark.parametrize("name", [T2, "..", "..%2f..", f"portal-{T1}.dump", "pre-restore-..", "x"])
async def test_delete_refuses_unknown_and_malformed_names(client, app, directory, name):
    put(directory / f"portal-{T1}.dump")
    put(directory / f"litellm-{T1}.dump")
    h = await admin(client, app)
    r = await client.delete(f"/api/admin/backups/{name}", headers=h)
    assert r.status_code in (404, 405), r.text
    assert len(list(directory.iterdir())) == 2
    assert await audit_rows(app, "backup.delete") == []


async def test_delete_needs_the_csrf_token(client, app, directory):
    put(directory / f"portal-{T1}.dump")
    await login(client, app, ADMIN)
    assert (await client.delete(f"/api/admin/backups/{T1}")).status_code == 403
    assert (directory / f"portal-{T1}.dump").exists()
