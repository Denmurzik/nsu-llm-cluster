import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app.config import Settings, key_fingerprint
from app.k8s import OwnedWorkload
from app.workloads.builders import (
    RpcWorker,
    leader_groups,
    plan_groups,
    replicas_daemonset,
    rpc_daemonset,
    split_leader,
    template_hash,
    vllm_daemonset,
)

S = Settings(node_image_tag="0.1.0", hf_mirror_enabled=False)  # nodes download from Hugging Face directly
M = Settings(node_image_tag="0.1.0", public_hostname="llm.example")  # through the mirror on main (the default)


def direct(**kw):
    return Settings(hf_mirror_enabled=False, **kw)


def dep(**kw):
    base = dict(
        name="gemma-4-e4b", engine="llamacpp", repo="org/model-GGUF", file="model-Q4_K_M.gguf", mode="replicas",
        group_size=2, accel="any", ctx_per_request=8192, parallel=2, meta={"arch": "gemma4", "weights_bytes": 5 * 10**9}, use_node_ram=True,
        ram_budget_pct=70,
    )
    return SimpleNamespace(**{**base, **kw})


def pod(manifest):
    return manifest["spec"]["template"]


def container(manifest):
    return pod(manifest)["spec"]["containers"][0]


def env(manifest):
    """Plain `value` variables; secret references are checked by `secret_env`."""
    return {e["name"]: e["value"] for e in container(manifest)["env"] if "value" in e}


def secret_env(manifest):
    return {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container(manifest)["env"] if "secretKeyRef" in e.get("valueFrom", {})}


def annotations(manifest):
    return pod(manifest)["metadata"]["annotations"]


def test_replicas_daemonset_contract():
    m = replicas_daemonset(dep(), "cpu", S)
    assert (m["kind"], m["metadata"]["name"], m["metadata"]["namespace"]) == ("DaemonSet", "llm-gemma-4-e4b-cpu", "llm")
    labels = {
        "app.kubernetes.io/part-of": "nsu-llm", "llm.nsu/deployment": "gemma-4-e4b", "llm.nsu/role": "inference",
    }
    assert labels.items() <= m["metadata"]["labels"].items()
    assert labels.items() <= pod(m)["metadata"]["labels"].items()
    assert labels.items() <= m["spec"]["selector"]["matchLabels"].items()
    assert m["spec"]["selector"]["matchLabels"].items() <= pod(m)["metadata"]["labels"].items()
    spec = pod(m)["spec"]
    assert spec["nodeSelector"] == {"llm.nsu/deployment": "gemma-4-e4b", "llm.nsu/accel": "cpu"}
    assert spec["hostNetwork"] is True and spec["dnsPolicy"] == "Default"
    c = container(m)
    assert c["image"] == "nsu-llm-node:0.1.0-cpu" and c["imagePullPolicy"] == "IfNotPresent"
    assert c["command"] == ["/opt/llm/bin/serve-llama.sh"]
    assert env(m) == {
        "MODEL_REPO": "org/model-GGUF", "MODEL_FILE": "model-Q4_K_M.gguf", "PUBLIC_MODEL_NAME": "gemma-4-e4b",
        "INFER_PORT": "18080", "CTX_SIZE": "16384", "PARALLEL_SLOTS": "2", "ACCEL": "cpu",
    }
    assert {"name": "models", "hostPath": {"path": "/var/lib/llm-models", "type": "DirectoryOrCreate"}} in spec["volumes"]
    assert {"name": "models", "mountPath": "/var/lib/llm-models"} in c["volumeMounts"]
    assert c["readinessProbe"] == {"httpGet": {"path": "/health", "port": 18080}, "periodSeconds": 10}
    assert c["startupProbe"] == {"httpGet": {"path": "/health", "port": 18080}, "periodSeconds": 10, "failureThreshold": 360}
    assert m["spec"]["updateStrategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}}
    a = annotations(m)
    assert (a["llm.nsu/port"], a["llm.nsu/model-name"], a["llm.nsu/max-input-tokens"]) == ("18080", "gemma-4-e4b", "8192")
    assert a["llm.nsu/spec-hash"] == template_hash(m) and len(a["llm.nsu/spec-hash"]) == 12
    assert "runtimeClassName" not in spec and "resources" not in c


def test_hf_token_only_when_set_and_only_as_a_secret_reference():
    plain = replicas_daemonset(dep(), "cpu", S)
    assert "HF_TOKEN" not in env(plain) and "HF_TOKEN" not in secret_env(plain)
    assert "llm.nsu/hf-token" not in annotations(plain)
    with_token = direct(hf_token="hf_secret")
    for m in (
        replicas_daemonset(dep(), "cpu", with_token),
        split_leader(dep(mode="split"), 0, group("a", "b"), with_token),
        vllm_daemonset(dep(engine="vllm", file=None), with_token),
    ):
        assert secret_env(m)["HF_TOKEN"] == {"name": "llm-node-secrets", "key": "HF_TOKEN", "optional": True}  # an older Secret lacks the key
        assert "hf_secret" not in str(m)  # the pod object (readable by anyone with get pods) has no token
        assert annotations(m)["llm.nsu/hf-token"] == key_fingerprint("hf_secret")
    rotated = replicas_daemonset(dep(), "cpu", direct(hf_token="hf_other"))
    # a secretKeyRef is read at container start: a new token must change the template, so the pods restart
    assert annotations(rotated)["llm.nsu/spec-hash"] != annotations(replicas_daemonset(dep(), "cpu", with_token))["llm.nsu/spec-hash"]


def test_gpu_variant_runtime_and_limit():
    m = replicas_daemonset(dep(), "nvidia", S)
    assert m["metadata"]["name"] == "llm-gemma-4-e4b-gpu"
    assert pod(m)["spec"]["runtimeClassName"] == "nvidia"
    assert pod(m)["spec"]["nodeSelector"]["llm.nsu/accel"] == "nvidia"
    assert container(m)["resources"] == {"limits": {"nvidia.com/gpu": 1}}
    assert container(m)["image"] == "nsu-llm-node:0.1.0-gpu"
    assert env(m)["ACCEL"] == "nvidia"


def test_vllm_daemonset_args_and_parsers():
    d = dep(engine="vllm", file=None, repo="google/gemma-4-E4B-it-qat-w4a16-ct")
    m = vllm_daemonset(d, S)
    assert m["metadata"]["name"] == "llm-gemma-4-e4b-vllm"
    c = container(m)
    assert c["image"] == "vllm/vllm-openai:v0.32.0"
    assert c["command"] == ["vllm", "serve"]
    args = c["args"]
    assert args[0] == "google/gemma-4-E4B-it-qat-w4a16-ct"
    flags = dict(zip(args[1:], args[2:], strict=False))
    assert flags["--served-model-name"] == "gemma-4-e4b"
    assert (flags["--host"], flags["--port"]) == ("0.0.0.0", "18080")
    assert (flags["--max-model-len"], flags["--max-num-seqs"]) == ("8192", "2")
    assert flags["--gpu-memory-utilization"] == "0.85"  # leaves room for the CUDA context and a desktop
    assert "--enable-auto-tool-choice" in args
    assert flags["--tool-call-parser"] == "gemma4" and flags["--reasoning-parser"] == "gemma4"
    spec = pod(m)["spec"]
    assert spec["runtimeClassName"] == "nvidia" and spec["nodeSelector"]["llm.nsu/accel"] == "nvidia"
    assert c["resources"]["limits"] == {"nvidia.com/gpu": 1}
    assert {"name": "hf-cache", "mountPath": "/root/.cache/huggingface"} in c["volumeMounts"]
    assert any(v["name"] == "shm" and v["emptyDir"]["medium"] == "Memory" for v in spec["volumes"])
    assert c["readinessProbe"]["httpGet"] == {"path": "/health", "port": 18080}
    assert annotations(m)["llm.nsu/max-input-tokens"] == "8192" and annotations(m)["llm.nsu/port"] == "18080"
    assert m["spec"]["template"]["metadata"]["labels"]["llm.nsu/role"] == "inference"

    other = vllm_daemonset(dep(engine="vllm", file=None, meta={"arch": "llama"}), S)
    assert "--tool-call-parser" not in container(other)["args"]
    assert "--enable-auto-tool-choice" not in container(other)["args"]
    assert "--tool-call-parser" not in container(vllm_daemonset(dep(engine="vllm", file=None, meta=None), S))["args"]


def test_rpc_daemonset_contract():
    cpu = rpc_daemonset(dep(mode="split"), "cpu", S)
    assert cpu["metadata"]["name"] == "llm-gemma-4-e4b-rpc-cpu"
    assert cpu["metadata"]["labels"]["llm.nsu/role"] == "rpc"
    assert pod(cpu)["spec"]["nodeSelector"] == {"llm.nsu/deployment": "gemma-4-e4b", "llm.nsu/accel": "cpu"}
    c = container(cpu)
    assert c["command"] == ["/opt/llm/bin/serve-rpc.sh"]
    # the tensor cache (-c) lives on the models volume, so a restarted group does not resend the weights
    assert env(cpu) == {
        "RPC_PORT": "18050", "RPC_DEVICES": "CPU", "RPC_CACHE": "1", "LLAMA_CACHE": "/var/lib/llm-models",
        "RPC_CACHE_MAX_GIB": "64",
    }
    assert {"name": "models", "mountPath": "/var/lib/llm-models"} in c["volumeMounts"]
    assert {"name": "models", "hostPath": {"path": "/var/lib/llm-models", "type": "DirectoryOrCreate"}} in pod(cpu)["spec"]["volumes"]
    # ggml-rpc-server serves one client at a time: a TCP probe queues behind the leader and times out.
    probe = c["readinessProbe"]
    assert "tcpSocket" not in probe and probe["exec"]["command"][:2] == ["grep", "-qsE"]
    assert c["ports"] == [{"containerPort": 18050, "hostPort": 18050}]
    assert annotations(cpu)["llm.nsu/port"] == "18050"
    assert "runtimeClassName" not in pod(cpu)["spec"] and "resources" not in c
    gpu = rpc_daemonset(dep(mode="split"), "nvidia", S)
    assert gpu["metadata"]["name"] == "llm-gemma-4-e4b-rpc-gpu"
    assert pod(gpu)["spec"]["runtimeClassName"] == "nvidia"
    assert container(gpu)["resources"]["limits"] == {"nvidia.com/gpu": 1}
    assert container(gpu)["image"] == "nsu-llm-node:0.1.0-gpu"
    assert env(gpu)["RPC_DEVICES"] == "CUDA0,CPU"  # VRAM and RAM of the node
    assert env(rpc_daemonset(dep(mode="split", use_node_ram=False), "nvidia", S))["RPC_DEVICES"] == "CUDA0"


def test_rpc_daemonset_rolls_out_at_once_and_stops_fast():
    rpc = rpc_daemonset(dep(mode="split"), "cpu", S)
    # a group is never formed against servers of two templates (the controller takes only current ones): all of them
    # are replaced together, so the groups reload once and not once per node
    assert rpc["spec"]["updateStrategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": "100%"}}
    # ggml-rpc-server (PID 1) ignores SIGTERM: without a short grace every recycle waits 30 s for the SIGKILL
    assert pod(rpc)["spec"]["terminationGracePeriodSeconds"] == 5
    # the probe reads /proc only, but under full CPU load the 1 s default made a busy server NotReady
    assert container(rpc)["readinessProbe"]["timeoutSeconds"] == 5
    # the inference DaemonSets still replace one node at a time
    replicas = replicas_daemonset(dep(), "cpu", S)
    assert replicas["spec"]["updateStrategy"]["rollingUpdate"] == {"maxUnavailable": 1}
    assert "terminationGracePeriodSeconds" not in pod(replicas)["spec"]


def test_llama_pods_read_the_repos_in_use_from_a_configmap_at_start():
    """serve-llama.sh deletes cached repos no deployment uses (KEEP_MODEL_REPOS). The list comes from the ConfigMap
    the workload controller keeps, read when the container starts: a new deployment changes no pod template."""
    ref = {"name": "KEEP_MODEL_REPOS", "valueFrom": {"configMapKeyRef": {"name": "llm-model-repos", "key": "repos", "optional": True}}}
    group = [RpcWorker("a", "10.0.0.1", 18050, "cpu"), RpcWorker("b", "10.0.0.2", 18050, "cpu")]
    for manifest in (replicas_daemonset(dep(), "cpu", S), split_leader(dep(mode="split"), 0, group, S)):
        assert ref in container(manifest)["env"]
    assert "KEEP_MODEL_REPOS" not in str(rpc_daemonset(dep(mode="split"), "cpu", S))


def test_rpc_memory_limit_is_the_backstop_of_the_ram_device():
    gpu = rpc_daemonset(dep(mode="split"), "nvidia", S, memory_limit_mib=21504)
    assert container(gpu)["resources"] == {
        "limits": {"nvidia.com/gpu": 1, "memory": "21504Mi"}, "requests": {"memory": "256Mi"},
    }
    cpu = rpc_daemonset(dep(mode="split"), "cpu", S, memory_limit_mib=8192)
    assert container(cpu)["resources"] == {"limits": {"memory": "8192Mi"}, "requests": {"memory": "256Mi"}}
    assert annotations(cpu)["llm.nsu/spec-hash"] != annotations(rpc_daemonset(dep(mode="split"), "cpu", S))["llm.nsu/spec-hash"]


def test_plan_groups_rules():
    w = lambda n, accel, ip="10.0.0.9": RpcWorker(n, ip, 18050, accel)  # noqa: E731
    workers = [w("c3", "cpu"), w("g2", "nvidia"), w("c1", "cpu"), w("g1", "nvidia"), w("c2", "cpu"), w("g3", "nvidia")]
    groups = plan_groups(workers, 2)
    assert {i: [x.node_name for x in g] for i, g in groups.items()} == {0: ["g1", "g2"], 1: ["c1", "c2"]}  # nvidia first
    assert plan_groups(workers, 3) == {
        0: [w("g1", "nvidia"), w("g2", "nvidia"), w("g3", "nvidia")],
        1: [w("c1", "cpu"), w("c2", "cpu"), w("c3", "cpu")],
    }
    assert plan_groups(workers[:2], 2) == {}  # mixed accel never forms a group
    assert plan_groups([], 2) == {}


def names_of(groups):
    return {i: [x.node_name for x in g] for i, g in groups.items()}


def test_plan_groups_mixed_forms_homogeneous_groups_first_then_mixes_the_rest():
    """accel `any`: groups of one accel first (as before), then the leftovers of both together, nvidia first, so one
    GPU node with CPU nodes forms a group instead of waiting for a GPU partner."""
    w = lambda n, accel: RpcWorker(n, "10.0.0.9", 18050, accel)  # noqa: E731
    workers = [w("c3", "cpu"), w("g2", "nvidia"), w("c1", "cpu"), w("g1", "nvidia"), w("c2", "cpu"), w("g3", "nvidia")]
    assert names_of(plan_groups(workers, 2, mixed=True)) == {0: ["g1", "g2"], 1: ["c1", "c2"], 2: ["g3", "c3"]}
    assert names_of(plan_groups(workers, 3, mixed=True)) == {0: ["g1", "g2", "g3"], 1: ["c1", "c2", "c3"]}
    assert names_of(plan_groups(workers[:2], 2, mixed=True)) == {0: ["g2", "c3"]}
    one_gpu = [w("g1", "nvidia")] + [w(f"c{i}", "cpu") for i in range(1, 5)]
    assert names_of(plan_groups(one_gpu, 5, mixed=True)) == {0: ["g1", "c1", "c2", "c3", "c4"]}
    # a running mixed group is kept as it is
    assert names_of(plan_groups(workers, 2, {0: ["g3", "c3"]}, mixed=True)) == {
        0: ["g3", "c3"], 1: ["g1", "g2"], 2: ["c1", "c2"],
    }
    assert names_of(plan_groups(workers, 2, {0: ["g3", "c3"]})) == {0: ["g1", "g2"], 1: ["c1", "c2"]}  # not without


def test_plan_groups_keeps_existing_groups_whose_members_are_all_alive():
    w = lambda n, ip="10.0.0.9": RpcWorker(n, ip, 18050, "cpu")  # noqa: E731
    existing = {0: ["a", "b"], 1: ["c", "d"], 2: ["e", "f"]}
    # a is gone: g0 breaks up, g1 and g2 keep their members and indices, b alone forms nothing
    alive = [w(n) for n in "bcdef"]
    assert names_of(plan_groups(alive, 2, existing)) == {1: ["c", "d"], 2: ["e", "f"]}
    # a comes back: a new group from the free workers takes the lowest free index
    assert names_of(plan_groups([w(n) for n in "abcdef"], 2, {1: ["c", "d"], 2: ["e", "f"]})) == {
        0: ["a", "b"], 1: ["c", "d"], 2: ["e", "f"],
    }
    # a new node joins: it waits for a partner instead of reshuffling the sorted list
    joined = [w(n) for n in "abcdefg"]
    assert names_of(plan_groups(joined, 2, existing)) == {0: ["a", "b"], 1: ["c", "d"], 2: ["e", "f"]}
    # the kept group carries the current addresses (a moved IP rebuilds only that leader)
    kept = plan_groups([w("c", "10.0.0.3"), w("d", "10.9.9.9")], 2, {0: ["c", "d"]})
    assert kept == {0: [w("c", "10.0.0.3"), w("d", "10.9.9.9")]}
    # the stored member order is kept: the leader stays on the same node
    assert names_of(plan_groups([w("c"), w("d")], 2, {0: ["d", "c"]})) == {0: ["d", "c"]}


def test_plan_groups_drops_existing_groups_that_no_longer_fit():
    c = lambda n: RpcWorker(n, "10.0.0.9", 18050, "cpu")  # noqa: E731
    g = lambda n: RpcWorker(n, "10.0.0.9", 18050, "nvidia")  # noqa: E731
    # the group size changed: the old pairs are re-planned into triples
    assert names_of(plan_groups([c(n) for n in "abcd"], 3, {0: ["a", "b"], 1: ["c", "d"]})) == {0: ["a", "b", "c"]}
    # a member changed its accelerator: the group is no longer homogeneous
    assert names_of(plan_groups([c("a"), g("b"), g("x"), g("y")], 2, {0: ["a", "b"]})) == {0: ["b", "x"]}
    # a node claimed by two stored groups stays in the first one only
    assert names_of(plan_groups([c(n) for n in "abc"], 2, {0: ["a", "b"], 1: ["b", "c"]})) == {0: ["a", "b"]}
    # nodes listed as excluded (their rpc pods are being recycled) form no group in this pass
    assert names_of(plan_groups([c(n) for n in "abcd"], 2, {}, exclude={"a"})) == {0: ["b", "c"]}


def test_plan_groups_limit_caps_only_new_groups():
    c = lambda n: RpcWorker(n, "10.0.0.9", 18050, "cpu")  # noqa: E731
    workers = [c(n) for n in "abcdef"]
    # spare nodes: no more than `limit` groups, the rest of the free workers wait in reserve
    assert names_of(plan_groups(workers, 2, limit=2)) == {0: ["a", "b"], 1: ["c", "d"]}
    assert plan_groups(workers, 2, limit=0) == {}
    # running groups are never broken up by the limit, they only use up its room
    assert names_of(plan_groups(workers, 2, {2: ["e", "f"]}, limit=1)) == {2: ["e", "f"]}
    assert names_of(plan_groups(workers, 2, {0: ["a", "b"], 1: ["c", "d"]}, limit=1)) == {0: ["a", "b"], 1: ["c", "d"]}
    assert names_of(plan_groups(workers, 2, {2: ["e", "f"]}, limit=2)) == {0: ["a", "b"], 2: ["e", "f"]}


def test_plan_groups_skips_reserved_indices():
    c = lambda n: RpcWorker(n, "10.0.0.9", 18050, "cpu")  # noqa: E731
    # index 1 belongs to a group that is waiting for a lost member: a new group must not overwrite its leader
    assert names_of(plan_groups([c(n) for n in "abef"], 2, {0: ["a", "b"]}, reserved={1})) == {
        0: ["a", "b"],
        2: ["e", "f"],
    }


def test_leader_groups_reads_members_of_this_deployments_leaders():
    owned = [
        OwnedWorkload("Deployment", "llm-sp-g0", "sp", "h", "x", "a,b"),
        OwnedWorkload("Deployment", "llm-sp-g3", "sp", "h", "x", "c,d"),
        OwnedWorkload("Deployment", "llm-sp-g1", "sp", "h", "x", None),  # built before members were recorded
        OwnedWorkload("Deployment", "llm-sp-g1-x", "sp-g1", "h", "x", "e,f"),  # another deployment's object
        OwnedWorkload("Deployment", "llm-other-g0", "other", "h", "x", "e,f"),
        OwnedWorkload("DaemonSet", "llm-sp-rpc-cpu", "sp", "h", None, None),
    ]
    assert leader_groups(owned, "sp") == {0: ["a", "b"], 3: ["c", "d"]}


def group(*names, accel="cpu"):
    return [RpcWorker(n, f"10.0.0.{i + 2}", 18050, accel) for i, n in enumerate(names)]


def test_split_leader_contract():
    d = dep(mode="split", name="big")
    g = group("a", "b")
    m = split_leader(d, 0, g, S)
    assert (m["kind"], m["metadata"]["name"]) == ("Deployment", "llm-big-g0")
    assert m["metadata"]["labels"]["llm.nsu/role"] == "split-leader"
    assert m["spec"]["strategy"] == {"type": "Recreate"} and m["spec"]["replicas"] == 1
    terms = pod(m)["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert terms == [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["a"]}]}]
    assert "nodeName" not in pod(m)["spec"]
    e = env(m)
    assert e["RPC_SERVERS"] == "127.0.0.1:18050,10.0.0.3:18050"
    assert (e["NGL"], e["ACCEL"], e["CTX_SIZE"], e["PARALLEL_SLOTS"]) == ("999", "cpu", "16384", "2")
    assert "TENSOR_SPLIT" not in e
    c = container(m)
    assert "resources" not in c and "runtimeClassName" not in pod(m)["spec"]
    assert c["image"] == "nsu-llm-node:0.1.0-cpu"
    assert c["ports"] == [{"containerPort": 18080, "hostPort": 18080}]
    a = annotations(m)
    assert a["llm.nsu/group-members"] == "a,b"  # read back by the controller to keep the group stable
    assert a["llm.nsu/group-hash"] == split_leader(d, 0, group("a", "b"), S)["spec"]["template"]["metadata"]["annotations"]["llm.nsu/group-hash"]
    assert a["llm.nsu/group-hash"] != annotations(split_leader(d, 0, group("a", "c"), S))["llm.nsu/group-hash"]
    moved = [RpcWorker("a", "10.0.0.2", 18050, "cpu"), RpcWorker("b", "10.9.9.9", 18050, "cpu")]
    assert a["llm.nsu/group-hash"] != annotations(split_leader(d, 0, moved, S))["llm.nsu/group-hash"]
    # a cpu group: cpu image, no nvidia runtime
    assert "runtimeClassName" not in pod(m)["spec"] and "NVIDIA_VISIBLE_DEVICES" not in e
    # an nvidia group: the gpu image (the only one on those nodes) and the runtime, but no GPU limit and no visible devices
    gpu = split_leader(d, 1, group("g1", "g2", accel="nvidia"), S)
    assert gpu["metadata"]["name"] == "llm-big-g1"
    assert container(gpu)["image"] == "nsu-llm-node:0.1.0-gpu"
    assert pod(gpu)["spec"]["runtimeClassName"] == "nvidia"
    assert "resources" not in container(gpu)
    ge = env(gpu)
    assert ge["NVIDIA_VISIBLE_DEVICES"] == "none" and ge["ACCEL"] == "cpu" and ge["NGL"] == "999"
    assert ge["RPC_SERVERS"] == "127.0.0.1:18050,10.0.0.3:18050"


def test_split_leader_passes_the_layer_placement():
    d = dep(mode="split", name="big")
    m = split_leader(d, 0, group("g1", "g2", accel="nvidia"), S, tensor_split=[10, 17, 10, 0])
    assert env(m)["TENSOR_SPLIT"] == "10,17,10,0"
    other = split_leader(d, 0, group("g1", "g2", accel="nvidia"), S, tensor_split=[10, 16, 11, 0])
    assert annotations(m)["llm.nsu/spec-hash"] != annotations(other)["llm.nsu/spec-hash"]  # a new placement restarts it


def test_template_hash_ignores_ctx_for_rpc_and_tracks_image():
    d = dep(mode="split")
    rpc = lambda x, s=S: annotations(rpc_daemonset(x, "cpu", s))["llm.nsu/spec-hash"]  # noqa: E731
    assert rpc(d) == rpc(dep(mode="split", ctx_per_request=4096, parallel=4, repo="other/repo"))
    assert rpc(d) != rpc(d, Settings(node_image_tag="0.2.0"))
    rep = lambda x, s=S: annotations(replicas_daemonset(x, "cpu", s))["llm.nsu/spec-hash"]  # noqa: E731
    assert rep(d) == rep(dep(mode="split"))
    assert rep(d) != rep(dep(mode="split", ctx_per_request=4096))
    assert rep(d) != rep(d, Settings(node_image_tag="0.2.0"))
    m = replicas_daemonset(d, "cpu", S)
    assert template_hash(m) == annotations(m)["llm.nsu/spec-hash"]  # the hash does not include itself


def test_inference_and_rpc_declare_host_ports():
    d = dep(mode="split")
    for m, port in (
        (replicas_daemonset(d, "cpu", S), 18080),
        (replicas_daemonset(d, "nvidia", S), 18080),
        (vllm_daemonset(dep(engine="vllm", file=None), S), 18080),
        (rpc_daemonset(d, "cpu", S), 18050),
        (rpc_daemonset(d, "nvidia", S), 18050),
        (split_leader(d, 0, group("a", "b"), S), 18080),
    ):
        assert {"containerPort": port, "hostPort": port} in container(m)["ports"]
        assert pod(m)["spec"]["hostNetwork"] is True
    custom = Settings(infer_port=28080, rpc_port=28050)
    assert container(replicas_daemonset(d, "cpu", custom))["ports"] == [{"containerPort": 28080, "hostPort": 28080}]
    assert container(rpc_daemonset(d, "cpu", custom))["ports"] == [{"containerPort": 28050, "hostPort": 28050}]


def test_names_and_labels_are_valid_k8s():
    d = dep(name="a" + "b" * 37 + "c", mode="split")  # the longest allowed deployment name (39)
    manifests = [
        replicas_daemonset(d, "cpu", S),
        replicas_daemonset(d, "nvidia", S),
        vllm_daemonset(d, S),
        rpc_daemonset(d, "cpu", S),
        rpc_daemonset(d, "nvidia", S),
        split_leader(d, 12, group("a", "b"), S),
    ]
    dns1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    names = [m["metadata"]["name"] for m in manifests]
    assert len(set(names)) == len(names)
    for m in manifests:
        assert dns1123.fullmatch(m["metadata"]["name"]) and len(m["metadata"]["name"]) <= 63
        for labels in (m["metadata"]["labels"], pod(m)["metadata"]["labels"], m["spec"]["selector"]["matchLabels"]):
            for k, v in labels.items():
                assert len(v) <= 63 and re.fullmatch(r"[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?", v), (k, v)
        assert m["spec"]["selector"]["matchLabels"].items() <= pod(m)["metadata"]["labels"].items()


def secret_ref():
    return {"name": "llm-node-secrets", "key": "BACKEND_API_KEY"}


def test_inference_pods_get_the_backend_key_from_the_secret_in_the_servers_own_variable():
    d = dep(mode="split")
    for m in (
        replicas_daemonset(d, "cpu", S),
        replicas_daemonset(d, "nvidia", S),
        split_leader(d, 0, group("a", "b"), S),
    ):
        assert secret_env(m) == {"LLAMA_API_KEY": secret_ref()}  # a reference, never the value
    assert secret_env(vllm_daemonset(dep(engine="vllm", file=None), S)) == {"VLLM_API_KEY": secret_ref()}
    assert secret_env(rpc_daemonset(d, "cpu", S)) == {}  # RPC has no authentication of its own
    keyed = Settings(backend_api_key="hex-secret")
    assert "hex-secret" not in str(replicas_daemonset(d, "cpu", keyed))


def test_the_key_never_reaches_a_command_line():
    keyed = Settings(backend_api_key="hex-secret")
    for m in (
        replicas_daemonset(dep(), "cpu", keyed),
        split_leader(dep(mode="split"), 0, group("a", "b"), keyed),
        vllm_daemonset(dep(engine="vllm", file=None), keyed),
    ):
        c = container(m)
        argv = c.get("command", []) + c.get("args", [])
        assert "--api-key" not in argv and not any("API_KEY" in a or "hex-secret" in a for a in argv)


def test_pod_template_carries_the_key_fingerprint_so_a_rotation_restarts_the_pods():
    keyed = Settings(backend_api_key="key-one")
    other = Settings(backend_api_key="key-two")
    for build in (
        lambda s: replicas_daemonset(dep(), "cpu", s),
        lambda s: split_leader(dep(mode="split"), 0, group("a", "b"), s),
        lambda s: vllm_daemonset(dep(engine="vllm", file=None), s),
    ):
        a, b, none = build(keyed), build(other), build(S)
        tag = annotations(a)["llm.nsu/backend-key"]
        assert tag == key_fingerprint("key-one") and len(tag) == 12
        assert annotations(a)["llm.nsu/spec-hash"] != annotations(b)["llm.nsu/spec-hash"]  # a new key: new pods
        assert "llm.nsu/backend-key" not in annotations(none)
        assert "key-one" not in str(a)
    assert "llm.nsu/backend-key" not in annotations(rpc_daemonset(dep(mode="split"), "cpu", keyed))


def test_hf_token_stays_next_to_the_backend_key():
    m = replicas_daemonset(dep(), "cpu", direct(hf_token="hf_abc"))
    assert secret_env(m) == {"HF_TOKEN": {"name": "llm-node-secrets", "key": "HF_TOKEN", "optional": True}, "LLAMA_API_KEY": secret_ref()}


@pytest.mark.skipif(shutil.which("grep") is None, reason="needs grep")
def test_rpc_readiness_matches_only_a_listening_socket(tmp_path):
    command = container(rpc_daemonset(dep(mode="split"), "cpu", S))["readinessProbe"]["exec"]["command"]
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    listening_v4 = "   0: 00000000:4682 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1\n"
    listening_v6 = "   0: 00000000000000000000000000000000:4682 00000000000000000000000000000000:0000 0A 0 0 0 0 0 1\n"
    client_only = "   1: 030011AC:8816 1503A8C0:4682 01 00000000:00000000 00:00000000 00000000     0        0 2\n"
    other_port = "   2: 00000000:4683 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 3\n"

    def ready(tcp, tcp6=None):
        (tmp_path / "tcp").write_text(header + tcp)
        files = [str(tmp_path / "tcp"), str(tmp_path / "tcp6")]
        if tcp6 is None:
            (tmp_path / "tcp6").unlink(missing_ok=True)
        else:
            (tmp_path / "tcp6").write_text(header + tcp6)
        return subprocess.run(command[:3] + files).returncode == 0

    assert ready(listening_v4 + client_only)
    assert ready(client_only, listening_v6)
    assert not ready(client_only + other_port, client_only)
    assert command[3:] == ["/proc/net/tcp", "/proc/net/tcp6"]


# ---------------------------------------------------------------- weights: HF mirror, CA, start-up time, rpc cache


def all_env(manifest):
    return {e["name"]: e for e in container(manifest)["env"]}


def test_inference_pods_download_through_the_mirror_on_main():
    with_token = Settings(node_image_tag="0.1.0", public_hostname="llm.example", hf_token="hf_real")
    for m in (
        replicas_daemonset(dep(), "cpu", with_token),
        replicas_daemonset(dep(), "nvidia", with_token),
        split_leader(dep(mode="split"), 0, group("a", "b"), with_token),
        vllm_daemonset(dep(engine="vllm", file=None), with_token),
    ):
        e = all_env(m)
        names = [x["name"] for x in container(m)["env"]]
        # the node name tags the client in the mirror's progress report; $(NODE_NAME) needs it defined first
        assert e["NODE_NAME"] == {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}
        assert names.index("NODE_NAME") < names.index("HF_ENDPOINT")
        assert e["HF_ENDPOINT"]["value"] == "https://llm.example/hf/c/$(NODE_NAME)"
        # the nodes get the mirror key, the real Hugging Face token stays on main
        assert secret_env(m)["HF_TOKEN"] == {"name": "llm-node-secrets", "key": "HF_MIRROR_TOKEN"}
        assert "llm.nsu/hf-token" not in annotations(m)
        assert {"name": "llm-ca", "mountPath": "/etc/llm-ca", "readOnly": True} in container(m)["volumeMounts"]
        assert {"name": "llm-ca", "configMap": {"name": "llm-ca", "optional": True}} in pod(m)["spec"]["volumes"]
    v = vllm_daemonset(dep(engine="vllm", file=None), M)
    assert container(v)["command"][:2] == ["/bin/sh", "-c"] and container(v)["command"][3] == "vllm"
    assert not {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"} & set(all_env(v))  # set by the wrapper, only with the file
    other = replicas_daemonset(dep(), "cpu", Settings(public_hostname="llm.example", hf_mirror_url="https://10.0.0.5/hf/"))
    assert all_env(other)["HF_ENDPOINT"]["value"] == "https://10.0.0.5/hf/c/$(NODE_NAME)"


def test_own_model_files_always_come_from_the_mirror_on_main():
    """local/<name> is served by the mirror only: even with HF_MIRROR_ENABLED=false the pods of an own model go there,
    with the mirror key, and llama.cpp gets the repository as it is (--hf-repo local/<name> --hf-file <file>)."""
    own = dict(repo="local/qwen3-235b", file="Qwen3-235B-A22B-Q2_K-00001-of-00002.gguf")
    off = Settings(node_image_tag="0.1.0", public_hostname="llm.example", hf_mirror_enabled=False, hf_token="hf_real")
    for m in (replicas_daemonset(dep(**own), "cpu", off), split_leader(dep(mode="split", **own), 0, group("a", "b"), off)):
        e = all_env(m)
        assert e["HF_ENDPOINT"]["value"] == "https://llm.example/hf/c/$(NODE_NAME)"
        assert secret_env(m)["HF_TOKEN"] == {"name": "llm-node-secrets", "key": "HF_MIRROR_TOKEN"}
        assert (env(m)["MODEL_REPO"], env(m)["MODEL_FILE"]) == (own["repo"], own["file"])
        assert "llm.nsu/hf-token" not in annotations(m)  # the real token is not used by these pods
        assert any(v["name"] == "llm-ca" for v in pod(m)["spec"]["volumes"])
    hf = replicas_daemonset(dep(), "cpu", off)  # a Hugging Face model of the same main still goes direct
    assert "HF_ENDPOINT" not in all_env(hf) and annotations(hf)["llm.nsu/hf-token"]


def test_direct_downloads_keep_the_old_contract():
    m = replicas_daemonset(dep(), "cpu", S)
    assert not {"HF_ENDPOINT", "NODE_NAME"} & set(all_env(m))
    assert all(v["name"] != "llm-ca" for v in pod(m)["spec"]["volumes"])


def test_rpc_pods_need_no_model_file_and_get_the_cache_limit():
    m = rpc_daemonset(dep(mode="split"), "cpu", M)
    assert not {"HF_ENDPOINT", "HF_TOKEN", "NODE_NAME"} & set(all_env(m))
    assert env(m)["RPC_CACHE_MAX_GIB"] == "64"
    assert env(rpc_daemonset(dep(mode="split"), "cpu", Settings(rpc_cache_max_gib=200)))["RPC_CACHE_MAX_GIB"] == "200"


def startup(manifest):
    return container(manifest)["startupProbe"]


def test_startup_probe_grows_with_the_model_size():
    small = replicas_daemonset(dep(meta={"arch": "gemma4", "weights_bytes": 5 * 10**9}), "cpu", M)
    assert startup(small) == {"httpGet": {"path": "/health", "port": 18080}, "periodSeconds": 10, "failureThreshold": 360}
    # 200 GB at 40 MB/s: download, then load (x2), with a 1.5 margin, plus 600 s
    big = dep(mode="split", meta={"arch": "deepseek2", "weights_bytes": 200 * 10**9})
    assert startup(split_leader(big, 0, group("a", "b"), M))["failureThreshold"] == (600 + 15000) // 10
    assert startup(replicas_daemonset(big, "cpu", M))["failureThreshold"] == 1560
    fast = Settings(model_load_mbps=100)
    assert startup(split_leader(big, 0, group("a", "b"), fast))["failureThreshold"] == (600 + 6000) // 10
    assert startup(vllm_daemonset(dep(engine="vllm", file=None, meta={"weights_bytes": 200 * 10**9}), M))["failureThreshold"] == 1560
    # size unknown (meta not read yet): budgeted as a 100 GB model rather than killed after an hour
    assert startup(replicas_daemonset(dep(meta=None), "cpu", M))["failureThreshold"] == (600 + 7500) // 10
    assert startup(replicas_daemonset(dep(meta={"arch": "llama"}), "cpu", M))["failureThreshold"] == (600 + 7500) // 10


CLASS_TOLERATION = {"key": "llm.nsu/class", "operator": "Equal", "value": "true", "effect": "NoSchedule"}


def test_every_class_pod_tolerates_the_class_taint():
    """Class nodes join with `llm.nsu/class=true:NoSchedule` (system pods stay on llm-main); every pod meant for a class
    node carries the matching toleration."""
    manifests = [
        replicas_daemonset(dep(), "cpu", S),
        replicas_daemonset(dep(), "nvidia", S),
        vllm_daemonset(dep(engine="vllm"), S),
        rpc_daemonset(dep(mode="split"), "cpu", S),
        rpc_daemonset(dep(mode="split"), "nvidia", S),
        split_leader(dep(mode="split"), 0, group("a", "b"), S),
        split_leader(dep(mode="split"), 1, group("c", "d", accel="nvidia"), S),
    ]
    for m in manifests:
        assert pod(m)["spec"]["tolerations"] == [CLASS_TOLERATION], m["metadata"]["name"]
    a, b = manifests[0], manifests[3]
    assert pod(a)["spec"]["tolerations"] is not pod(b)["spec"]["tolerations"]  # no shared mutable list


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_the_vllm_wrapper_sets_the_ca_bundle_only_when_the_configmap_is_there(tmp_path):
    import os

    c = container(vllm_daemonset(dep(engine="vllm", file=None), M))
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "vllm").write_text('#!/bin/sh\necho "args=$*"; echo "cert=${SSL_CERT_FILE:-none}"\n', encoding="utf-8")
    (fake / "vllm").chmod(0o755)
    bundle = tmp_path / "bundle.crt"

    def run():
        env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "LLM_CA_BUNDLE": str(bundle)}
        env.pop("SSL_CERT_FILE", None)
        return subprocess.run(["sh", *c["command"][1:], *c["args"][:3]], env=env, capture_output=True, text=True).stdout

    out = run()
    assert "args=serve org/model-GGUF --served-model-name gemma-4-e4b" in out and "cert=none" in out
    bundle.write_text("pem", encoding="utf-8")
    assert f"cert={bundle}" in run()


def test_node_pods_reference_only_the_node_secret():
    """A kubelet may read every Secret its pods reference (k3s Node authorizer): no pod built here (all run on nodes)
    names `llm-secrets`, which holds the LiteLLM master key, the salt, the database passwords and the real HF token."""
    for s in (S, M, direct(hf_token="hf_real"), Settings(node_image_tag="0.1.0", backend_api_key="k", hf_token="hf_real")):
        for m in (
            replicas_daemonset(dep(), "cpu", s),
            replicas_daemonset(dep(), "nvidia", s),
            vllm_daemonset(dep(engine="vllm", file=None), s),
            rpc_daemonset(dep(mode="split"), "cpu", s),
            rpc_daemonset(dep(mode="split"), "nvidia", s),
            split_leader(dep(mode="split"), 0, group("a", "b"), s),
            replicas_daemonset(dep(repo="local/own", file="own.gguf"), "cpu", s),
        ):
            assert "llm-secrets" not in str(m), m["metadata"]["name"]
            assert {r["name"] for r in secret_env(m).values()} <= {"llm-node-secrets"}


def test_plan_groups_new_group_leads_with_the_node_with_most_ram():
    """The leader keeps token_embd, its compute buffer and mmproj in RAM, and the last node's RAM holds no weights:
    a new group puts the node with the most RAM first and the one with the least last."""
    w = lambda n, ram: RpcWorker(n, "10.0.0.9", 18050, "nvidia", ram)  # noqa: E731
    groups = plan_groups([w("nsu-4060", 7793), w("nsu-4070", 10951)], 2)
    assert names_of(groups) == {0: ["nsu-4070", "nsu-4060"]}
    # without RAM the order stays by name, and a known RAM beats an unknown one
    assert names_of(plan_groups([w("b", None), w("a", None)], 2)) == {0: ["a", "b"]}
    assert names_of(plan_groups([w("a", None), w("b", 4096)], 2)) == {0: ["b", "a"]}
    # a running group keeps its order
    running = plan_groups([w("nsu-4060", 7793), w("nsu-4070", 10951)], 2, existing={0: ["nsu-4060", "nsu-4070"]})
    assert names_of(running) == {0: ["nsu-4060", "nsu-4070"]}
