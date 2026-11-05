"""Pure builders of Kubernetes manifests (DaemonSets and Deployments) for model deployments."""

import copy
import hashlib
import json
import logging
import math
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.catalog.placement import rpc_devices
from app.config import key_fingerprint
from app.deployments import llama_args
from app.localmodels.store import is_local
from app.nodes import versions
from app import mirror_tokens
from app.k8s import (
    A_GROUP_HASH,
    A_GROUP_MEMBERS,
    A_MAX_TOKENS,
    A_MODEL,
    A_PORT,
    A_SPEC_HASH,
    CLASS_TAINT,
    L_ACCEL,
    L_DEPLOYMENT,
    L_NODE_DEPLOYMENT,
    L_ROLE,
)

VLLM_IMAGE = "vllm/vllm-openai:v0.32.0"
MODELS_DIR = "/var/lib/llm-models"
VLLM_HF_CACHE = "/root/.cache/huggingface"
log = logging.getLogger(__name__)
PART_OF = "app.kubernetes.io/part-of"
NAME_LABEL = "app.kubernetes.io/name"
HOSTNAME_LABEL = "kubernetes.io/hostname"
GPU_RESOURCE = "nvidia.com/gpu"
# Every pod built here runs on a node, whose kubelet may read each Secret its pods reference (k3s Node authorizer):
# they reference only `llm-node-secrets` (main/manifests/common-10-secrets), never `llm-secrets` with main's own keys,
# and of it only HF_TOKEN with the mirror off. Neither the shared backend key nor the mirror's own key reaches a pod
# any more (task SEC-1): the backend key is the node's own, the mirror token names the deployment's repositories.
NODE_SECRETS_NAME = "llm-node-secrets"
HF_TOKEN_SECRET_KEY = "HF_TOKEN"  # set only with HF_MIRROR_ENABLED=false (main/render_manifests.py)
# The node's own backend key: registrar.sh writes it to $STATE_DIR/backend/key of the node container (= the host of the
# k3s agent, so a hostPath), derived from the node's key (app.nodes.identity.backend_key_of). Only that directory is
# mounted, never the state directory with the node's key itself.
BACKEND_KEY_HOST_DIR = "/var/lib/llm-node/backend"
BACKEND_KEY_DIR = "/run/llm-backend"
BACKEND_KEY_FILE = f"{BACKEND_KEY_DIR}/key"
# repos of every deployment, kept by the workload controller: serve-llama.sh deletes cached shards of other repos
MODEL_REPOS_CONFIGMAP = "llm-model-repos"
MODEL_REPOS_KEY = "repos"
CA_CONFIGMAP = "llm-ca"  # main's CA for https://<main>/hf (main/entrypoint.sh, render_manifests.py --ca)
CA_DIR = "/etc/llm-ca"
CA_BUNDLE = f"{CA_DIR}/bundle.crt"
# the node's backend key from its file (waits for the node container to write it, like serve-llama.sh); then, with the
# mirror, the CA bundle only when the optional llm-ca ConfigMap is there
VLLM_WRAPPER = (
    'i=0; while [ ! -s "$VLLM_API_KEY_FILE" ] && [ "$i" -lt 300 ]; do sleep 1; i=$((i+1)); done; '
    '[ -s "$VLLM_API_KEY_FILE" ] || { echo "no backend key in $VLLM_API_KEY_FILE: the node has not registered" >&2; exit 1; }; '
    'VLLM_API_KEY="$(cat "$VLLM_API_KEY_FILE")"; export VLLM_API_KEY; unset VLLM_API_KEY_FILE; '
    f'b="${{LLM_CA_BUNDLE:-{CA_BUNDLE}}}"; '
    'if [ -s "$b" ]; then export SSL_CERT_FILE="$b" REQUESTS_CA_BUNDLE="$b" CURL_CA_BUNDLE="$b"; fi; '
    'exec vllm serve "$@"'
)
A_HF_TOKEN = "llm.nsu/hf-token"  # fingerprint of the Hugging Face token the pods were started with

ROLE_INFERENCE = "inference"
ROLE_RPC = "rpc"
ROLE_LEADER = "split-leader"

_SUFFIX = {"cpu": "cpu", "nvidia": "gpu"}
# more than any model has layers: every block and the output layer go to the rpc devices
LEADER_NGL = "999"
RPC_MEMORY_REQUEST = "256Mi"  # a request equal to the large limit could leave the pod unschedulable
CLASS_TOLERATION = {"operator": "Equal", **CLASS_TAINT}  # class nodes: llm.nsu/class=true:NoSchedule
_GEMMA4_FLAGS = ["--enable-auto-tool-choice", "--tool-call-parser", "gemma4", "--reasoning-parser", "gemma4"]


@dataclass(frozen=True)
class RpcWorker:
    node_name: str
    ip: str
    port: int
    accel: str
    # RAM of the node (MiB), when known: within a new group the node with the most RAM leads (token_embd, the compute
    # buffer and mmproj sit in the leader's RAM) and the one with the least goes last (the last node's RAM holds no
    # weights, so the output layer lands on its GPU)
    ram_mib: int | None = None


def plan_groups(
    workers: list[RpcWorker],
    size: int,
    existing: dict[int, list[str]] | None = None,
    exclude: Iterable[str] = (),
    limit: int | None = None,
    reserved: Iterable[int] = (),
    mixed: bool = False,
) -> dict[int, list[RpcWorker]]:
    """Split groups by index (the leader is `g<index>`), stable across membership changes.

    A group in `existing` (index -> member names, leader first) is kept, with its index and member order, while all its
    members are among `workers`, share one accel (unless `mixed`) and the group still has `size` members. The other
    workers, minus `exclude`, form new groups: homogeneous accel, nvidia first, taken by node name and each group ordered by RAM (most first), under the lowest free
    indices. With `mixed` (a deployment on accel `any`) the workers left over by those then form groups of both accels
    together, nvidia first; the incomplete tail is dropped. So a node that leaves breaks up only its own group, and a
    node that joins waits for a partner. `limit` caps the number of groups: new ones are formed only while there is
    room, kept ones are never dropped for it (the workers left over are the deployment's spare nodes). `reserved`
    indices are never given to new groups.
    """
    alive = {w.node_name: w for w in workers}
    groups: dict[int, list[RpcWorker]] = {}
    used: set[str] = set()
    for index, names in sorted((existing or {}).items()):
        members = [alive.get(n) for n in names]
        if (
            len(names) != size
            or len(set(names)) != size
            or None in members
            or used.intersection(names)
            or (not mixed and len({w.accel for w in members}) != 1)
        ):
            continue
        groups[index] = members
        used.update(names)
    used.update(exclude)
    fresh: list[list[RpcWorker]] = []
    rest: list[RpcWorker] = []
    for accel in ("nvidia", "cpu"):
        free = sorted((w for w in workers if w.accel == accel and w.node_name not in used), key=lambda w: w.node_name)
        whole = len(free) - len(free) % size
        fresh += [
            sorted(free[i : i + size], key=lambda w: (-(w.ram_mib if w.ram_mib is not None else -1), w.node_name))
            for i in range(0, whole, size)
        ]
        rest += free[whole:]
    if mixed:
        fresh += [rest[i : i + size] for i in range(0, len(rest) - size + 1, size)]
    index = 0
    taken = set(reserved)
    for group in fresh:
        if limit is not None and len(groups) >= limit:
            break
        while index in groups or index in taken:
            index += 1
        groups[index] = group
    return dict(sorted(groups.items()))


def leader_groups(owned: Iterable, deployment: str) -> dict[int, list[str]]:
    """index -> member names of the split leaders of `deployment` that recorded their group (`OwnedWorkload`s)."""
    pattern = re.compile(rf"llm-{re.escape(deployment)}-g(\d+)")
    groups: dict[int, list[str]] = {}
    for o in owned:
        match = pattern.fullmatch(o.name)
        if o.kind == "Deployment" and o.deployment == deployment and match and o.group_members:
            groups[int(match.group(1))] = o.group_members.split(",")
    return groups


def template_hash(manifest: dict) -> str:
    """12 hex chars over the pod template without its `llm.nsu/spec-hash` annotation."""
    template = copy.deepcopy(manifest["spec"]["template"])
    (template.get("metadata") or {}).get("annotations", {}).pop(A_SPEC_HASH, None)
    raw = json.dumps(template, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def group_hash(group: list[RpcWorker]) -> str:
    raw = ",".join([w.node_name for w in group] + [w.ip for w in group])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def object_name(dep, kind: str) -> str:
    """Name of a workload object; kind is one of cpu, gpu, vllm, rpc-cpu, rpc-gpu, g<i>."""
    return f"llm-{dep.name}-{kind}"


def _tag_image(s, accel: str) -> str:
    return f"{s.node_image_repo}:{s.node_image_tag}-{_SUFFIX[accel]}"


def _image(s, accel: str) -> str:
    """The node image of the pods: `<repo>@sha256:...` while main's registry has the tag, so an image pushed under the same
    tag becomes a new pod template and reaches the nodes by itself, and the template stays the same while it does not;
    else the tag (versions.Registry knows the digest; the workload controller asks the registry before it builds)."""
    digest = versions.pinned_digest(s, accel)
    return f"{s.node_image_repo}@{digest}" if digest else _tag_image(s, accel)


def _pull_policy(s, accel: str) -> str:
    """IfNotPresent for a digest (it names one image) and while pinning is off; Always for the bare tag, which can move."""
    if not s.node_image_pin or versions.pinned_digest(s, accel):
        return "IfNotPresent"
    return "Always"


def _labels(dep, role: str) -> dict[str, str]:
    return {PART_OF: "nsu-llm", L_DEPLOYMENT: dep.name, L_ROLE: role}


def _finish(kind: str, name: str, namespace: str, labels: dict, selector: dict, pod_spec: dict, annotations: dict, extra_spec: dict) -> dict:
    """Assembles the workload and writes the template hash into the pod annotations."""
    pod_labels = {**labels, NAME_LABEL: name}
    manifest = {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            **extra_spec,
            "selector": {"matchLabels": {**selector, NAME_LABEL: name}},
            "template": {"metadata": {"labels": pod_labels, "annotations": dict(annotations)}, "spec": pod_spec},
        },
    }
    manifest["spec"]["template"]["metadata"]["annotations"][A_SPEC_HASH] = template_hash(manifest)
    return manifest


def _daemonset(
    dep, accel: str, role: str, name: str, s, container: dict, pod_extra: dict, annotations: dict, max_unavailable=1
) -> dict:
    labels = _labels(dep, role)
    pod_spec = {
        # the portal's label (a kubelet cannot set it on its own Node, k8s.L_NODE_DEPLOYMENT), set only on approved nodes
        "nodeSelector": {L_NODE_DEPLOYMENT: dep.name, L_ACCEL: accel},
        "hostNetwork": True,
        "dnsPolicy": "Default",
        "tolerations": [dict(CLASS_TOLERATION)],
        "containers": [container],
        **pod_extra,
    }
    if accel == "nvidia":
        pod_spec["runtimeClassName"] = "nvidia"
        container.setdefault("resources", {}).setdefault("limits", {})[GPU_RESOURCE] = 1
    return _finish(
        "DaemonSet", name, s.k8s_namespace, labels, labels, pod_spec, annotations,
        {"updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": max_unavailable}}},
    )


SAMPLING_FLAGS = {
    "temperature": "--temp", "top_p": "--top-p", "top_k": "--top-k", "min_p": "--min-p", "repeat_penalty": "--repeat-penalty",
}


EXTRA_ARGS_SEP = "\n"  # EXTRA_ARGS holds one token per line


def _number(value: float) -> str:
    """A sampling value as serve-llama.sh accepts it (digits and one dot, no exponent)."""
    return str(int(value)) if float(value).is_integer() else f"{float(value):.6f}".rstrip("0").rstrip(".")


def llama_settings_env(dep) -> list[tuple[str, str]]:
    """The llama.cpp settings of a deployment as the environment of serve-llama.sh (task 2-22b). Only the settings that
    differ from what the script does without them are passed, so a deployment made before them keeps its pod template
    (and its pods). The script turns them into flags and checks them again (see its header)."""
    env: list[tuple[str, str]] = []
    if (reasoning := getattr(dep, "reasoning", None)) and reasoning != "off":
        env.append(("THINKING", reasoning))
    if (kv := getattr(dep, "kv_cache_type", None)) and kv != "f16":
        env.append(("KV_CACHE_TYPE", kv))
    if sampling := getattr(dep, "sampling", None):
        pairs = [f"{flag} {_number(sampling[key])}" for key, flag in SAMPLING_FLAGS.items() if key in sampling]
        if pairs:
            env.append(("SAMPLING_ARGS", " ".join(pairs)))
    if (draft_repo := getattr(dep, "draft_repo", None)) and (draft_file := getattr(dep, "draft_file", None)):
        env += [("DRAFT_REPO", draft_repo), ("DRAFT_FILE", draft_file)]
        if draft_revision := getattr(dep, "draft_revision", None):  # pinned by the portal, like `revision`
            env.append(("DRAFT_REVISION", draft_revision))
    if mmproj := getattr(dep, "mmproj_file", None):
        env.append(("MMPROJ_FILE", mmproj))
    if extra := getattr(dep, "extra_args", None):
        try:
            # one token per line: a quoted value with a space stays one token; the node checks the same rules
            env.append(("EXTRA_ARGS", EXTRA_ARGS_SEP.join(llama_args.validate_extra_args(extra))))
        except llama_args.ExtraArgsInvalid as exc:  # stored before a stricter list: the server runs without them
            log.warning("deployment %s: own llama-server flags ignored (%s)", dep.name, exc)
    return env


def _llama_env(dep, s, accel: str) -> list[dict]:
    env = [
        ("MODEL_REPO", dep.repo),
        ("MODEL_FILE", dep.file),
        ("PUBLIC_MODEL_NAME", dep.name),
        ("INFER_PORT", str(s.infer_port)),
        ("CTX_SIZE", str(dep.ctx_per_request * dep.parallel)),
        ("PARALLEL_SLOTS", str(dep.parallel)),
        ("ACCEL", accel),
    ]
    if getattr(dep, "revision", None):  # the pinned commit: serve-llama.sh downloads resolve/<sha>
        env.append(("MODEL_REVISION", dep.revision))
    env += llama_settings_env(dep)
    # read when the container starts (optional: the pod also starts before the controller wrote it), so a new
    # deployment changes no pod template
    keep = {"configMapKeyRef": {"name": MODEL_REPOS_CONFIGMAP, "key": MODEL_REPOS_KEY, "optional": True}}
    return [{"name": k, "value": v} for k, v in env] + [{"name": "KEEP_MODEL_REPOS", "valueFrom": keep}]


def has_local_source(repo: str | None, draft_repo: str | None = None, mmproj_file: str | None = None) -> bool:
    """One of the files of a deployment is an own file (`local/<name>`): it exists on the mirror of main only."""
    return bool(is_local(repo) or is_local(draft_repo) or is_local(mmproj_file))


def deployment_repos(dep) -> set[str]:
    """Every repository whose files the pods of `dep` keep in the cache: the model, its draft model and the repository of
    an own projector (a projector of the model's repository is already the model's)."""
    repos = {dep.repo}
    if draft := getattr(dep, "draft_repo", None):
        repos.add(draft)
    mmproj = getattr(dep, "mmproj_file", None)
    if is_local(mmproj):
        repos.add("local/" + mmproj[len("local/"):].partition("/")[0])
    return repos


def uses_mirror(dep, s) -> bool:
    """The pods of `dep` download through the mirror on main: always when the model, its draft or its projector is an
    own file (local/<name> exists nowhere else), otherwise when HF_MIRROR_ENABLED."""
    return s.hf_mirror_enabled or has_local_source(
        dep.repo, getattr(dep, "draft_repo", None), getattr(dep, "mmproj_file", None)
    )


def _with_secrets(env: list[dict], s, key_env: str, mirror: bool, dep=None) -> list[dict]:
    """Adds the Hugging Face access and the node's own backend key; no secret value is written into the pod object.

    With the mirror on main the pod downloads from `<mirror>/c/<node>` (the node name tags its transfers for the
    progress report) with a mirror token that opens the repositories of `dep` only (app.mirror_tokens: a node never holds
    the mirror's own key, which would open everything the operator's HF_TOKEN sees). Otherwise from Hugging Face with the
    real token, if any, as a reference to `llm-node-secrets`.
    The backend key is the file `<key_env>_FILE` names (the node's own, BACKEND_KEY_FILE); the server reads it into its
    own environment variable (`LLAMA_API_KEY` for llama-server, `VLLM_API_KEY` for vLLM), never a command line.
    """
    env = list(env)
    if mirror:
        repos = deployment_repos(dep) if dep is not None else set()
        token = mirror_tokens.mint(s.hf_mirror_token.get_secret_value(), mirror_tokens.ANY_CLIENT, repos)
        env += [
            {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
            {"name": "HF_ENDPOINT", "value": f"{s.node_hf_endpoint}/c/$(NODE_NAME)"},
            {"name": "HF_TOKEN", "value": token},
        ]
    elif s.hf_token.get_secret_value():
        # optional: a Secret rendered by an older main has no HF_TOKEN key, and the pod must still start
        ref = {"name": NODE_SECRETS_NAME, "key": HF_TOKEN_SECRET_KEY, "optional": True}
        env.append({"name": "HF_TOKEN", "valueFrom": {"secretKeyRef": ref}})
    return env + [{"name": f"{key_env}_FILE", "value": BACKEND_KEY_FILE}]


def _backend_key_volume() -> tuple[dict, dict]:
    """The node's backend key directory (hostPath of the node container), read-only. DirectoryOrCreate: a pod that starts
    before the node container wrote its key waits for the file (serve-llama.sh) instead of failing to mount."""
    mount = {"name": "backend-key", "mountPath": BACKEND_KEY_DIR, "readOnly": True}
    volume = {"name": "backend-key", "hostPath": {"path": BACKEND_KEY_HOST_DIR, "type": "DirectoryOrCreate"}}
    return mount, volume


def _inference_annotations(dep, s) -> dict[str, str]:
    annotations = {
        A_PORT: str(s.infer_port),
        A_MODEL: dep.name,
        A_MAX_TOKENS: str(dep.ctx_per_request),
    }
    hf_token = s.hf_token.get_secret_value()
    if hf_token and not uses_mirror(dep, s):
        annotations[A_HF_TOKEN] = key_fingerprint(hf_token)  # same reason: a new token restarts the pods
    return annotations


PROBE_PERIOD_S = 10
MIN_STARTUP_S = 3600
STARTUP_BASE_S = 600
STARTUP_MARGIN = 1.5


UNKNOWN_SIZE_BYTES = 100 * 10**9  # meta not read yet (Hugging Face was unreachable): assume a big model


def startup_budget_s(dep, s) -> int:
    """Seconds an inference pod may take to answer /health: the weights cross the network twice (the mirror -> the
    node, then for a split leader the node -> the rpc servers; a replica reads them from disk instead), at
    `model_load_mbps`, with a margin.

    `model_load_mbps` is the rate one pod gets: replicas that download at the same time share the mirror's link of
    main, so for N of them on 1 GbE it is about 110 / N MB/s (docs/hf-mirror.md). It is not divided here, because the
    number of nodes would then be part of the pod template and every new assignment would restart all replicas.
    """
    size = int((dep.meta or {}).get("weights_bytes") or 0) or UNKNOWN_SIZE_BYTES
    seconds = STARTUP_BASE_S + math.ceil(2 * size / (s.model_load_mbps * 1e6) * STARTUP_MARGIN)
    return max(MIN_STARTUP_S, seconds)


def _http_probes(port: int, startup_s: int = MIN_STARTUP_S) -> dict:
    return {
        "readinessProbe": {"httpGet": {"path": "/health", "port": port}, "periodSeconds": PROBE_PERIOD_S},
        "startupProbe": {
            "httpGet": {"path": "/health", "port": port}, "periodSeconds": PROBE_PERIOD_S,
            "failureThreshold": math.ceil(startup_s / PROBE_PERIOD_S),
        },
    }


def _models_volume() -> tuple[dict, dict]:
    mount = {"name": "models", "mountPath": MODELS_DIR}
    volume = {"name": "models", "hostPath": {"path": MODELS_DIR, "type": "DirectoryOrCreate"}}
    return mount, volume


def _ca_volume(mirror: bool) -> tuple[list[dict], list[dict]]:
    """main's CA (ConfigMap llm-ca) for the https address of the mirror; optional, so a pod starts without it."""
    if not mirror:
        return [], []
    mount = {"name": "llm-ca", "mountPath": CA_DIR, "readOnly": True}
    volume = {"name": "llm-ca", "configMap": {"name": CA_CONFIGMAP, "optional": True}}
    return [mount], [volume]


def replicas_daemonset(dep, accel: str, s) -> dict:
    """llama.cpp replica per node of the given accel (`cpu` or `nvidia`)."""
    mount, volume = _models_volume()
    key_mount, key_volume = _backend_key_volume()
    ca_mounts, ca_volumes = _ca_volume(uses_mirror(dep, s))
    container = {
        "name": "llama",
        "image": _image(s, accel),
        "imagePullPolicy": _pull_policy(s, accel),
        "command": ["/opt/llm/bin/serve-llama.sh"],
        "env": _with_secrets(_llama_env(dep, s, accel), s, "LLAMA_API_KEY", uses_mirror(dep, s), dep),
        "ports": [{"containerPort": s.infer_port, "hostPort": s.infer_port}],
        "volumeMounts": [mount, key_mount, *ca_mounts],
        **_http_probes(s.infer_port, startup_budget_s(dep, s)),
    }
    return _daemonset(
        dep, accel, ROLE_INFERENCE, object_name(dep, _SUFFIX[accel]), s, container,
        {"volumes": [volume, key_volume, *ca_volumes]}, _inference_annotations(dep, s),
    )


def vllm_daemonset(dep, s) -> dict:
    """Experimental vLLM replica on NVIDIA nodes. The command is `vllm serve`, the model is its positional argument."""
    arch = (dep.meta or {}).get("arch") or ""
    args = [
        dep.repo,
        "--served-model-name", dep.name,
        "--host", "0.0.0.0",
        "--port", str(s.infer_port),
        "--max-model-len", str(dep.ctx_per_request),
        "--max-num-seqs", str(dep.parallel),
        # not the recipes' 0.90: the CUDA context, the sampler and a desktop on the same GPU need the rest
        "--gpu-memory-utilization", "0.85",
    ]
    if getattr(dep, "revision", None):
        args += ["--revision", dep.revision]
    if arch.startswith("gemma4"):
        args += _GEMMA4_FLAGS
    ca_mounts, ca_volumes = _ca_volume(uses_mirror(dep, s))
    key_mount, key_volume = _backend_key_volume()
    # a shell reads the node's backend key from its file and, with the mirror, sets the CA bundle only when the optional
    # llm-ca ConfigMap is there (an SSL_CERT_FILE naming a missing file would break every TLS connection of vLLM);
    # `$0` is "vllm", the args follow as "$@"
    command = ["/bin/sh", "-c", VLLM_WRAPPER, "vllm"]
    container = {
        "name": "vllm",
        "image": VLLM_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "command": command,
        "args": args,
        "env": _with_secrets([], s, "VLLM_API_KEY", uses_mirror(dep, s), dep),
        "ports": [{"containerPort": s.infer_port, "hostPort": s.infer_port}],
        "volumeMounts": [
            {"name": "hf-cache", "mountPath": VLLM_HF_CACHE}, {"name": "shm", "mountPath": "/dev/shm"}, key_mount, *ca_mounts,
        ],
        **_http_probes(s.infer_port, startup_budget_s(dep, s)),
    }
    volumes = [
        {"name": "hf-cache", "hostPath": {"path": f"{MODELS_DIR}/huggingface", "type": "DirectoryOrCreate"}},
        {"name": "shm", "emptyDir": {"medium": "Memory"}},
        key_volume,
        *ca_volumes,
    ]
    return _daemonset(
        dep, "nvidia", ROLE_INFERENCE, object_name(dep, "vllm"), s, container, {"volumes": volumes}, _inference_annotations(dep, s)
    )


def _listening_probe(port: int) -> dict:
    """Ready while something listens on `port`, checked in /proc without connecting.

    ggml-rpc-server serves one client at a time: a TCP probe waits behind the split leader's connection, times out,
    and the worker turns NotReady exactly while it serves (the controller then rebuilds the group).
    """
    listening = f":{port:04X} [0:]+ 0A"  # local port, zero remote address, state LISTEN
    return {
        "exec": {"command": ["grep", "-qsE", listening, "/proc/net/tcp", "/proc/net/tcp6"]},
        "periodSeconds": 10,
        "timeoutSeconds": 5,  # the 1 s default fails under full CPU load and makes a busy server NotReady
    }


def rpc_daemonset(dep, accel: str, s, memory_limit_mib: int | None = None) -> dict:
    """ggml-rpc-server per node of the given accel; on NVIDIA nodes this pod owns the GPU.

    It offers the devices of `placement.rpc_devices` (with `use_node_ram` an NVIDIA node also gives its RAM) and keeps
    a tensor cache (`-c`) on the models volume, so a restarted group does not send the weights over the network
    again. `memory_limit_mib` (`placement.rpc_memory_limit_mib`) bounds a RAM device that took more than its budget.

    A new template replaces every rpc pod at once (maxUnavailable 100%): the controller forms groups only from servers
    of the current template, so the groups reload once instead of once per node. ggml-rpc-server runs as PID 1 and
    ignores SIGTERM, hence the short termination grace.
    """
    mount, volume = _models_volume()
    env = {
        "RPC_PORT": str(s.rpc_port),
        "RPC_DEVICES": rpc_devices(accel, dep.use_node_ram),
        "RPC_CACHE": "1",
        "LLAMA_CACHE": MODELS_DIR,
        "RPC_CACHE_MAX_GIB": str(s.rpc_cache_max_gib),  # rpc-cache-gc.sh: LRU of the -c cache
    }
    container = {
        "name": "rpc",
        "image": _image(s, accel),
        "imagePullPolicy": _pull_policy(s, accel),
        "command": ["/opt/llm/bin/serve-rpc.sh"],
        "env": [{"name": k, "value": v} for k, v in env.items()],
        "ports": [{"containerPort": s.rpc_port, "hostPort": s.rpc_port}],
        "volumeMounts": [mount],
        "readinessProbe": _listening_probe(s.rpc_port),
    }
    if memory_limit_mib is not None:
        container["resources"] = {"limits": {"memory": f"{memory_limit_mib}Mi"}, "requests": {"memory": RPC_MEMORY_REQUEST}}
    return _daemonset(
        dep, accel, ROLE_RPC, object_name(dep, f"rpc-{_SUFFIX[accel]}"), s, container,
        {"volumes": [volume], "terminationGracePeriodSeconds": 5}, {A_PORT: str(s.rpc_port)}, max_unavailable="100%",
    )


def split_leader(dep, index: int, group: list[RpcWorker], s, tensor_split: list[int] | None = None) -> dict:
    """llama-server for one split group, pinned to the first node of the group.

    The leader has no GPU of its own: every layer is offloaded to the rpc servers (its own node's one over loopback).
    On an nvidia group it runs the image that node has (`-gpu`) with the nvidia runtime (for libcuda) but
    `NVIDIA_VISIBLE_DEVICES=none` and no GPU limit: the GPU stays with the rpc pod. `tensor_split` (layers per rpc
    device, `placement.plan_split`) becomes `--tensor-split`; without it llama.cpp splits by the devices' free memory.
    """
    accel = group[0].accel
    mount, volume = _models_volume()
    key_mount, key_volume = _backend_key_volume()
    ca_mounts, ca_volumes = _ca_volume(uses_mirror(dep, s))
    rpc_servers = ",".join([f"127.0.0.1:{s.rpc_port}"] + [f"{w.ip}:{w.port}" for w in group[1:]])
    env = _llama_env(dep, s, "cpu") + [
        {"name": "NGL", "value": LEADER_NGL},
        {"name": "RPC_SERVERS", "value": rpc_servers},
    ]
    if tensor_split is not None:
        env.append({"name": "TENSOR_SPLIT", "value": ",".join(str(n) for n in tensor_split)})
    if accel == "nvidia":
        env.append({"name": "NVIDIA_VISIBLE_DEVICES", "value": "none"})
    container = {
        "name": "llama",
        "image": _image(s, accel),
        "imagePullPolicy": _pull_policy(s, accel),
        "command": ["/opt/llm/bin/serve-llama.sh"],
        "env": _with_secrets(env, s, "LLAMA_API_KEY", uses_mirror(dep, s), dep),
        "ports": [{"containerPort": s.infer_port, "hostPort": s.infer_port}],
        "volumeMounts": [mount, key_mount, *ca_mounts],
        **_http_probes(s.infer_port, startup_budget_s(dep, s)),
    }
    pod_spec = {
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    # by the Node's name, which a kubelet cannot choose for another node (a label it can set itself)
                    "nodeSelectorTerms": [
                        {"matchFields": [{"key": "metadata.name", "operator": "In", "values": [group[0].node_name]}]}
                    ]
                }
            }
        },
        "hostNetwork": True,
        "dnsPolicy": "Default",
        "tolerations": [dict(CLASS_TOLERATION)],
        "containers": [container],
        "volumes": [volume, key_volume, *ca_volumes],
    }
    if accel == "nvidia":
        pod_spec["runtimeClassName"] = "nvidia"
    labels = _labels(dep, ROLE_LEADER)
    annotations = {
        **_inference_annotations(dep, s),
        A_GROUP_HASH: group_hash(group),
        A_GROUP_MEMBERS: ",".join(w.node_name for w in group),
    }
    return _finish(
        "Deployment", object_name(dep, f"g{index}"), s.k8s_namespace, labels, labels, pod_spec, annotations,
        {"replicas": 1, "strategy": {"type": "Recreate"}},
    )


ROLE_WAKE = "wake"
L_WAKE_TARGET = "llm.nsu/wake-target"


def wake_job(relay: str, accel: str | None, target: str, mac: str, broadcast: str, s) -> dict:
    """A one-shot Job on the relay node that sends the magic packet of `target` to the broadcast of their common subnet
    (wol-send.sh of the node image, perl-base only). hostNetwork: the packet leaves on the PC's own network, not the pod
    network; no privilege, no service account token, no secrets; Kubernetes collects the finished Job."""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "generateName": "llm-wake-",
            "namespace": s.k8s_namespace,
            "labels": {PART_OF: "nsu-llm", L_ROLE: ROLE_WAKE, L_WAKE_TARGET: target},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 60,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {PART_OF: "nsu-llm", L_ROLE: ROLE_WAKE, L_WAKE_TARGET: target}},
                "spec": {
                    "nodeName": relay,
                    "hostNetwork": True,
                    "dnsPolicy": "Default",
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "tolerations": [dict(CLASS_TOLERATION)],
                    "containers": [
                        {
                            "name": "wake",
                            "image": _tag_image(s, accel if accel in _SUFFIX else "cpu"),  # the name the node image has on the node
                            "imagePullPolicy": "Never",
                            "command": ["wol-send.sh", mac, broadcast],
                            "securityContext": {"allowPrivilegeEscalation": False, "privileged": False},
                            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "64Mi"}},
                        }
                    ],
                },
            },
        },
    }

ROLE_PURGE = "purge"
L_PURGE_NODE = "llm.nsu/purge-node"
PURGE_DEADLINE_S = 900  # removing tens of GB from a slow disk may take minutes


def purge_job(node: str, accel: str | None, args: list[str], s) -> dict:
    """A one-shot Job pinned to a class node that runs models-cleanup.sh of the node image on its models volume (task
    2-26): the volume is the node's `llm-models`, which the pods see as the hostPath /var/lib/llm-models, on WSL and
    Docker Desktop nodes alike. It needs no pod of the deployment and no listener or token on the node. hostNetwork like
    every pod here (no dependence on the pod network of a PC), no privilege, no service account token, no secrets;
    Kubernetes collects the finished Job, the portal reads the `RESULT` line of its log first. `args` are the
    arguments of the script (`--keep repo:file`, `--keep-rpc`, ...); the image is the node image the portal is
    configured with, pulled from main's registry if the node has not got it."""
    mount, volume = _models_volume()
    labels = {PART_OF: "nsu-llm", L_ROLE: ROLE_PURGE, L_PURGE_NODE: node}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"generateName": "llm-purge-", "namespace": s.k8s_namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": PURGE_DEADLINE_S,
            "ttlSecondsAfterFinished": 900,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "nodeName": node,
                    "hostNetwork": True,
                    "dnsPolicy": "Default",
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "tolerations": [dict(CLASS_TOLERATION)],
                    "containers": [
                        {
                            "name": "purge",
                            "image": _image(s, accel if accel in _SUFFIX else "cpu"),
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/opt/llm/bin/models-cleanup.sh", *args],
                            "env": [{"name": "LLAMA_CACHE", "value": MODELS_DIR}],
                            "securityContext": {"allowPrivilegeEscalation": False, "privileged": False},
                            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "256Mi"}},
                            "volumeMounts": [mount],
                        }
                    ],
                    "volumes": [volume],
                },
            },
        },
    }


# ---------- the command of llama-server, for the admin's preview (task 2-22b) ----------

SECRET_MASK = "***"
_SHARD = re.compile(r"^(.*)-00001-of-(\d{5})\.gguf$")


def _env_values(env: list[dict]) -> dict[str, str]:
    return {e["name"]: e["value"] for e in env if "value" in e}


def _download_files(repo: str, file: str) -> list[str]:
    """The files of a GGUF in the cache of the node (every shard of a multi-file one); the first one is the model."""
    base = f"{MODELS_DIR}/{repo.replace('/', '__')}"
    match = _SHARD.match(file)
    if not match:
        return [f"{base}/{file}"]
    prefix, total = match.group(1), int(match.group(2))
    return [f"{base}/{prefix}-{i:05d}-of-{total:05d}.gguf" for i in range(1, total + 1)]


def serve_llama_argv(env: dict[str, str], threads: str = "$THREADS") -> list[str]:
    """The llama-server command line serve-llama.sh builds from this environment. It mirrors the script's flags (the
    flags live in the script because the standalone nodes start it too); a test runs the script's `--print-cmd` on the
    same environments and compares, so the two do not drift apart."""
    repo, file, accel = env["MODEL_REPO"], env["MODEL_FILE"], env.get("ACCEL", "cpu")
    rpc = env.get("RPC_SERVERS", "")
    revision = "" if is_local(repo) else env.get("MODEL_REVISION", "")
    ngl = env.get("NGL") or ("99" if rpc or accel == "nvidia" else "0")
    cmd = [
        "llama-server", "--host", "0.0.0.0", "--port", env.get("INFER_PORT", "18080"),
        "--jinja", "--metrics", "--cache-ram", "512", "-fa", "auto",
        "--ctx-size", env.get("CTX_SIZE", "8192"), "--parallel", env.get("PARALLEL_SLOTS", "1"), "--no-kv-unified",
        "-ngl", ngl, "-t", threads, "--alias", env.get("PUBLIC_MODEL_NAME") or file.removesuffix(".gguf"),
    ]
    if rpc:
        cmd += ["--rpc", rpc, "--fit", "off"]
        if env.get("TENSOR_SPLIT"):
            cmd += ["--tensor-split", env["TENSOR_SPLIT"]]
    cmd += ["--reasoning", env.get("THINKING", "off")]
    if (kv := env.get("KV_CACHE_TYPE", "f16")) != "f16":
        cmd += ["--cache-type-k", kv, "--cache-type-v", kv]
    if env.get("SAMPLING_ARGS"):
        cmd += env["SAMPLING_ARGS"].split()
    if _SHARD.match(file) or revision:
        cmd += ["-m", _download_files(repo, file)[0]]
    else:
        cmd += ["--hf-repo", repo, "--hf-file", file]
    if mmproj := env.get("MMPROJ_FILE"):
        match = re.fullmatch(r"local/([^/]+)/(.+)", mmproj)
        cmd += ["--mmproj", _download_files(f"local/{match.group(1)}" if match else repo, match.group(2) if match else mmproj)[0]]
        if rpc:  # on a split leader the only GPU-type devices are the rpc servers: the projector stays on the leader
            cmd.append("--no-mmproj-offload")
    else:
        cmd.append("--no-mmproj")
    if draft_repo := env.get("DRAFT_REPO"):
        cmd += ["-md", _download_files(draft_repo, env["DRAFT_FILE"])[0], "--spec-type", "draft-simple", "--spec-draft-n-max", "4", "--spec-draft-n-min", "1"]
        cmd += ["-ngld", "99" if not rpc and accel == "nvidia" else "0"]
    if env.get("EXTRA_ARGS"):
        cmd += env["EXTRA_ARGS"].split(EXTRA_ARGS_SEP)
    return cmd


def _line(tokens: list[str], env: dict[str, str] | None = None) -> str:
    prefix = [f"{k}={v}" for k, v in (env or {}).items()]
    return " ".join(prefix + [shlex.quote(t) if re.search(r"[^\w@%+=:,./<>$-]", t) else t for t in tokens])


def command_preview(dep, s) -> dict[str, Any]:
    """The command of the server pods of a deployment as the admin would read it: `{"leader": str, "rpc": str | None,
    "notes": [str]}`. Built by the manifest builders' own environment; keys and tokens are masked."""
    notes: list[str] = []
    if dep.engine == "vllm":
        args = vllm_daemonset(dep, s)["spec"]["template"]["spec"]["containers"][0]["args"]
        return {"leader": _line(["vllm", "serve", *args], {"VLLM_API_KEY": SECRET_MASK}), "rpc": None, "notes": notes}
    secrets = {"LLAMA_API_KEY": SECRET_MASK}
    if uses_mirror(dep, s):
        secrets |= {"HF_ENDPOINT": f"{s.node_hf_endpoint}/c/<нода>", "HF_TOKEN": SECRET_MASK}
    elif s.hf_token.get_secret_value():
        secrets["HF_TOKEN"] = SECRET_MASK
    split = dep.mode == "split"
    accel = "nvidia" if dep.accel == "nvidia" else "cpu"
    values = _env_values(_llama_env(dep, s, "cpu" if split else accel))
    rpc = None
    if split:
        size = max(dep.group_size or 2, 2)
        servers = [f"127.0.0.1:{s.rpc_port}"] + [f"<ip-{i}>:{s.rpc_port}" for i in range(2, size + 1)]
        values |= {"NGL": LEADER_NGL, "RPC_SERVERS": ",".join(servers), "TENSOR_SPLIT": "<layers>"}
        devices = rpc_devices("nvidia" if dep.accel != "cpu" else "cpu", dep.use_node_ram)
        rpc = _line(
            ["ggml-rpc-server", "-H", "0.0.0.0", "-p", str(s.rpc_port), "-t", "$THREADS", "-d", devices, "-c"],
            {"LLAMA_CACHE": MODELS_DIR},
        )
        notes += [
            "Лидер группы запускается на первой ноде, остальные ноды держат только rpc-сервер.",
            "<ip-N> это адрес N-й ноды группы, <layers> раскладка слоёв по устройствам (--tensor-split): портал считает её по памяти нод в момент запуска группы.",
            "Черновая модель и проектор изображений загружаются только лидером и лежат в RAM его ноды.",
        ]
    elif dep.accel == "any":
        notes.append("Показана команда для CPU-ноды; на ноде с видеокартой -ngl 99" + (" и -ngld 99." if getattr(dep, "draft_repo", None) else "."))
    notes.append("-t: число ядер ноды без одного, KEEP_MODEL_REPOS и HF_ENDPOINT нода получает от портала.")
    files = [(dep.repo, dep.file)] if (_SHARD.match(dep.file or "") or getattr(dep, "revision", None)) else []
    if mmproj := getattr(dep, "mmproj_file", None):
        files.append((dep.repo, mmproj))
    if draft_repo := getattr(dep, "draft_repo", None):
        files.append((draft_repo, getattr(dep, "draft_file", None) or ""))
    if files:
        notes.append(
            "Перед запуском нода скачивает "
            + ", ".join(f"{r}/{f}" for r, f in files)
            + (" через зеркало главного." if uses_mirror(dep, s) else " с Hugging Face.")
        )
    return {"leader": _line(serve_llama_argv(values), secrets), "rpc": rpc, "notes": notes}
