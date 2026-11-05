"""Run-now of the backup: a Job built from the postgres-backup CronJob, one at a time (app.backup_jobs)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from app.backup_jobs import AlreadyRunning, BackupJobError, BackupJobs
from app.k8s import K8sUnavailable

NS = "llm"


def cronjob(uid="uid-1", keep="14"):
    container = client.V1Container(
        name="backup",
        image="postgres:17-alpine",
        command=["sh", "/scripts/pg-backup.sh"],
        env=[client.V1EnvVar(name="BACKUP_DIR", value="/backups"), client.V1EnvVar(name="BACKUP_KEEP", value=keep)],
    )
    template = client.V1JobTemplateSpec(
        spec=client.V1JobSpec(
            backoff_limit=2,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app.kubernetes.io/name": "postgres-backup"}),
                spec=client.V1PodSpec(restart_policy="Never", containers=[container]),
            ),
        )
    )
    return client.V1CronJob(
        metadata=client.V1ObjectMeta(name="postgres-backup", uid=uid),
        spec=client.V1CronJobSpec(schedule="17 21 * * *", job_template=template, suspend=False),
        status=client.V1CronJobStatus(last_schedule_time=datetime(2026, 11, 3, 21, 17, tzinfo=UTC)),
    )


def job(name, conditions=(), owner="postgres-backup", manual=False, started=None):
    return client.V1Job(
        metadata=client.V1ObjectMeta(
            name=name,
            annotations={"cronjob.kubernetes.io/instantiate": "manual"} if manual else None,
            owner_references=[client.V1OwnerReference(api_version="batch/v1", kind="CronJob", name=owner, uid="u")],
            creation_timestamp=started or datetime(2026, 11, 3, 21, 17, tzinfo=UTC),
        ),
        status=client.V1JobStatus(
            start_time=started or datetime(2026, 11, 3, 21, 17, tzinfo=UTC),
            conditions=[client.V1JobCondition(type=c, status="True") for c in conditions] or None,
        ),
    )


class StubK8s:
    """What BackupJobs needs of K8sClient, over a dict of Jobs."""

    namespace = NS

    def __init__(self):
        self._api_client = None  # an ApiClient needs a running loop: set by the fixture
        self.cronjob = cronjob()
        self.jobs: dict[str, client.V1Job] = {}
        self.created: list[dict] = []
        self.unavailable = False
        self.create_error: ApiException | None = None
        self.delay = 0.0

    async def _apis(self):
        if self.unavailable:
            raise K8sUnavailable("down")

    async def _call(self, fn, *args, **kwargs):
        name = fn.__name__
        if name == "read_namespaced_cron_job":
            if self.cronjob is None:
                raise ApiException(status=404, reason="Not Found")
            return self.cronjob
        if name == "list_namespaced_job":
            return client.V1JobList(items=list(self.jobs.values()))
        if name == "create_namespaced_job":
            await asyncio.sleep(self.delay)
            if self.create_error:
                raise self.create_error
            body = kwargs["body"]
            if body["metadata"]["name"] in self.jobs:
                raise ApiException(status=409, reason="Conflict")
            self.created.append(body)
            self.jobs[body["metadata"]["name"]] = job(body["metadata"]["name"], manual=True, owner="postgres-backup")
            return None
        raise AssertionError(name)


@pytest.fixture
async def k8s():
    stub = StubK8s()
    stub._api_client = client.ApiClient()
    yield stub
    await stub._api_client.close()


async def test_run_now_creates_a_job_from_the_cronjob_template_owned_by_it(k8s):
    name = await BackupJobs(k8s).run_now()
    (body,) = k8s.created
    assert name == body["metadata"]["name"] and name.startswith("postgres-backup-manual-")
    assert body["metadata"]["annotations"] == {"cronjob.kubernetes.io/instantiate": "manual"}
    (owner,) = body["metadata"]["ownerReferences"]
    assert (owner["kind"], owner["name"], owner["uid"], owner["controller"]) == ("CronJob", "postgres-backup", "uid-1", True)
    pod = body["spec"]["template"]["spec"]
    assert pod["containers"][0]["command"] == ["sh", "/scripts/pg-backup.sh"]  # the scheduled script, as it is
    assert body["spec"]["backoffLimit"] == 2 and body["spec"]["ttlSecondsAfterFinished"] == 86400
    assert pod["restartPolicy"] == "Never"


async def test_a_second_run_while_one_is_running_is_refused(k8s):
    jobs = BackupJobs(k8s)
    await jobs.run_now()
    with pytest.raises(AlreadyRunning):
        await jobs.run_now()
    assert len(k8s.created) == 1


async def test_a_running_scheduled_job_blocks_a_manual_run(k8s):
    k8s.jobs["postgres-backup-29000"] = job("postgres-backup-29000")
    with pytest.raises(AlreadyRunning):
        await BackupJobs(k8s).run_now()
    assert k8s.created == []


async def test_finished_jobs_and_foreign_jobs_do_not_block(k8s):
    k8s.jobs["old-ok"] = job("old-ok", ["Complete"])
    k8s.jobs["old-bad"] = job("old-bad", ["Failed"])
    k8s.jobs["other"] = job("other", owner="something-else")
    await BackupJobs(k8s).run_now()
    assert len(k8s.created) == 1


async def test_concurrent_requests_make_exactly_one_job(k8s):
    k8s.delay = 0.05
    jobs = BackupJobs(k8s)
    results = await asyncio.gather(*(jobs.run_now() for _ in range(5)), return_exceptions=True)
    assert sum(isinstance(r, str) for r in results) == 1
    assert sum(isinstance(r, AlreadyRunning) for r in results) == 4
    assert len(k8s.created) == 1


async def test_two_portals_meeting_at_the_api_server_get_a_conflict(k8s):
    # another portal process created the job in the same second: the API server answers 409
    k8s.create_error = ApiException(status=409, reason="Conflict")
    with pytest.raises(AlreadyRunning):
        await BackupJobs(k8s).run_now()


async def test_run_now_errors(k8s):
    k8s.create_error = ApiException(status=403, reason="Forbidden")
    with pytest.raises(BackupJobError, match="403"):
        await BackupJobs(k8s).run_now()
    k8s.cronjob = None
    with pytest.raises(BackupJobError, match="postgres-backup"):
        await BackupJobs(k8s).run_now()
    k8s.unavailable = True
    with pytest.raises(K8sUnavailable):
        await BackupJobs(k8s).run_now()


async def test_state_reads_schedule_retention_and_jobs_newest_first(k8s):
    now = datetime(2026, 11, 4, tzinfo=UTC)
    k8s.jobs["a"] = job("a", ["Complete"], started=now - timedelta(days=2))
    k8s.jobs["b"] = job("b", ["Failed"], started=now - timedelta(days=1))
    k8s.jobs["c"] = job("c", manual=True, started=now)
    schedule, jobs = await BackupJobs(k8s).state()
    assert (schedule.cron, schedule.keep, schedule.suspended) == ("17 21 * * *", 14, False)
    assert schedule.last_schedule_at == datetime(2026, 11, 3, 21, 17, tzinfo=UTC)
    assert [(j.name, j.state, j.manual) for j in jobs] == [("c", "running", True), ("b", "failed", False), ("a", "succeeded", False)]


async def test_state_is_none_when_kubernetes_cannot_be_asked(k8s):
    k8s.unavailable = True
    assert await BackupJobs(k8s).state() is None
    k8s.unavailable = False
    k8s.cronjob = None
    assert await BackupJobs(k8s).state() is None
