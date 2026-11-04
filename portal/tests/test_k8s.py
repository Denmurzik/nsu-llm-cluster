import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from app.k8s import Endpoint, K8sClient, K8sUnavailable, NodeInfo, OwnedWorkload, PodInfo
from tests.fakes.k8s import FakeK8s

NS = "llm"


def _node(name, ready=True, labels=None, internal="172.17.0.3", external=None, memory="16384000Ki"):
    addresses = [client.V1NodeAddress(type="InternalIP", address=internal)]
    if external:
        addresses.append(client.V1NodeAddress(type="ExternalIP", address=external))
    return client.V1Node(
        metadata=client.V1ObjectMeta(name=name, labels=labels or {}),
        status=client.V1NodeStatus(
            addresses=addresses,
            capacity={"memory": memory, "cpu": "8"},
            conditions=[client.V1NodeCondition(type="Ready", status="True" if ready else "False")],
            node_info=client.V1NodeSystemInfo(
                kubelet_version="v1.36.5+k3s1",
                architecture="amd64",
                boot_id="b",
                container_runtime_version="containerd://2",
                kernel_version="6",
                kube_proxy_version="v1",
                machine_id="m",
                operating_system="linux",
                os_image="ubuntu",
                system_uuid="u",
            ),
        ),
    )


def _pod(
    name,
    node="n1",
    role="inference",
    deployment="dep",
    ready=True,
    terminating=False,
    annotations=None,
    phase="Running",
    container_status=None,
    labels=None,
):
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=name,
            labels={"llm.nsu/role": role, "llm.nsu/deployment": deployment, **(labels or {})},
            annotations=annotations
            if annotations is not None
            else {
                "llm.nsu/port": "18080",
                "llm.nsu/model-name": "gemma",
                "llm.nsu/max-input-tokens": "8192",
                "llm.nsu/spec-hash": "abc",
            },
            deletion_timestamp=datetime.now(timezone.utc) if terminating else None,
        ),
        spec=client.V1PodSpec(node_name=node, containers=[client.V1Container(name="main")]),
        status=client.V1PodStatus(
            phase=phase,
            conditions=[client.V1PodCondition(type="Ready", status="True" if ready else "False")],
            container_statuses=[container_status] if container_status else None,
        ),
    )


def _apps_obj(cls, spec_cls, name, deployment, spec_hash, group_hash=None, group_members=None):
    annotations = {"llm.nsu/spec-hash": spec_hash}
    if group_hash:
        annotations["llm.nsu/group-hash"] = group_hash
    if group_members:
        annotations["llm.nsu/group-members"] = group_members
    return cls(
        metadata=client.V1ObjectMeta(name=name, labels={"llm.nsu/deployment": deployment}),
        spec=spec_cls(
            selector=client.V1LabelSelector(match_labels={"a": "b"}),
            template=client.V1PodTemplateSpec(metadata=client.V1ObjectMeta(annotations=annotations)),
        ),
    )


@pytest.fixture
def apis(monkeypatch):
    core, apps = AsyncMock(), AsyncMock()
    monkeypatch.setattr(client, "CoreV1Api", lambda *_a, **_k: core)
    monkeypatch.setattr(client, "AppsV1Api", lambda *_a, **_k: apps)
    monkeypatch.setattr(client, "ApiClient", lambda *_a, **_k: MagicMock(close=AsyncMock()))
    monkeypatch.setattr("app.k8s.config.load_kube_config", AsyncMock())
    return core, apps


@pytest.fixture
def core(apis):
    return apis[0]


@pytest.fixture
def apps(apis):
    return apis[1]


@pytest.fixture
def k8s(apis):
    return K8sClient("/kubeconfig", NS)


def _set_cluster(core, nodes, pods):
    core.list_node.return_value = client.V1NodeList(items=nodes)
    core.list_namespaced_pod.return_value = client.V1PodList(items=pods)


async def test_list_endpoints_uses_external_ip_and_annotations(k8s, core):
    _set_cluster(
        core,
        [_node("n1", labels={"llm.nsu/accel": "nvidia"}, external="192.168.3.200")],
        [_pod("p1")],
    )
    eps = await k8s.list_endpoints("inference")
    assert eps == [
        Endpoint(
            node_name="n1",
            ip="192.168.3.200",
            port=18080,
            role="inference",
            deployment="dep",
            model_name="gemma",
            max_input_tokens=8192,
            accel="nvidia",
            spec_hash="abc",
            pod_name="p1",
        )
    ]
    assert core.list_namespaced_pod.await_args.args[0] == NS
    assert core.list_namespaced_pod.await_args.kwargs["label_selector"] == "llm.nsu/role=inference"


async def test_list_endpoints_falls_back_to_internal_ip(k8s, core):
    _set_cluster(core, [_node("n1", internal="172.17.0.3")], [_pod("p1")])
    (ep,) = await k8s.list_endpoints("inference")
    assert ep.ip == "172.17.0.3"
    assert ep.accel is None


async def test_list_endpoints_skips_not_ready_and_terminating(k8s, core):
    _set_cluster(
        core,
        [_node("n1"), _node("n2"), _node("n3")],
        [
            _pod("ok", node="n1"),
            _pod("notready", node="n2", ready=False),
            _pod("dying", node="n3", terminating=True),
        ],
    )
    eps = await k8s.list_endpoints("inference")
    assert [e.node_name for e in eps] == ["n1"]


async def test_list_endpoints_filters_by_deployment(k8s, core):
    _set_cluster(
        core,
        [_node("n1"), _node("n2")],
        [_pod("a", node="n1", deployment="dep-a"), _pod("b", node="n2", deployment="dep-b")],
    )
    eps = await k8s.list_endpoints("inference", deployment="dep-b")
    assert [e.node_name for e in eps] == ["n2"]
    selector = core.list_namespaced_pod.await_args.kwargs["label_selector"]
    assert "llm.nsu/role=inference" in selector and "llm.nsu/deployment=dep-b" in selector


async def test_list_endpoints_rpc_pod_has_only_port(k8s, core):
    _set_cluster(core, [_node("n1")], [_pod("r", role="rpc", annotations={"llm.nsu/port": "18050"})])
    (ep,) = await k8s.list_endpoints("rpc")
    assert (ep.port, ep.model_name, ep.max_input_tokens, ep.spec_hash) == (18050, None, None, None)


async def test_list_nodes_maps_labels_capacity_and_ready(k8s, core):
    core.list_node.return_value = client.V1NodeList(
        items=[
            _node("llm-main"),
            _node(
                "gpu1",
                labels={
                    "llm.nsu/accel": "nvidia", "llm.nsu/vram-mib": "24576", "llm.nsu/ram-mib": "28672",
                    "llm.nsu/deployment": "dep",
                },
                external="192.168.3.200",
            ),
            _node("cpu1", ready=False, labels={"llm.nsu/accel": "cpu"}),
        ]
    )
    main, gpu, cpu = await k8s.list_nodes()
    assert main == NodeInfo(
        name="llm-main",
        accel=None,
        internal_ip="172.17.0.3",
        external_ip=None,
        ready=True,
        kubelet_version="v1.36.5+k3s1",
        is_main=True,
        vram_mib=None,
        mem_mib=16000,
        deployment=None,
    )
    assert main.ram_mib is None and gpu.ram_mib == 28672
    assert (gpu.accel, gpu.vram_mib, gpu.deployment, gpu.external_ip, gpu.is_main) == (
        "nvidia",
        24576,
        "dep",
        "192.168.3.200",
        False,
    )
    assert (cpu.accel, cpu.ready, cpu.vram_mib) == ("cpu", False, None)


async def test_patch_node_labels_none_removes(k8s, core):
    await k8s.patch_node_labels("n1", {"llm.nsu/deployment": None, "llm.nsu/accel": "cpu"})
    core.patch_node.assert_awaited_once()
    args, kwargs = core.patch_node.await_args
    body = kwargs.get("body", args[1] if len(args) > 1 else None)
    assert args[0] == "n1"
    assert body == {"metadata": {"labels": {"llm.nsu/deployment": None, "llm.nsu/accel": "cpu"}}}


def _tainted(node, *taints):
    node.spec = client.V1NodeSpec(taints=list(taints))
    node.metadata.resource_version = "41"
    return node


async def test_list_nodes_reports_the_class_taint(k8s, core):
    other = client.V1Taint(key="node.kubernetes.io/unreachable", effect="NoExecute")
    nodes = [
        _node("plain"),
        _tainted(_node("tainted"), client.V1Taint(key="llm.nsu/class", value="true", effect="NoSchedule")),
        _tainted(_node("other"), other),
    ]
    _set_cluster(core, nodes, [])
    got = {n.name: n.class_taint for n in await k8s.list_nodes()}
    assert got == {"plain": False, "tainted": True, "other": False}


async def test_add_class_taint_keeps_other_taints_and_locks_on_the_resource_version(k8s, core):
    added = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    other = client.V1Taint(key="node.kubernetes.io/unreachable", effect="NoExecute", time_added=added)
    core.read_node.return_value = _tainted(_node("n1"), other)
    await k8s.add_class_taint("n1")
    args, kwargs = core.patch_node.await_args
    assert args[0] == "n1"
    assert kwargs["body"] == {
        "metadata": {"resourceVersion": "41"},
        "spec": {
            "taints": [
                {"key": "node.kubernetes.io/unreachable", "effect": "NoExecute", "timeAdded": "2026-11-03T10:00:00Z"},
                {"key": "llm.nsu/class", "value": "true", "effect": "NoSchedule"},
            ]
        },
    }


async def test_add_class_taint_is_idempotent(k8s, core):
    core.read_node.return_value = _tainted(_node("n1"), client.V1Taint(key="llm.nsu/class", value="true", effect="NoSchedule"))
    await k8s.add_class_taint("n1")
    core.patch_node.assert_not_awaited()
    core.read_node.return_value = _node("n2")  # no spec at all
    await k8s.add_class_taint("n2")
    assert core.patch_node.await_args.kwargs["body"]["spec"]["taints"] == [
        {"key": "llm.nsu/class", "value": "true", "effect": "NoSchedule"}
    ]


async def test_delete_node_404_returns_false(k8s, core):
    core.delete_node.side_effect = ApiException(status=404)
    assert await k8s.delete_node("gone") is False
    core.delete_node.side_effect = None
    assert await k8s.delete_node("there") is True


async def test_delete_node_other_error_propagates(k8s, core):
    core.delete_node.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        await k8s.delete_node("n1")


def _manifest(kind="DaemonSet", name="llm-dep-cpu"):
    return {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": NS, "labels": {"llm.nsu/deployment": "dep"}},
        "spec": {"selector": {"matchLabels": {"a": "b"}}, "template": {}},
    }


async def test_apply_creates_on_404_and_replaces_existing(k8s, apps):
    apps.read_namespaced_daemon_set.side_effect = ApiException(status=404)
    await k8s.apply(_manifest())
    apps.create_namespaced_daemon_set.assert_awaited_once()
    assert apps.create_namespaced_daemon_set.await_args.args[0] == NS
    apps.replace_namespaced_daemon_set.assert_not_awaited()

    apps.read_namespaced_daemon_set.side_effect = None
    apps.read_namespaced_daemon_set.return_value = client.V1DaemonSet(metadata=client.V1ObjectMeta(resource_version="77"))
    manifest = _manifest()
    await k8s.apply(manifest)
    args, kwargs = apps.replace_namespaced_daemon_set.await_args
    body = kwargs.get("body", args[2] if len(args) > 2 else None)
    assert args[:2] == ("llm-dep-cpu", NS)
    assert body["metadata"]["resourceVersion"] == "77"
    assert body["spec"] == manifest["spec"]
    assert "resourceVersion" not in manifest["metadata"]  # caller's dict is not mutated


async def test_put_config_map_creates_on_404_and_replaces_existing(k8s, core):
    core.read_namespaced_config_map.side_effect = ApiException(status=404)
    await k8s.put_config_map("llm-model-repos", {"repos": "a/b"})
    args, kwargs = core.create_namespaced_config_map.await_args
    body = kwargs["body"]
    assert args[0] == NS and body["metadata"]["name"] == "llm-model-repos" and body["data"] == {"repos": "a/b"}
    assert body["metadata"]["labels"] == {"app.kubernetes.io/part-of": "nsu-llm"}

    core.read_namespaced_config_map.side_effect = None
    core.read_namespaced_config_map.return_value = client.V1ConfigMap(metadata=client.V1ObjectMeta(resource_version="9"))
    await k8s.put_config_map("llm-model-repos", {"repos": "a/b,c/d"})
    args, kwargs = core.replace_namespaced_config_map.await_args
    assert args[:2] == ("llm-model-repos", NS)
    assert kwargs["body"]["metadata"]["resourceVersion"] == "9" and kwargs["body"]["data"] == {"repos": "a/b,c/d"}


async def test_apply_deployment_uses_deployment_api(k8s, apps):
    apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    await k8s.apply(_manifest("Deployment", "llm-dep-g0"))
    apps.create_namespaced_deployment.assert_awaited_once()
    apps.create_namespaced_daemon_set.assert_not_awaited()


async def test_apply_rejects_unknown_kind(k8s):
    with pytest.raises(ValueError):
        await k8s.apply(_manifest("StatefulSet"))


async def test_delete_ignores_404(k8s, apps):
    apps.delete_namespaced_daemon_set.side_effect = ApiException(status=404)
    await k8s.delete("DaemonSet", "x")
    await k8s.delete("Deployment", "y")
    apps.delete_namespaced_deployment.assert_awaited_once()
    assert apps.delete_namespaced_deployment.await_args.args[:2] == ("y", NS)


async def test_delete_pod_ignores_404(k8s, core):
    await k8s.delete_pod("rpc-a")
    assert core.delete_namespaced_pod.await_args.args[:2] == ("rpc-a", NS)
    assert "grace_period_seconds" not in core.delete_namespaced_pod.await_args.kwargs
    await k8s.delete_pod("leader-a", grace_period_seconds=0)  # a pod hanging in Terminating on a down node
    assert core.delete_namespaced_pod.await_args.args[:2] == ("leader-a", NS)
    assert core.delete_namespaced_pod.await_args.kwargs["grace_period_seconds"] == 0
    core.delete_namespaced_pod.side_effect = ApiException(status=404)
    await k8s.delete_pod("gone")
    core.delete_namespaced_pod.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        await k8s.delete_pod("forbidden")


async def test_list_owned_reads_template_hashes(k8s, apps):
    apps.list_namespaced_daemon_set.return_value = client.V1DaemonSetList(
        items=[_apps_obj(client.V1DaemonSet, client.V1DaemonSetSpec, "llm-dep-cpu", "dep", "h1")]
    )
    apps.list_namespaced_deployment.return_value = client.V1DeploymentList(
        items=[_apps_obj(client.V1Deployment, client.V1DeploymentSpec, "llm-dep-g0", "dep", "h2", "g1", "a,b")]
    )
    owned = await k8s.list_owned()
    assert owned == [
        OwnedWorkload("DaemonSet", "llm-dep-cpu", "dep", "h1", None),
        OwnedWorkload("Deployment", "llm-dep-g0", "dep", "h2", "g1", "a,b"),
    ]
    assert apps.list_namespaced_daemon_set.await_args.kwargs["label_selector"] == "llm.nsu/deployment"


async def test_pod_info_reports_crashloop_message(k8s, core):
    status = client.V1ContainerStatus(
        name="main",
        image="i",
        image_id="i",
        ready=False,
        restart_count=7,
        state=client.V1ContainerState(
            waiting=client.V1ContainerStateWaiting(reason="CrashLoopBackOff", message="back-off 5m0s")
        ),
    )
    core.list_namespaced_pod.return_value = client.V1PodList(
        items=[
            _pod("bad", ready=False, container_status=status),
            _pod("fine", role="rpc"),
        ]
    )
    bad, fine = await k8s.list_pods("dep")
    assert bad == PodInfo("bad", "n1", "inference", "dep", "Running", False, 7, "CrashLoopBackOff: back-off 5m0s")
    assert (fine.role, fine.ready, fine.restarts, fine.message) == ("rpc", True, 0, None)
    assert core.list_namespaced_pod.await_args.kwargs["label_selector"] == "llm.nsu/deployment=dep"


async def test_pod_info_names_the_workload_object_of_the_pod(k8s, core):
    """A leader pod name may be truncated (generateName): the controller reads its object from the name label."""
    core.list_namespaced_pod.return_value = client.V1PodList(
        items=[
            _pod("llm-x-g3-7c9d8f6b5x7k2p", role="split-leader", labels={"app.kubernetes.io/name": "llm-x-g3"}),
            _pod("bare"),
        ]
    )
    leader, bare = await k8s.list_pods("dep")
    assert leader.workload == "llm-x-g3"
    assert bare.workload is None


def test_the_name_label_is_the_one_the_builders_put_on_pods():
    from app.k8s import L_NAME
    from app.workloads.builders import NAME_LABEL

    assert L_NAME == NAME_LABEL


async def test_pod_info_reports_terminated_reason(k8s, core):
    status = client.V1ContainerStatus(
        name="main",
        image="i",
        image_id="i",
        ready=False,
        restart_count=1,
        state=client.V1ContainerState(terminated=client.V1ContainerStateTerminated(exit_code=1, reason="Error")),
    )
    core.list_namespaced_pod.return_value = client.V1PodList(items=[_pod("p", container_status=status)])
    (pod,) = await k8s.list_pods("dep")
    assert pod.message == "Error"


async def test_pod_log_tail(k8s, core):
    core.read_namespaced_pod_log.return_value = "line1\nline2\n"
    assert await k8s.pod_log_tail("p1", lines=20) == "line1\nline2\n"
    args, kwargs = core.read_namespaced_pod_log.await_args
    assert args[:2] == ("p1", NS)
    assert kwargs["tail_lines"] == 20
    core.read_namespaced_pod_log.side_effect = ApiException(status=400)
    assert await k8s.pod_log_tail("p1") == ""


async def test_read_token_file(k8s, tmp_path):
    f = tmp_path / "agent-token"
    f.write_text("K10abc::node:xyz\n")
    assert k8s.read_token_file(str(f)) == "K10abc::node:xyz"
    assert k8s.read_token_file(str(tmp_path / "missing")) is None


async def test_unavailable_when_kubeconfig_missing(tmp_path):
    k = K8sClient(str(tmp_path / "no-such-kubeconfig"), NS)
    assert await k.available() is False
    with pytest.raises(K8sUnavailable):
        await k.list_nodes()


async def test_unavailable_when_cluster_unreachable(k8s, core):
    core.list_node.side_effect = OSError("connection refused")
    with pytest.raises(K8sUnavailable):
        await k8s.list_nodes()
    assert await k8s.available() is False
    core.list_node.side_effect = None
    core.list_node.return_value = client.V1NodeList(items=[])
    assert await k8s.available() is True


@pytest.fixture
def leases(apis, monkeypatch):
    api = AsyncMock()
    monkeypatch.setattr(client, "CoordinationV1Api", lambda *_a, **_k: api)
    return api


def _lease(name, renew):
    return client.V1Lease(metadata=client.V1ObjectMeta(name=name), spec=client.V1LeaseSpec(renew_time=renew))


async def test_node_heartbeats_are_the_renew_times_of_the_node_leases(k8s, leases):
    t1, t2 = datetime(2026, 11, 3, 10, 0, 5, tzinfo=timezone.utc), datetime(2026, 11, 3, 10, 0, 9, tzinfo=timezone.utc)
    leases.list_namespaced_lease.return_value = client.V1LeaseList(
        items=[_lease("pc01", t1), _lease("pc02", t2), _lease("no-renew", None)]
    )
    assert await k8s.node_heartbeats() == {"pc01": t1, "pc02": t2}
    assert leases.list_namespaced_lease.call_args.args == ("kube-node-lease",)


async def test_node_heartbeats_unreachable_cluster_is_unavailable(k8s, leases):
    leases.list_namespaced_lease.side_effect = OSError("connection refused")
    with pytest.raises(K8sUnavailable):
        await k8s.node_heartbeats()


async def test_node_heartbeats_api_error_gives_no_heartbeats(k8s, leases):
    leases.list_namespaced_lease.side_effect = ApiException(status=403)
    assert await k8s.node_heartbeats() == {}


def _public(cls):
    return {n: f for n, f in inspect.getmembers(cls, inspect.isfunction) if not n.startswith("_")}


def test_fake_k8s_matches_client_interface():
    real, fake = _public(K8sClient), _public(FakeK8s)
    for name, fn in real.items():
        assert name in fake, f"FakeK8s lacks {name}"
        assert inspect.iscoroutinefunction(fn) == inspect.iscoroutinefunction(fake[name]), name
        assert list(inspect.signature(fn).parameters) == list(inspect.signature(fake[name]).parameters), name


async def test_fake_k8s_behaviour():
    fake = FakeK8s()
    fake.add_node("n1", accel="cpu", external_ip="192.168.3.5")
    fake.add_pod("p1", "n1", "inference", "dep", model_name="m", max_input_tokens=4096)
    fake.add_pod("p2", "n1", "inference", "dep", ready=False)
    fake.add_pod("p3", "n1", "rpc", "dep", port=18050)
    fake.logs["p1"] = "a\nb\nc\n"

    (ep,) = await fake.list_endpoints("inference", "dep")
    assert (ep.ip, ep.port, ep.model_name, ep.max_input_tokens, ep.accel) == ("192.168.3.5", 18080, "m", 4096, "cpu")
    assert len(await fake.list_pods("dep")) == 3
    assert await fake.pod_log_tail("p1", lines=2) == "b\nc"

    await fake.patch_node_labels("n1", {"llm.nsu/deployment": "dep", "llm.nsu/vram-mib": "100"})
    node = (await fake.list_nodes())[0]
    assert (node.deployment, node.vram_mib) == ("dep", 100)
    await fake.patch_node_labels("n1", {"llm.nsu/deployment": None})
    assert (await fake.list_nodes())[0].deployment is None

    manifest = _manifest()
    manifest["spec"]["template"] = {"metadata": {"annotations": {"llm.nsu/spec-hash": "h"}}}
    await fake.apply(manifest)
    assert await fake.list_owned() == [OwnedWorkload("DaemonSet", "llm-dep-cpu", "dep", "h", None)]
    await fake.delete("DaemonSet", "llm-dep-cpu")
    assert await fake.list_owned() == []

    (rpc,) = await fake.list_endpoints("rpc", "dep")
    assert rpc.pod_name == "p3"
    await fake.delete_pod("p3")
    assert fake.deleted_pods == ["p3"] and "p3" not in fake.pods

    assert (await fake.list_nodes())[0].class_taint is False
    await fake.add_class_taint("n1")
    assert (await fake.list_nodes())[0].class_taint is True and fake.tainted_nodes == ["n1"]

    assert await fake.delete_node("n1") is True
    assert await fake.delete_node("n1") is False

    fake.unavailable = True
    assert await fake.available() is False
    with pytest.raises(K8sUnavailable):
        await fake.list_nodes()


def _running_status(started_at):
    return client.V1ContainerStatus(
        name="main",
        image="i",
        image_id="i",
        ready=True,
        restart_count=0,
        state=client.V1ContainerState(running=client.V1ContainerStateRunning(started_at=started_at)),
    )


async def test_pod_info_and_endpoints_carry_the_container_start(k8s, core):
    """The controller tells a pod that ran through an outage from one started after it by this time."""
    started = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    pods = [
        _pod("up", role="rpc", annotations={"llm.nsu/port": "18050"}, container_status=_running_status(started)),
        _pod("waiting", role="rpc", annotations={"llm.nsu/port": "18050"}, ready=False),
    ]
    _set_cluster(core, [_node("n1")], pods)
    up, waiting = await k8s.list_pods("dep")
    assert up.started_at == started.timestamp() and waiting.started_at is None
    (ep,) = await k8s.list_endpoints("rpc")
    assert ep.started_at == started.timestamp()


async def test_endpoints_name_the_workload_object_of_the_pod(k8s, core):
    _set_cluster(
        core, [_node("n1")], [_pod("llm-x-g3-7c9d8f6b5x7k2p", role="split-leader", labels={"app.kubernetes.io/name": "llm-x-g3"})]
    )
    (ep,) = await k8s.list_endpoints("split-leader")
    assert ep.workload == "llm-x-g3"
