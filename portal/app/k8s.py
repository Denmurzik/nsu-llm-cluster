"""Kubernetes client wrapper (kubernetes_asyncio): nodes, endpoints, pods, owned workloads."""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException

log = logging.getLogger(__name__)

MAIN_NODE = "llm-main"
REQUEST_TIMEOUT = 15

L_ACCEL = "llm.nsu/accel"
L_VRAM = "llm.nsu/vram-mib"
L_RAM = "llm.nsu/ram-mib"  # memory the node's host (WSL) gives it, set by the node at registration
L_DEPLOYMENT = "llm.nsu/deployment"
L_ROLE = "llm.nsu/role"
L_NAME = "app.kubernetes.io/name"  # the workload object a pod belongs to
A_PORT = "llm.nsu/port"
A_MODEL = "llm.nsu/model-name"
A_MAX_TOKENS = "llm.nsu/max-input-tokens"
A_BACKEND_KEY = "llm.nsu/backend-key"  # fingerprint of the backend key the pods were started with
A_SPEC_HASH = "llm.nsu/spec-hash"
A_GROUP_HASH = "llm.nsu/group-hash"
A_GROUP_MEMBERS = "llm.nsu/group-members"  # split leader: comma-separated node names of its group, leader first
# Class nodes carry this taint (set by `k3s agent --node-taint`, added by the workload controller to nodes that joined
# without it), so the k3s system pods (coredns, local-path-provisioner) never leave llm-main.
T_CLASS = "llm.nsu/class"
CLASS_TAINT = {"key": T_CLASS, "value": "true", "effect": "NoSchedule"}

_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}
_NETWORK_ERRORS = (aiohttp.ClientError, OSError, asyncio.TimeoutError)


class K8sUnavailable(Exception):
    """The cluster cannot be reached (no kubeconfig, API down)."""


@dataclass
class NodeInfo:
    name: str
    accel: str | None
    internal_ip: str | None
    external_ip: str | None
    ready: bool
    kubelet_version: str | None
    is_main: bool
    vram_mib: int | None
    mem_mib: int | None
    deployment: str | None
    ram_mib: int | None = None  # label llm.nsu/ram-mib
    class_taint: bool = False


@dataclass
class Endpoint:
    node_name: str
    ip: str
    port: int
    role: str
    deployment: str | None
    model_name: str | None
    max_input_tokens: int | None
    accel: str | None
    spec_hash: str | None
    pod_name: str = ""
    started_at: float | None = None  # start of the running container (epoch seconds, the node's clock)
    workload: str | None = None  # the object the pod belongs to (L_NAME)


@dataclass
class PodInfo:
    name: str
    node_name: str | None
    role: str | None
    deployment: str | None
    phase: str | None
    ready: bool
    restarts: int
    message: str | None
    workload: str | None = None  # the object the pod belongs to (L_NAME)
    started_at: float | None = None  # start of the running container (epoch seconds), None while it does not run


@dataclass
class OwnedWorkload:
    kind: str  # "DaemonSet" | "Deployment"
    name: str
    deployment: str
    spec_hash: str
    group_hash: str | None
    group_members: str | None = None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mib(quantity: str | None) -> int | None:
    """Kubernetes memory quantity ("16314900Ki", "2Gi", "1048576") to whole MiB."""
    m = re.fullmatch(r"(\d+)([A-Za-z]*)", quantity or "")
    if not m or (m.group(2) and m.group(2) not in _UNITS):
        return None
    return int(m.group(1)) * _UNITS.get(m.group(2), 1) // (1024**2)


def _is_ready(conditions) -> bool:
    return any(c.type == "Ready" and c.status == "True" for c in conditions or [])


def _has_class_taint(node) -> bool:
    taints = (node.spec.taints if node.spec else None) or []
    return any(t.key == T_CLASS and t.effect == CLASS_TAINT["effect"] for t in taints)


def _taint_body(t) -> dict:
    body = {"key": t.key, "value": t.value, "effect": t.effect}
    if t.time_added:
        body["timeAdded"] = t.time_added.isoformat().replace("+00:00", "Z")
    return {k: v for k, v in body.items() if v is not None}


def _node_info(node) -> NodeInfo:
    labels = node.metadata.labels or {}
    status = node.status
    addresses = {a.type: a.address for a in (status.addresses or [])}
    return NodeInfo(
        name=node.metadata.name,
        accel=labels.get(L_ACCEL),
        internal_ip=addresses.get("InternalIP"),
        external_ip=addresses.get("ExternalIP"),
        ready=_is_ready(status.conditions),
        kubelet_version=status.node_info.kubelet_version if status.node_info else None,
        is_main=node.metadata.name == MAIN_NODE,
        vram_mib=_int(labels.get(L_VRAM)),
        mem_mib=_mib((status.capacity or {}).get("memory")),
        deployment=labels.get(L_DEPLOYMENT),
        ram_mib=_int(labels.get(L_RAM)),
        class_taint=_has_class_taint(node),
    )


def _container_message(pod) -> str | None:
    statuses = pod.status.container_statuses or []
    if not statuses or statuses[0].state is None:
        return None
    state = statuses[0].state
    detail = state.waiting or state.terminated
    if detail is None:
        return None
    return ": ".join(p for p in (detail.reason, detail.message) if p) or None


def _started_at(pod) -> float | None:
    """When the pod's (first) container started running: a later time means it was restarted."""
    statuses = pod.status.container_statuses or []
    running = statuses[0].state.running if statuses and statuses[0].state else None
    return running.started_at.timestamp() if running is not None and running.started_at is not None else None


def _pod_info(pod) -> PodInfo:
    labels = pod.metadata.labels or {}
    statuses = pod.status.container_statuses or []
    return PodInfo(
        name=pod.metadata.name,
        node_name=pod.spec.node_name,
        role=labels.get(L_ROLE),
        deployment=labels.get(L_DEPLOYMENT),
        phase=pod.status.phase,
        ready=_is_ready(pod.status.conditions),
        restarts=sum(s.restart_count or 0 for s in statuses),
        message=_container_message(pod),
        workload=labels.get(L_NAME),
        started_at=_started_at(pod),
    )


def _template_annotations(workload) -> dict[str, str]:
    template = workload.spec.template if workload.spec else None
    return (template.metadata.annotations if template and template.metadata else None) or {}


_SUFFIX = {"DaemonSet": "daemon_set", "Deployment": "deployment"}


class K8sClient:
    def __init__(self, kubeconfig: str, namespace: str):
        self.kubeconfig = kubeconfig
        self.namespace = namespace
        self._api_client = None
        self._core = None
        self._apps = None
        self._lock = asyncio.Lock()

    async def _apis(self):
        """Lazily load the kubeconfig and build the API objects; failures are not cached."""
        if self._core is None:
            async with self._lock:
                if self._core is None:
                    try:
                        await config.load_kube_config(config_file=self.kubeconfig)
                    except Exception as exc:  # missing/invalid kubeconfig
                        raise K8sUnavailable(f"kubeconfig {self.kubeconfig}: {exc}") from exc
                    self._api_client = client.ApiClient()
                    self._apps = client.AppsV1Api(self._api_client)
                    self._core = client.CoreV1Api(self._api_client)
        return self._core, self._apps

    async def _call(self, fn, *args, **kwargs):
        try:
            return await fn(*args, _request_timeout=REQUEST_TIMEOUT, **kwargs)
        except _NETWORK_ERRORS as exc:
            raise K8sUnavailable(f"kubernetes API unreachable: {exc!r}") from exc

    async def aclose(self) -> None:
        if self._api_client is not None:
            await self._api_client.close()
        self._api_client = self._core = self._apps = None

    async def available(self) -> bool:
        try:
            core, _ = await self._apis()
            await self._call(core.list_node, limit=1)
        except (K8sUnavailable, ApiException):
            return False
        return True

    # ---- nodes -------------------------------------------------------------------

    async def list_nodes(self) -> list[NodeInfo]:
        core, _ = await self._apis()
        result = await self._call(core.list_node)
        return [_node_info(n) for n in result.items]

    async def node_heartbeats(self) -> dict[str, datetime]:
        """Node name -> last renewal of its kubelet lease (about every 10 s while the node lives).

        The Ready condition's own heartbeat is refreshed only every few minutes on a healthy node, the lease is
        the real one. Nodes whose lease is missing or never renewed are absent; an API error gives no heartbeats.
        """
        await self._apis()
        leases = client.CoordinationV1Api(self._api_client)
        try:
            result = await self._call(leases.list_namespaced_lease, "kube-node-lease")
        except ApiException as exc:
            log.warning("node heartbeats unavailable: %s", exc)
            return {}
        return {
            lease.metadata.name: lease.spec.renew_time
            for lease in result.items
            if lease.spec is not None and lease.spec.renew_time is not None
        }

    async def patch_node_labels(self, name: str, labels: dict[str, str | None]) -> None:
        """Merge-patch node labels; a None value removes the label."""
        core, _ = await self._apis()
        await self._call(core.patch_node, name, body={"metadata": {"labels": labels}})

    async def add_class_taint(self, name: str) -> None:
        """Adds `CLASS_TAINT` unless the node has it. A patch replaces the whole taint list, so it carries the read
        resourceVersion: a concurrent change (the node lifecycle controller's own taints) fails it with 409, the next
        pass retries."""
        core, _ = await self._apis()
        node = await self._call(core.read_node, name)
        if _has_class_taint(node):
            return
        taints = [_taint_body(t) for t in ((node.spec.taints if node.spec else None) or [])] + [dict(CLASS_TAINT)]
        body = {"metadata": {"resourceVersion": node.metadata.resource_version}, "spec": {"taints": taints}}
        await self._call(core.patch_node, name, body=body)

    async def delete_node(self, name: str) -> bool:
        core, _ = await self._apis()
        try:
            await self._call(core.delete_node, name)
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise
        return True

    # ---- pods --------------------------------------------------------------------

    async def list_endpoints(self, role: str, deployment: str | None = None) -> list[Endpoint]:
        core, _ = await self._apis()
        selector = f"{L_ROLE}={role}" + (f",{L_DEPLOYMENT}={deployment}" if deployment else "")
        pods = await self._call(core.list_namespaced_pod, self.namespace, label_selector=selector)
        nodes = {n.name: n for n in await self.list_nodes()}
        endpoints = []
        for pod in pods.items:
            labels = pod.metadata.labels or {}
            annotations = pod.metadata.annotations or {}
            node = nodes.get(pod.spec.node_name)
            port = _int(annotations.get(A_PORT))
            if (
                labels.get(L_ROLE) != role
                or (deployment and labels.get(L_DEPLOYMENT) != deployment)
                or pod.metadata.deletion_timestamp is not None
                or not _is_ready(pod.status.conditions)
                or node is None
                or port is None
            ):
                continue
            ip = node.external_ip or node.internal_ip
            if not ip:
                continue
            endpoints.append(
                Endpoint(
                    node_name=node.name,
                    ip=ip,
                    port=port,
                    role=role,
                    deployment=labels.get(L_DEPLOYMENT),
                    model_name=annotations.get(A_MODEL),
                    max_input_tokens=_int(annotations.get(A_MAX_TOKENS)),
                    accel=node.accel,
                    spec_hash=annotations.get(A_SPEC_HASH),
                    pod_name=pod.metadata.name,
                    started_at=_started_at(pod),
                    workload=labels.get(L_NAME),
                )
            )
        return endpoints

    async def list_pods(self, deployment: str) -> list[PodInfo]:
        core, _ = await self._apis()
        pods = await self._call(core.list_namespaced_pod, self.namespace, label_selector=f"{L_DEPLOYMENT}={deployment}")
        return [_pod_info(p) for p in pods.items]

    async def delete_pod(self, name: str, grace_period_seconds: int | None = None) -> None:
        """Deletes one pod (its DaemonSet starts a fresh one); an already gone pod is not an error.
        `grace_period_seconds=0` removes it at once, also from a down node whose kubelet cannot confirm."""
        core, _ = await self._apis()
        kwargs = {} if grace_period_seconds is None else {"grace_period_seconds": grace_period_seconds}
        try:
            await self._call(core.delete_namespaced_pod, name, self.namespace, **kwargs)
        except ApiException as exc:
            if exc.status != 404:
                raise

    async def pod_log_tail(self, name: str, lines: int = 50) -> str:
        """Last log lines; empty when the pod is gone or its container has not started."""
        core, _ = await self._apis()
        try:
            return await self._call(core.read_namespaced_pod_log, name, self.namespace, tail_lines=lines)
        except ApiException as exc:
            if exc.status in (400, 404):
                return ""
            raise

    # ---- owned workloads ---------------------------------------------------------

    async def list_owned(self) -> list[OwnedWorkload]:
        _, apps = await self._apis()
        owned = []
        for kind, lister in (("DaemonSet", apps.list_namespaced_daemon_set), ("Deployment", apps.list_namespaced_deployment)):
            result = await self._call(lister, self.namespace, label_selector=L_DEPLOYMENT)
            for obj in result.items:
                annotations = _template_annotations(obj)
                owned.append(
                    OwnedWorkload(
                        kind=kind,
                        name=obj.metadata.name,
                        deployment=(obj.metadata.labels or {}).get(L_DEPLOYMENT, ""),
                        spec_hash=annotations.get(A_SPEC_HASH, ""),
                        group_hash=annotations.get(A_GROUP_HASH),
                        group_members=annotations.get(A_GROUP_MEMBERS),
                    )
                )
        return owned

    @staticmethod
    def _suffix(kind: str) -> str:
        try:
            return _SUFFIX[kind]
        except KeyError:
            raise ValueError(f"unsupported kind: {kind}") from None

    async def apply(self, manifest: dict) -> None:
        """Read, then replace with the read resourceVersion; create when absent."""
        _, apps = await self._apis()
        suffix = self._suffix(manifest["kind"])
        name = manifest["metadata"]["name"]
        read = getattr(apps, f"read_namespaced_{suffix}")
        try:
            current = await self._call(read, name, self.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            await self._call(getattr(apps, f"create_namespaced_{suffix}"), self.namespace, body=manifest)
            return
        body = {**manifest, "metadata": {**manifest["metadata"], "resourceVersion": current.metadata.resource_version}}
        await self._call(getattr(apps, f"replace_namespaced_{suffix}"), name, self.namespace, body=body)

    async def create_job(self, manifest: dict) -> None:
        """Creates a batch/v1 Job (the Wake-on-LAN relay, builders.wake_job: generateName, collected by its TTL)."""
        await self._apis()
        batch = client.BatchV1Api(self._api_client)
        await self._call(batch.create_namespaced_job, self.namespace, body=manifest)

    async def put_config_map(self, name: str, data: dict[str, str]) -> None:
        """Creates or replaces a ConfigMap of the namespace (read, then replace with the read resourceVersion)."""
        core, _ = await self._apis()
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": self.namespace, "labels": {"app.kubernetes.io/part-of": "nsu-llm"}},
            "data": dict(data),
        }
        try:
            current = await self._call(core.read_namespaced_config_map, name, self.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            await self._call(core.create_namespaced_config_map, self.namespace, body=body)
            return
        body["metadata"]["resourceVersion"] = current.metadata.resource_version
        await self._call(core.replace_namespaced_config_map, name, self.namespace, body=body)

    async def delete(self, kind: str, name: str) -> None:
        _, apps = await self._apis()
        suffix = self._suffix(kind)
        try:
            await self._call(getattr(apps, f"delete_namespaced_{suffix}"), name, self.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    # ---- misc --------------------------------------------------------------------

    def read_token_file(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
