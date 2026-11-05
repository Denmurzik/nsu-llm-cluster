"""In-memory stand-in for app.k8s.K8sClient, for tests of controllers and APIs."""

from dataclasses import dataclass, replace
from datetime import datetime

from app.k8s import (
    A_GROUP_HASH,
    A_GROUP_MEMBERS,
    A_SPEC_HASH,
    L_ACCEL,
    L_DEPLOYMENT,
    L_NODE_DEPLOYMENT,
    L_VRAM,
    Endpoint,
    JobInfo,
    K8sUnavailable,
    NodeInfo,
    OwnedWorkload,
    PodInfo,
)


@dataclass
class FakePod:
    info: PodInfo
    port: int = 18080
    model_name: str | None = None
    max_input_tokens: int | None = None
    spec_hash: str | None = None
    terminating: bool = False


class FakeK8s:
    def __init__(self):
        self.unavailable = False
        self.nodes: dict[str, NodeInfo] = {}
        self.pods: dict[str, FakePod] = {}
        self.workloads: dict[tuple[str, str], dict] = {}  # (kind, name) -> manifest
        self.logs: dict[str, str] = {}
        self.applied: list[dict] = []  # every apply() call, in order
        self.deleted: list[tuple[str, str]] = []  # every delete() call, in order
        self.deleted_nodes: list[str] = []
        self.deleted_node_passwords: list[str] = []
        self.tainted_nodes: list[str] = []  # every add_class_taint() call that changed a node
        self.deleted_pods: list[str] = []
        self.jobs: list[dict] = []  # every create_job() call (Wake-on-LAN relays, models cleanups)
        self.job_names: list[str] = []  # the name each of `jobs` got
        # job name -> (active, succeeded, failed) as read_job() reports it; a Job not listed here is running
        self.job_states: dict[str, tuple[int, int, int]] = {}
        self.job_conditions: dict[str, tuple[str, str]] = {}  # job name -> (condition, reason) as read_job() reports it
        self.job_logs: dict[str, str] = {}  # job name -> its pod's log
        self.agent_token: str | None = None  # content of k3s server/agent-token
        self.heartbeats: dict[str, datetime] = {}  # node name -> last kubelet lease renewal
        self.heartbeats_unavailable = False  # only the lease listing fails, the node list still works
        self.force_deleted_pods: list[str] = []
        # (deployment, accel) -> spec hash of the rpc DaemonSet's current template: an rpc pod added without a
        # spec_hash of its own was created from it (tests of the workload controller fill it from the builder)
        self.rpc_templates: dict[tuple[str, str], str] = {}
        self.config_maps: dict[str, dict[str, str]] = {}
        self.config_map_writes: list[str] = []  # every put_config_map() call, in order
        self.restarted: list[str] = []  # every restart_deployment() call, in order
        self.missing_deployments: set[str] = set()  # restart_deployment() answers False for these

    def _check(self):
        if self.unavailable:
            raise K8sUnavailable("fake: cluster unavailable")

    # ---- test setup helpers --------------------------------------------------------

    def add_node(
        self,
        name: str,
        accel: str | None = None,
        internal_ip: str = "10.0.0.1",
        external_ip: str | None = None,
        ready: bool = True,
        vram_mib: int | None = None,
        mem_mib: int | None = 16000,
        deployment: str | None = None,
        is_main: bool = False,
        ram_mib: int | None = None,
    ) -> NodeInfo:
        node = NodeInfo(
            name, accel, internal_ip, external_ip, ready, "v1.36.5+k3s1", is_main, vram_mib, mem_mib, deployment, ram_mib
        )
        self.nodes[name] = node
        return node

    def add_pod(
        self,
        name: str,
        node_name: str,
        role: str,
        deployment: str,
        ready: bool = True,
        port: int = 18080,
        model_name: str | None = None,
        max_input_tokens: int | None = None,
        spec_hash: str | None = None,
        phase: str = "Running",
        restarts: int = 0,
        message: str | None = None,
        terminating: bool = False,
        workload: str | None = None,
        started_at: float | None = None,
    ) -> FakePod:
        pod = FakePod(
            PodInfo(name, node_name, role, deployment, phase, ready, restarts, message, workload, started_at, port),
            port,
            model_name,
            max_input_tokens,
            spec_hash,
            terminating,
        )
        self.pods[name] = pod
        return pod

    # ---- K8sClient interface ---------------------------------------------------------

    async def aclose(self) -> None:
        return None

    async def available(self) -> bool:
        return not self.unavailable

    async def list_nodes(self) -> list[NodeInfo]:
        self._check()
        return list(self.nodes.values())

    async def node_heartbeats(self) -> dict[str, datetime]:
        self._check()
        if self.heartbeats_unavailable:
            raise K8sUnavailable("fake: leases unavailable")
        return dict(self.heartbeats)

    async def patch_node_labels(self, name: str, labels: dict[str, str | None]) -> None:
        self._check()
        node = self.nodes[name]
        changes = {}
        if L_ACCEL in labels:
            changes["accel"] = labels[L_ACCEL]
        if L_NODE_DEPLOYMENT in labels:  # the node's deployment (k8s.L_NODE_DEPLOYMENT); the old llm.nsu/deployment is ignored
            changes["deployment"] = labels[L_NODE_DEPLOYMENT]
        if L_VRAM in labels:
            changes["vram_mib"] = int(labels[L_VRAM]) if labels[L_VRAM] is not None else None
        self.nodes[name] = replace(node, **changes)

    async def add_class_taint(self, name: str) -> None:
        self._check()
        if not self.nodes[name].class_taint:
            self.tainted_nodes.append(name)
            self.nodes[name] = replace(self.nodes[name], class_taint=True)

    async def delete_node(self, name: str) -> bool:
        self._check()
        self.deleted_nodes.append(name)
        return self.nodes.pop(name, None) is not None

    async def delete_node_password(self, name: str) -> bool:
        self._check()
        self.deleted_node_passwords.append(name)
        return True

    async def list_endpoints(self, role: str, deployment: str | None = None) -> list[Endpoint]:
        self._check()
        endpoints = []
        for pod in self.pods.values():
            info, node = pod.info, self.nodes.get(pod.info.node_name)
            if (
                info.role != role
                or (deployment and info.deployment != deployment)
                or not info.ready
                or pod.terminating
                or node is None
            ):
                continue
            ip = node.internal_ip if node.is_main else node.external_ip or node.internal_ip
            spec_hash = pod.spec_hash
            if spec_hash is None and role == "rpc":
                spec_hash = self.rpc_templates.get((info.deployment, node.accel))
            endpoints.append(
                Endpoint(
                    node.name, ip, pod.port, role, info.deployment, pod.model_name, pod.max_input_tokens, node.accel,
                    spec_hash, info.name, info.started_at, info.workload,
                )
            )
        return endpoints

    async def list_pods(self, deployment: str) -> list[PodInfo]:
        self._check()
        return [p.info for p in self.pods.values() if p.info.deployment == deployment]

    async def delete_pod(self, name: str, grace_period_seconds: int | None = None) -> None:
        """The pod disappears; a test that wants the DaemonSet's replacement adds it again. On a NotReady node it
        stays Terminating (its kubelet cannot confirm), unless force-deleted with `grace_period_seconds=0`."""
        self._check()
        self.deleted_pods.append(name)
        if grace_period_seconds == 0:
            self.force_deleted_pods.append(name)
        self._remove_pod(name, force=grace_period_seconds == 0)

    def _remove_pod(self, name: str, force: bool) -> None:
        pod = self.pods.get(name)
        if pod is None:
            return
        node = self.nodes.get(pod.info.node_name)
        if not force and node is not None and not node.ready:
            pod.terminating = True
        else:
            del self.pods[name]

    async def pod_log_tail(self, name: str, lines: int = 50) -> str:
        self._check()
        return "\n".join(self.logs.get(name, "").splitlines()[-lines:])

    async def list_owned(self) -> list[OwnedWorkload]:
        self._check()
        owned = []
        for (kind, name), manifest in self.workloads.items():
            labels = manifest["metadata"].get("labels") or {}
            if L_DEPLOYMENT not in labels:
                continue
            annotations = ((manifest.get("spec") or {}).get("template") or {}).get("metadata", {}).get("annotations") or {}
            owned.append(
                OwnedWorkload(
                    kind, name, labels[L_DEPLOYMENT], annotations.get(A_SPEC_HASH, ""), annotations.get(A_GROUP_HASH),
                    annotations.get(A_GROUP_MEMBERS),
                )
            )
        return owned

    async def create_job(self, manifest: dict) -> str:
        self._check()
        if manifest["kind"] != "Job":
            raise ValueError(f"not a Job: {manifest['kind']}")
        self.jobs.append(manifest)
        name = f"{manifest['metadata'].get('generateName', 'job-')}{len(self.jobs):05d}"
        self.job_names.append(name)
        return name

    def finish_job(self, name: str, ok: bool = True, log: str = "") -> None:
        """Test helper: the Job ended (succeeded or failed) with this pod log."""
        self.job_states[name] = (0, 1, 0) if ok else (0, 0, 1)
        self.job_logs[name] = log

    def kill_job(self, name: str, reason: str = "DeadlineExceeded") -> None:
        """Test helper: the Job hit its deadline: Failed condition, no pod counted as failed, no log."""
        self.job_states[name] = (0, 0, 0)
        self.job_conditions[name] = ("Failed", reason)
        self.job_logs[name] = ""

    async def read_job(self, name: str) -> JobInfo | None:
        self._check()
        if name not in self.job_names:
            return None
        active, succeeded, failed = self.job_states.get(name, (1, 0, 0))
        condition, reason = self.job_conditions.get(name, ("", ""))
        return JobInfo(name, active, succeeded, failed, condition, reason)

    async def job_log(self, name: str, lines: int = 50) -> str:
        self._check()
        return "\n".join(self.job_logs.get(name, "").splitlines()[-lines:])

    async def apply(self, manifest: dict) -> None:
        self._check()
        if manifest["kind"] not in ("DaemonSet", "Deployment"):
            raise ValueError(f"unsupported kind: {manifest['kind']}")
        self.applied.append(manifest)
        self.workloads[(manifest["kind"], manifest["metadata"]["name"])] = manifest

    async def delete(self, kind: str, name: str) -> None:
        """The object goes; the pods of a Deployment go with it (Terminating on a NotReady node, see delete_pod)."""
        self._check()
        self.deleted.append((kind, name))
        self.workloads.pop((kind, name), None)
        if kind == "Deployment":
            for pod in [p for p, fp in self.pods.items() if fp.info.workload == name or p.startswith(f"{name}-")]:
                self._remove_pod(pod, force=False)

    async def restart_deployment(self, name: str) -> bool:
        self._check()
        self.restarted.append(name)
        return name not in self.missing_deployments

    async def put_config_map(self, name: str, data: dict[str, str]) -> None:
        self._check()
        self.config_map_writes.append(name)
        self.config_maps[name] = dict(data)

    def read_token_file(self, path: str) -> str | None:
        return self.agent_token
