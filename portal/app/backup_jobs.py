"""Run the `postgres-backup` CronJob now, and read its schedule and state (admin «Резервные копии»).

The daily backup is a Kubernetes CronJob (main/manifests/common-20-postgres.yaml.tmpl, script main/backup/pg-backup.sh).
«Сделать копию сейчас» does what `kubectl create job --from=cronjob/postgres-backup` does (README, «Копии Postgres»):
it creates a Job from the CronJob's own `jobTemplate`, owned by the CronJob. So the manual run is the very same image,
script, environment, volume and node as the scheduled one; nothing is copied into the portal.

One backup at a time: the CronJob has `concurrencyPolicy: Forbid`, but a hand-made Job is not covered by it, so
`run_now` refuses (`AlreadyRunning`) while any Job of the CronJob is unfinished. The Job name carries the second of
creation, so two requests that both see "nothing running" meet at the API server (409 AlreadyExists) as well.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from app.k8s import K8sClient, K8sUnavailable

CRONJOB = "postgres-backup"
MANUAL_TTL_S = 86400  # a finished manual Job disappears after a day (the CronJob's own history is capped by its limits)
KEEP_ENV = "BACKUP_KEEP"


class AlreadyRunning(Exception):
    """A backup Job is unfinished."""


class BackupJobError(Exception):
    """The Kubernetes API refused or failed; the message is for the admin."""


@dataclass(frozen=True)
class Schedule:
    cron: str  # five fields, UTC
    keep: int | None  # BACKUP_KEEP of the Job template
    suspended: bool
    last_schedule_at: datetime | None


@dataclass(frozen=True)
class JobInfo:
    name: str
    state: str  # running | succeeded | failed
    started_at: datetime | None
    finished_at: datetime | None
    manual: bool


def _conditions(job) -> set[str]:
    return {c.type for c in (job.status.conditions or []) if c.status == "True"} if job.status else set()


def _owned(job) -> bool:
    return any(o.kind == "CronJob" and o.name == CRONJOB for o in (job.metadata.owner_references or []))


def _job_info(job) -> JobInfo:
    done = _conditions(job)
    state = "succeeded" if "Complete" in done else "failed" if "Failed" in done else "running"
    status = job.status
    return JobInfo(
        name=job.metadata.name,
        state=state,
        started_at=(status.start_time if status and status.start_time else job.metadata.creation_timestamp),
        finished_at=status.completion_time if status and status.completion_time else None,
        manual=(job.metadata.annotations or {}).get("cronjob.kubernetes.io/instantiate") == "manual",
    )


def _keep(cronjob) -> int | None:
    try:
        containers = cronjob.spec.job_template.spec.template.spec.containers
        for env in containers[0].env or []:
            if env.name == KEEP_ENV:
                return int(env.value)
    except (AttributeError, IndexError, TypeError, ValueError):
        pass
    return None


class BackupJobs:
    def __init__(self, k8s: K8sClient):
        self._k8s = k8s
        self._lock = asyncio.Lock()

    async def _batch(self):
        await self._k8s._apis()
        return client.BatchV1Api(self._k8s._api_client)

    async def _read_cronjob(self, batch):
        try:
            return await self._k8s._call(batch.read_namespaced_cron_job, CRONJOB, self._k8s.namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    async def _jobs(self, batch) -> list[JobInfo]:
        result = await self._k8s._call(batch.list_namespaced_job, self._k8s.namespace)
        jobs = [_job_info(j) for j in result.items if _owned(j)]
        return sorted(jobs, key=lambda j: j.started_at or datetime.min.replace(tzinfo=UTC), reverse=True)

    async def state(self) -> tuple[Schedule | None, list[JobInfo]] | None:
        """The CronJob's schedule and its Jobs (newest first); None when Kubernetes cannot be asked."""
        try:
            batch = await self._batch()
            cronjob = await self._read_cronjob(batch)
            if cronjob is None:
                return None
            jobs = await self._jobs(batch)
        except (K8sUnavailable, ApiException):
            return None
        schedule = Schedule(
            cron=cronjob.spec.schedule,
            keep=_keep(cronjob),
            suspended=bool(cronjob.spec.suspend),
            last_schedule_at=cronjob.status.last_schedule_time if cronjob.status else None,
        )
        return schedule, jobs

    async def run_now(self) -> str:
        """Creates a Job from the CronJob's template; its name. AlreadyRunning, BackupJobError, K8sUnavailable."""
        async with self._lock:
            batch = await self._batch()
            try:
                cronjob = await self._read_cronjob(batch)
                if cronjob is None:
                    raise BackupJobError(f"В кластере нет задания {CRONJOB}")
                if any(j.state == "running" for j in await self._jobs(batch)):
                    raise AlreadyRunning
                name = f"{CRONJOB}-manual-{datetime.now(UTC):%Y%m%d-%H%M%S}"
                spec: dict[str, Any] = self._k8s._api_client.sanitize_for_serialization(cronjob.spec.job_template.spec)
                spec["ttlSecondsAfterFinished"] = MANUAL_TTL_S
                body = {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {
                        "name": name,
                        "namespace": self._k8s.namespace,
                        "labels": {"app.kubernetes.io/part-of": "nsu-llm"},
                        "annotations": {"cronjob.kubernetes.io/instantiate": "manual"},
                        "ownerReferences": [
                            {
                                "apiVersion": "batch/v1",
                                "kind": "CronJob",
                                "name": CRONJOB,
                                "uid": cronjob.metadata.uid,
                                "controller": True,
                            }
                        ],
                    },
                    "spec": spec,
                }
                await self._k8s._call(batch.create_namespaced_job, self._k8s.namespace, body=body)
            except ApiException as exc:
                if exc.status == 409:
                    raise AlreadyRunning from exc
                raise BackupJobError(f"Kubernetes отказал: {exc.status} {exc.reason}") from exc
            return name
