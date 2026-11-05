import re
import sys
from pathlib import Path

import pytest
import yaml

MAIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MAIN))

import render_manifests  # noqa: E402

ENV = {
    "POSTGRES_PASSWORD": "pg-root-secret",
    "LITELLM_DB_PASSWORD": "ll-db-secret",
    "PORTAL_DB_PASSWORD": "portal-db-secret",
    "LITELLM_MASTER_KEY": "sk-master-secret",
    "LITELLM_SALT_KEY": "salt-secret",
    "BACKEND_API_KEY": "backend-secret",
    "HF_MIRROR_TOKEN": "hf_" + "ab" * 20,
    "REDIS_PASSWORD": "redis-secret",
    "HF_MIRROR_ADMIN_TOKEN": "mirror-admin-secret",
    "REGISTRY_HTPASSWD": "nsu-push:$2a$06$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
}
EXPECTED = [
    "common-00-namespace.yaml",
    "common-10-secrets.yaml",
    "common-20-postgres.yaml",
    "common-25-redis.yaml",
    "common-30-litellm.yaml",
    "common-40-hf-mirror.yaml",
    "common-45-nvidia-device-plugin.yaml",
    "common-50-registry.yaml",
]


def docs_of(path):
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def all_docs(out):
    return {p.name: docs_of(p) for p in sorted(out.glob("common-*.yaml"))}


@pytest.fixture
def out(tmp_path):
    render_manifests.render(ENV, tmp_path)
    return tmp_path


def test_renders_exactly_common_files(tmp_path):
    written = render_manifests.render(ENV, tmp_path)
    assert sorted(p.name for p in written) == EXPECTED
    assert sorted(p.name for p in tmp_path.iterdir()) == EXPECTED
    assert not any("example" in p.name or "lws" in p.name for p in written)


def test_every_file_starts_with_namespace(out):
    for name, docs in all_docs(out).items():
        assert docs[0]["kind"] == "Namespace", name
        assert docs[0]["metadata"]["name"] == "llm", name


def find(docs_by_file, kind, name):
    for docs in docs_by_file.values():
        for d in docs:
            if d["kind"] == kind and d["metadata"]["name"] == name:
                return d
    raise AssertionError(f"{kind}/{name} not found")


MAIN_TOLERATION = {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}


def assert_main_only(spec):
    """SEC-2 H1: a pod of main selects llm-main by the name of the Node object (a node cannot rename itself), not by the
    kubernetes.io/hostname label its own kubelet sets, and tolerates the taint main puts on itself."""
    assert "nodeSelector" not in spec
    terms = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert terms == [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": ["llm-main"]}]}]
    assert spec["tolerations"] == [MAIN_TOLERATION]


def _pod_specs(by_file):
    for name, docs in by_file.items():
        for d in docs:
            spec = d.get("spec") or {}
            if d["kind"] == "CronJob":
                yield name, d, spec["jobTemplate"]["spec"]["template"]["spec"]
            elif "template" in spec:
                yield name, d, spec["template"]["spec"]


def test_every_pod_of_main_is_pinned_to_main_by_node_name(out):
    """H1: no pod of main can land on a node that labelled itself kubernetes.io/hostname=llm-main; only the GPU device
    plugin (a DaemonSet without secrets) runs on class nodes."""
    by_file = all_docs(out)
    seen = set()
    for name, d, spec in _pod_specs(by_file):
        if d["kind"] == "DaemonSet":
            assert "llm-secrets" not in yaml.safe_dump(spec)
            continue
        assert_main_only(spec)
        seen.add(d["metadata"]["name"])
    assert {"postgres", "postgres-backup", "redis", "litellm", "hf-mirror", "registry"} <= seen
    for name, docs in by_file.items():
        assert "kubernetes.io/hostname" not in yaml.safe_dump_all(docs), name


def test_documents_parse_and_follow_conventions(out):
    by_file = all_docs(out)
    for docs in by_file.values():
        for d in docs:
            assert d["kind"] and d["apiVersion"]
            if d["kind"] in ("StatefulSet", "Deployment") and d["metadata"]["namespace"] == "llm":
                assert d["metadata"]["labels"]["app.kubernetes.io/part-of"] == "nsu-llm"
                tpl = d["spec"]["template"]
                assert tpl["metadata"]["labels"]["app.kubernetes.io/part-of"] == "nsu-llm"

    for kind, name in (("StatefulSet", "postgres"), ("Deployment", "litellm"), ("Deployment", "redis")):
        spec = find(by_file, kind, name)["spec"]["template"]["spec"]
        assert_main_only(spec)
        assert spec["hostNetwork"] is True

    litellm = find(by_file, "Deployment", "litellm")["spec"]["template"]["spec"]["containers"][0]
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert litellm[probe]["httpGet"]["host"] == "127.0.0.1", probe
    assert litellm["startupProbe"]["httpGet"]["path"] == "/health/liveliness"
    assert litellm["startupProbe"]["periodSeconds"] == 10
    assert litellm["startupProbe"]["failureThreshold"] == 60
    assert litellm["readinessProbe"]["httpGet"]["path"] == "/health/readiness"
    assert litellm["livenessProbe"]["httpGet"]["path"] == "/health/liveliness"
    # a busy event loop answers slowly: the default 1 s timeout would restart LiteLLM under load and cut every stream
    assert litellm["livenessProbe"]["timeoutSeconds"] == 10
    assert litellm["livenessProbe"]["failureThreshold"] == 6
    assert litellm["readinessProbe"]["timeoutSeconds"] == 5
    assert litellm["image"] == "ghcr.io/berriai/litellm:v1.106.6"
    assert litellm["args"] == [
        "--config", "/etc/litellm/config.yaml", "--host", "127.0.0.1",
        "--port", "4000", "--num_workers", "4",
    ]
    env = {e["name"]: e for e in litellm["env"]}
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "llm-secrets", "key": "LITELLM_DATABASE_URL"}
    for k in ("LITELLM_MASTER_KEY", "LITELLM_SALT_KEY"):
        assert env[k]["valueFrom"]["secretKeyRef"]["name"] == "llm-secrets"
    assert {k: env[k]["value"] for k in (
        "STORE_MODEL_IN_DB", "DISABLE_ADMIN_UI", "NO_DOCS", "NO_REDOC",
        "LITELLM_TELEMETRY_DISABLED", "LITELLM_LOCAL_MODEL_COST_MAP")} == {
        "STORE_MODEL_IN_DB": "True", "DISABLE_ADMIN_UI": "True", "NO_DOCS": "True",
        "NO_REDOC": "True", "LITELLM_TELEMETRY_DISABLED": "true",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    # idle backend connections are dropped before llama-server's 5 s keep-alive closes them (no "Server disconnected"),
    # and a key lookup that waits for a busy worker's event loop is not reported as an unreachable database
    assert env["AIOHTTP_KEEPALIVE_TIMEOUT"]["value"] == "4"
    assert env["PROXY_DB_LOOKUP_DEADLINE_SECONDS"]["value"] == "30"

    cfg = yaml.safe_load(find(by_file, "ConfigMap", "litellm-config")["data"]["config.yaml"])
    assert cfg["model_list"] == []
    assert cfg["general_settings"] == {
        "master_key": "os.environ/LITELLM_MASTER_KEY",
        "store_model_in_db": True,
        "background_health_checks": False,
        "database_connection_pool_limit": 30,
        "coordination_redis": {"host": "127.0.0.1", "port": 6379},
        # a deleted key stops working on every worker within this many seconds
        "user_api_key_cache_ttl": 15,
        # one LiteLLM_SpendLogs row per request: rows older than 30 days are deleted daily (usage comes from the daily tables)
        "maximum_spend_logs_retention_period": "30d",
        # a refused (429) or failed request writes no LiteLLM_SpendLogs row and is not counted in api_requests:
        # the portal's usage shows served requests only; errors stay in the LiteLLM stdout logs
        "disable_error_logs": True,
    }
    assert cfg["router_settings"] == {
        "routing_strategy": "least-busy", "num_retries": 2, "timeout": 600,
        "allowed_fails": 1, "cooldown_time": 30,
        "redis_host": "127.0.0.1", "redis_port": 6379,
        # a saturated model answers 429 at once: no retries with back-off that keep the request and a worker busy
        "retry_policy": {"RateLimitErrorRetries": 0}}
    assert cfg["litellm_settings"] == {
        "drop_params": True, "callbacks": ["overload_hook.handler", "system_prompt_hook.handler"],
        "enable_redis_auth_cache": True}

    pg = find(by_file, "StatefulSet", "postgres")
    assert pg["spec"]["replicas"] == 1
    pgc = pg["spec"]["template"]["spec"]["containers"][0]
    assert pgc["image"] == "postgres:17-alpine"
    assert pgc["args"] == ["-c", "listen_addresses=127.0.0.1", "-c", "max_connections=300"]
    assert "pg_isready" in pgc["readinessProbe"]["exec"]["command"]
    assert "127.0.0.1" in pgc["readinessProbe"]["exec"]["command"]
    pvc = pg["spec"]["volumeClaimTemplates"][0]["spec"]
    assert pvc["storageClassName"] == "local-path"
    assert pvc["resources"]["requests"]["storage"] == "5Gi"

    dp = find(by_file, "DaemonSet", "nvidia-device-plugin-daemonset")
    assert dp["metadata"]["namespace"] == "kube-system"
    dps = dp["spec"]["template"]["spec"]
    assert dps["nodeSelector"] == {"llm.nsu/accel": "nvidia"}
    assert dps["runtimeClassName"] == "nvidia"
    # nvcr.io answers 403 from Russia: the plugin (v0.19.3, built from source) runs from the GPU node image, which
    # every GPU node already has in its k3s images dir (run-node saves it): taken from there, pulled from main only when the tag is new
    c = dps["containers"][0]
    assert (c["image"], c["imagePullPolicy"], c["command"]) == ("nsu-llm-node:0.2.1-gpu", "IfNotPresent", ["nvidia-device-plugin"])
    # a missing image halts the roll-out at one node, it does not switch the plugin off everywhere
    assert dp["spec"]["updateStrategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}}
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env == {
        "FAIL_ON_INIT_ERROR": "false", "NVIDIA_DISABLE_REQUIRE": "true", "NVIDIA_VISIBLE_DEVICES": "all",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
    }
    # the plugin runs on class GPU nodes, which carry llm.nsu/class=true:NoSchedule
    assert {"key": "llm.nsu/class", "operator": "Equal", "value": "true", "effect": "NoSchedule"} in dps["tolerations"]


def test_device_plugin_follows_the_node_image_or_a_mirror(tmp_path):
    def plugin(**env):
        render_manifests.render({**ENV, **env}, tmp_path)
        dp = find(all_docs(tmp_path), "DaemonSet", "nvidia-device-plugin-daemonset")
        c = dp["spec"]["template"]["spec"]["containers"][0]
        return c["image"], c["imagePullPolicy"]

    assert plugin(NODE_IMAGE_REPO="reg.nsu.ru/nsu-llm-node", NODE_IMAGE_TAG="0.2.1") == ("reg.nsu.ru/nsu-llm-node:0.2.1-gpu", "IfNotPresent")
    # the official image from a mirror registry of nvcr.io (its entrypoint is the same nvidia-device-plugin)
    mirror = "mirror.nsu.ru/nvidia/k8s-device-plugin:v0.19.3"
    assert plugin(DEVICE_PLUGIN_IMAGE=mirror) == (mirror, "IfNotPresent")
    for bad in ("x y", 'a"b', "img:tag\nkind: Secret"):
        with pytest.raises(ValueError, match="DEVICE_PLUGIN_IMAGE"):
            render_manifests.render({**ENV, "DEVICE_PLUGIN_IMAGE": bad}, tmp_path)


def test_secret_contents(out):
    sec = find(all_docs(out), "Secret", "llm-secrets")
    data = sec["stringData"]
    assert data["LITELLM_DATABASE_URL"] == \
        "postgresql://litellm:ll-db-secret@127.0.0.1:5432/litellm"
    for k, v in ENV.items():
        if k != "REGISTRY_HTPASSWD":  # its own Secret llm-registry-auth (common-50-registry)
            assert data[k] == v
    assert "REGISTRY_HTPASSWD" not in data and "REGISTRY_PASSWORD" not in data


def test_dollar_escaping_survives_render(out):
    text = (out / "common-20-postgres.yaml").read_text(encoding="utf-8")
    cm = find(all_docs(out), "ConfigMap", "postgres-init")
    script = next(iter(cm["data"].values()))
    assert "$POSTGRES_USER" in script
    assert "$LITELLM_DB_PASSWORD" in script and "$PORTAL_DB_PASSWORD" in script
    assert "$$" not in script
    for secret in ENV.values():
        assert secret not in text


def test_backend_key_is_rendered_into_the_secret(out):
    assert find(all_docs(out), "Secret", "llm-secrets")["stringData"]["BACKEND_API_KEY"] == "backend-secret"
    env = {k: v for k, v in ENV.items() if k != "BACKEND_API_KEY"}
    with pytest.raises(KeyError, match="BACKEND_API_KEY"):
        render_manifests.render(env, out / "again")


def test_hf_token_is_optional_and_lands_in_the_secret(tmp_path):
    secret = lambda out: find(all_docs(out), "Secret", "llm-secrets")["stringData"]  # noqa: E731
    render_manifests.render(ENV, tmp_path / "none")
    assert secret(tmp_path / "none")["HF_TOKEN"] == ""  # the portal adds no reference to it then
    render_manifests.render(dict(ENV, HF_TOKEN="hf_AbC123"), tmp_path / "set")
    assert secret(tmp_path / "set")["HF_TOKEN"] == "hf_AbC123"
    with pytest.raises(ValueError, match="HF_TOKEN"):
        render_manifests.render(dict(ENV, HF_TOKEN='hf"x'), tmp_path / "bad")


def test_missing_variable_raises_with_name(tmp_path):
    env = dict(ENV)
    del env["LITELLM_SALT_KEY"]
    with pytest.raises(KeyError) as e:
        render_manifests.render(env, tmp_path)
    assert "LITELLM_SALT_KEY" in str(e.value)


def test_rerender_is_idempotent(tmp_path):
    first = render_manifests.render(ENV, tmp_path)
    snap = {p.name: p.read_bytes() for p in first}
    second = render_manifests.render(ENV, tmp_path)
    assert {p.name: p.read_bytes() for p in second} == snap
    assert sorted(p.name for p in tmp_path.iterdir()) == EXPECTED


def test_unsafe_value_rejected_with_name(tmp_path):
    env = dict(ENV, LITELLM_DB_PASSWORD='bad"pw/with:chars')
    with pytest.raises(ValueError) as e:
        render_manifests.render(env, tmp_path)
    assert "LITELLM_DB_PASSWORD" in str(e.value)


def test_cli_renders_from_environ(tmp_path):
    import os
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(MAIN / "render_manifests.py"), "--out", str(tmp_path / "o")],
        env={**os.environ, **ENV}, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == EXPECTED
    env = {k: v for k, v in os.environ.items() if k != "LITELLM_SALT_KEY"}
    proc = subprocess.run(
        [sys.executable, str(MAIN / "render_manifests.py"), "--out", str(tmp_path / "p")],
        env={**env, **{k: v for k, v in ENV.items() if k != "LITELLM_SALT_KEY"}},
        capture_output=True, text=True,
    )
    assert proc.returncode == 2 and "LITELLM_SALT_KEY" in proc.stderr


def test_litellm_waits_for_postgres(out):
    spec = find(all_docs(out), "Deployment", "litellm")["spec"]["template"]["spec"]
    init = spec["initContainers"][0]
    assert init["image"] == "postgres:17-alpine"
    assert "pg_isready -h 127.0.0.1" in " ".join(init["command"])


def test_litellm_waits_for_redis(out):
    spec = find(all_docs(out), "Deployment", "litellm")["spec"]["template"]["spec"]
    names = [c["name"] for c in spec["initContainers"]]
    assert names == ["wait-postgres", "wait-redis"]
    wait = spec["initContainers"][1]
    assert wait["image"] == "redis:7-alpine"
    assert "redis-cli -h 127.0.0.1 -p 6379 ping | grep -qx PONG" in " ".join(wait["command"])
    assert wait["env"] == [{"name": "REDISCLI_AUTH", "valueFrom": {"secretKeyRef": {"name": "llm-secrets", "key": "REDIS_PASSWORD"}}}]


def test_redis_manifest(out):
    by_file = all_docs(out)
    assert [d["kind"] for d in by_file["common-25-redis.yaml"]] == ["Namespace", "Deployment"]
    dep = find(by_file, "Deployment", "redis")
    assert dep["metadata"]["namespace"] == "llm"
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["strategy"] == {"type": "Recreate"}
    spec = dep["spec"]["template"]["spec"]
    (c,) = spec["containers"]
    assert c["image"] == "redis:7-alpine"
    args = c["args"]
    # loopback only, no persistence: counters, cooldowns and cached keys are rebuilt after a restart
    assert args[args.index("--bind") + 1] == "127.0.0.1"
    assert args[args.index("--port") + 1] == "6379"
    assert args[args.index("--save") + 1] == ""
    assert args[args.index("--appendonly") + 1] == "no"
    assert args[args.index("--maxmemory-policy") + 1] == "allkeys-lru"
    ping = ["sh", "-c", "redis-cli -h 127.0.0.1 -p 6379 ping | grep -qx PONG"]
    assert c["readinessProbe"]["exec"]["command"] == ping and c["livenessProbe"]["exec"]["command"] == ping
    assert "volumeMounts" not in c and "volumes" not in spec


def test_redis_requires_the_password_from_the_secret(out):
    """SEC-2 M2: requirepass from llm-secrets, never in argv; probes and clients authenticate."""
    by_file = all_docs(out)
    c = find(by_file, "Deployment", "redis")["spec"]["template"]["spec"]["containers"][0]
    ref = {"secretKeyRef": {"name": "llm-secrets", "key": "REDIS_PASSWORD"}}
    env = {e["name"]: e.get("valueFrom") for e in c["env"]}
    assert env == {"REDIS_PASSWORD": ref, "REDISCLI_AUTH": ref}
    assert c["command"][:2] == ["sh", "-c"] and "requirepass %s" in c["command"][2]
    assert '"$REDIS_PASSWORD"' in c["command"][2] and "exec redis-server /tmp/redis.conf" in c["command"][2]
    assert "redis-secret" not in yaml.safe_dump(c)
    secret = find(by_file, "Secret", "llm-secrets")["stringData"]
    assert secret["REDIS_PASSWORD"] == "redis-secret"
    assert "REDIS_PASSWORD" not in find(by_file, "Secret", "llm-node-secrets")["stringData"]
    litellm = find(by_file, "Deployment", "litellm")["spec"]["template"]["spec"]["containers"][0]
    lenv = {e["name"]: e for e in litellm["env"]}
    assert lenv["REDIS_PASSWORD"]["valueFrom"] == ref
    # Kubernetes expands $(REDIS_PASSWORD): the variable is defined earlier in the same list
    names = [e["name"] for e in litellm["env"]]
    assert names.index("REDIS_PASSWORD") < names.index("GATEWAY_STATS_REDIS_URL")
    assert lenv["GATEWAY_STATS_REDIS_URL"]["value"] == "redis://:$(REDIS_PASSWORD)@127.0.0.1:6379/0"


def test_litellm_workers_and_db_pool_follow_the_setting(tmp_path):
    render_manifests.render(dict(ENV, LITELLM_WORKERS="2"), tmp_path)
    by_file = all_docs(tmp_path)
    args = find(by_file, "Deployment", "litellm")["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[args.index("--num_workers") + 1] == "2"
    cfg = yaml.safe_load(find(by_file, "ConfigMap", "litellm-config")["data"]["config.yaml"])
    assert cfg["general_settings"]["database_connection_pool_limit"] == 60


@pytest.mark.parametrize("workers", range(1, render_manifests.MAX_LITELLM_WORKERS + 1))
def test_litellm_db_connections_fit_postgres(workers):
    limit = render_manifests.litellm_db_pool_limit(workers)
    assert limit >= 1
    # LiteLLM (one Prisma engine per worker) + portal + superuser reserve stay under max_connections
    assert workers * limit + render_manifests.PORTAL_DB_CONNECTIONS + 3 < render_manifests.POSTGRES_MAX_CONNECTIONS


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "", "17", "2.5"])
def test_bad_litellm_workers_rejected(tmp_path, bad):
    with pytest.raises(ValueError) as e:
        render_manifests.render(dict(ENV, LITELLM_WORKERS=bad), tmp_path)
    assert "LITELLM_WORKERS" in str(e.value)


def test_overload_hook_is_shipped_next_to_the_config(out):
    data = find(all_docs(out), "ConfigMap", "litellm-config")["data"]
    assert set(data) == {"config.yaml", "overload_hook.py", "system_prompt_hook.py"}
    for name in ("overload_hook.py", "system_prompt_hook.py"):
        assert data[name] == (MAIN / "litellm" / name).read_text(encoding="utf-8")


def test_overload_hook_gets_the_redis_address_the_portal_reads_the_counters_from(out):
    """The hook defaults to 127.0.0.1:6379 only by accident: the manifest sets the address from the same place as the portal's."""
    by_file = all_docs(out)
    container = find(by_file, "Deployment", "litellm")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    entrypoint = (MAIN / "entrypoint.sh").read_text(encoding="utf-8")
    portal_url = re.search(r'^REDIS_URL="(\S+)"$', entrypoint, re.M).group(1)
    assert env["GATEWAY_STATS_REDIS_URL"] == portal_url.replace("${REDIS_PASSWORD}", "$(REDIS_PASSWORD)")
    config = yaml.safe_load(find(by_file, "ConfigMap", "litellm-config")["data"]["config.yaml"])
    router = config["router_settings"]
    assert portal_url == f"redis://:${{REDIS_PASSWORD}}@{router['redis_host']}:{router['redis_port']}/0"


def _litellm_pod_annotations(env, out_dir):
    render_manifests.render(env, out_dir)
    return find(all_docs(out_dir), "Deployment", "litellm")["spec"]["template"]["metadata"].get("annotations", {})


def test_litellm_pods_roll_when_their_config_changes(tmp_path):
    # LiteLLM reads config.yaml only at start: the pod template carries a hash of the rendered file
    first = _litellm_pod_annotations(ENV, tmp_path / "a")
    again = _litellm_pod_annotations(ENV, tmp_path / "b")
    other = _litellm_pod_annotations(dict(ENV, LITELLM_WORKERS="2"), tmp_path / "c")
    assert re.fullmatch(r"[0-9a-f]{16}", first["llm.nsu/config-hash"])
    assert first == again
    assert other["llm.nsu/config-hash"] != first["llm.nsu/config-hash"]


# ---------------------------------------------------------------- Hugging Face mirror


def _mirror(out_dir):
    docs = all_docs(out_dir)
    return find(docs, "Deployment", "hf-mirror"), find(docs, "ConfigMap", "hf-mirror")


def test_hf_mirror_runs_on_main_from_its_own_volume(out):
    dep, cm = _mirror(out)
    assert dep["metadata"]["namespace"] == "llm" and dep["spec"]["strategy"] == {"type": "Recreate"}
    spec = dep["spec"]["template"]["spec"]
    assert_main_only(spec)
    assert spec["hostNetwork"] is True
    (c,) = spec["containers"]
    # the LiteLLM image is already on main (also offline, run-main --airgap): its python3 runs the stdlib script
    assert c["image"] == "ghcr.io/berriai/litellm:v1.106.6"
    assert c["command"] == ["python3", "/opt/hf-mirror/hf_mirror.py"]
    env = {e["name"]: e for e in c["env"]}
    assert {k: env[k]["value"] for k in ("HF_MIRROR_DIR", "HF_MIRROR_HOST", "HF_MIRROR_PORT", "HF_MIRROR_MAX_GIB",
                                         "HF_MIRROR_UPSTREAM", "HF_MIRROR_PREFIX")} == {
        "HF_MIRROR_DIR": "/data", "HF_MIRROR_HOST": "127.0.0.1", "HF_MIRROR_PORT": "8090", "HF_MIRROR_MAX_GIB": "500",
        "HF_MIRROR_UPSTREAM": "https://huggingface.co", "HF_MIRROR_PREFIX": "/hf",
    }
    assert env["HF_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "llm-secrets", "key": "HF_TOKEN", "optional": True}
    # the limit set in the portal panel: a ConfigMap of the portal, optional (the mirror starts without it)
    assert env["HF_MIRROR_MAX_GIB_OVERRIDE"]["valueFrom"]["configMapKeyRef"] == {
        "name": "hf-mirror-settings", "key": "max_gib", "optional": True,
    }
    assert env["HF_MIRROR_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "llm-secrets", "key": "HF_MIRROR_TOKEN"}
    for probe in ("readinessProbe", "livenessProbe"):
        assert c[probe]["httpGet"] == {"host": "127.0.0.1", "port": 8090, "path": "/_mirror/health"}, probe
    mounts = {m["name"]: m["mountPath"] for m in c["volumeMounts"]}
    assert mounts == {"data": "/data", "code": "/opt/hf-mirror"}
    volumes = {v["name"]: v for v in spec["volumes"]}
    # /var/lib/llm-hf is the llm-main-hf volume of the main container (run-main): the airgap copy goes there
    assert volumes["data"]["hostPath"] == {"path": "/var/lib/llm-hf", "type": "DirectoryOrCreate"}
    assert volumes["code"]["configMap"]["name"] == "hf-mirror"
    assert cm["data"]["hf_mirror.py"] == (MAIN / "hf_mirror.py").read_text(encoding="utf-8")
    assert re.fullmatch(r"[0-9a-f]{16}", dep["spec"]["template"]["metadata"]["annotations"]["llm.nsu/config-hash"])


def test_hf_mirror_settings_and_proxy(tmp_path):
    env = dict(ENV, HF_MIRROR_MAX_GIB="2000", HF_MIRROR_PORT="8191", HF_ENDPOINT="https://hf.example.org/",
               HTTPS_PROXY="http://proxy.nsu.ru:3128", NO_PROXY="localhost,127.0.0.1")
    render_manifests.render(env, tmp_path)
    dep, _ = _mirror(tmp_path)
    c = dep["spec"]["template"]["spec"]["containers"][0]
    values = {e["name"]: e.get("value") for e in c["env"]}
    assert values["HF_MIRROR_MAX_GIB"] == "2000" and values["HF_MIRROR_PORT"] == "8191"
    assert values["HF_MIRROR_UPSTREAM"] == "https://hf.example.org"
    assert values["HTTPS_PROXY"] == "http://proxy.nsu.ru:3128" and values["NO_PROXY"] == "localhost,127.0.0.1"
    assert "HTTP_PROXY" not in values
    assert c["readinessProbe"]["httpGet"]["port"] == 8191


@pytest.mark.parametrize("name,bad", [
    ("HF_MIRROR_MAX_GIB", "0"), ("HF_MIRROR_MAX_GIB", "lots"), ("HF_MIRROR_PORT", "80"), ("HF_MIRROR_PORT", "x"),
    ("HF_ENDPOINT", "ftp://x"), ("HTTPS_PROXY", 'http://a"b'),
])
def test_bad_hf_mirror_settings_are_rejected(tmp_path, name, bad):
    with pytest.raises(ValueError, match=name):
        render_manifests.render(dict(ENV, **{name: bad}), tmp_path)


def test_hf_mirror_token_is_required_and_lands_in_the_secret(out):
    assert find(all_docs(out), "Secret", "llm-secrets")["stringData"]["HF_MIRROR_TOKEN"] == ENV["HF_MIRROR_TOKEN"]
    with pytest.raises(KeyError, match="HF_MIRROR_TOKEN"):
        render_manifests.render({k: v for k, v in ENV.items() if k != "HF_MIRROR_TOKEN"}, out / "again")


def test_ca_configmap_carries_the_main_ca_and_a_bundle(tmp_path):
    ca = tmp_path / "root.crt"
    ca.write_text(PEM_MAIN, encoding="utf-8")
    system = tmp_path / "system.crt"
    system.write_text(PEM_SYSTEM.rstrip(), encoding="utf-8")  # no final newline: the bundle still splits the two
    path = render_manifests.render_ca(ca, tmp_path / "out", system_bundle=system)
    assert path.name == "common-15-ca.yaml"
    docs = docs_of(path)
    assert docs[0]["kind"] == "Namespace"
    cm = find({"f": docs}, "ConfigMap", "llm-ca")
    assert cm["metadata"]["namespace"] == "llm"
    assert cm["data"]["ca.crt"] == PEM_MAIN
    assert cm["data"]["bundle.crt"] == PEM_SYSTEM + PEM_MAIN
    # no system bundle: the bundle is the main CA alone
    path = render_manifests.render_ca(ca, tmp_path / "out2", system_bundle=tmp_path / "missing")
    assert find({"f": docs_of(path)}, "ConfigMap", "llm-ca")["data"]["bundle.crt"] == PEM_MAIN


def test_cli_renders_the_ca_configmap(tmp_path):
    import subprocess

    ca = tmp_path / "root.crt"
    ca.write_text(PEM_MAIN, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(MAIN / "render_manifests.py"), "--ca", str(ca), "--out", str(tmp_path / "o")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == ["common-15-ca.yaml"]
    proc = subprocess.run(
        [sys.executable, str(MAIN / "render_manifests.py"), "--ca", str(tmp_path / "none"), "--out", str(tmp_path / "p")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2 and "none" in proc.stderr


PEM_MAIN = "-----BEGIN CERTIFICATE-----\nMAIN\n-----END CERTIFICATE-----\n"
PEM_SYSTEM = "-----BEGIN CERTIFICATE-----\nSYSTEM\n-----END CERTIFICATE-----\n"


def test_hf_mirror_restarts_when_the_hugging_face_token_changes(tmp_path):
    def pod_meta(env, d):
        render_manifests.render(env, tmp_path / d)
        return _mirror(tmp_path / d)[0]["spec"]["template"]["metadata"]["annotations"]
    none, one, two = pod_meta(ENV, "a"), pod_meta(dict(ENV, HF_TOKEN="hf_one"), "b"), pod_meta(dict(ENV, HF_TOKEN="hf_two"), "c")
    assert none["llm.nsu/hf-token"] == "" and re.fullmatch(r"[0-9a-f]{12}", one["llm.nsu/hf-token"])
    assert len({none["llm.nsu/config-hash"], one["llm.nsu/config-hash"], two["llm.nsu/config-hash"]}) == 3
    assert "hf_one" not in (tmp_path / "b" / "common-40-hf-mirror.yaml").read_text(encoding="utf-8")


# ---------------------------------------------------------------- daily Postgres backup (CronJob)


def _backup(env, out_dir):
    render_manifests.render(env, out_dir)
    by_file = all_docs(out_dir)
    return find(by_file, "CronJob", "postgres-backup"), find(by_file, "ConfigMap", "postgres-backup")


def test_backup_cronjob_dumps_every_database_daily_from_the_postgres_image(tmp_path):
    cron, _ = _backup(ENV, tmp_path)
    spec = cron["spec"]
    assert cron["metadata"]["namespace"] == "llm"
    assert cron["metadata"]["labels"]["app.kubernetes.io/part-of"] == "nsu-llm"
    # the controller-manager runs in the UTC container: 21:17 UTC is 04:17 in Novosibirsk
    assert spec["schedule"] == "17 21 * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    # k3s down at the scheduled minute: the run still happens when it is back within two hours
    assert spec["startingDeadlineSeconds"] == 7200
    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["hostNetwork"] is True
    assert_main_only(pod)
    c = pod["containers"][0]
    # same image as the server: pg_dump of another major version refuses to dump it
    assert c["image"] == "postgres:17-alpine"
    assert c["command"] == ["sh", "/scripts/pg-backup.sh"]
    env = {e["name"]: e for e in c["env"]}
    assert env["PGHOST"]["value"] == "127.0.0.1" and env["PGUSER"]["value"] == "postgres"
    assert env["PGPASSWORD"]["valueFrom"]["secretKeyRef"] == {"name": "llm-secrets", "key": "POSTGRES_PASSWORD"}
    assert env["BACKUP_DIR"]["value"] == "/backups" and env["BACKUP_KEEP"]["value"] == "14"
    mounts = {m["name"]: m for m in c["volumeMounts"]}
    assert mounts["scripts"]["mountPath"] == "/scripts"
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["scripts"]["configMap"]["name"] == "postgres-backup"
    # the files land in the llm-main-data volume (/var/lib/llm), where the portal reads them for the status page
    assert volumes["backups"]["hostPath"] == {"path": "/var/lib/llm/backups", "type": "DirectoryOrCreate"}
    assert mounts["backups"]["mountPath"] == "/backups"


def test_backup_script_is_shipped_in_a_configmap(tmp_path):
    _, cm = _backup(ENV, tmp_path)
    source = (MAIN / "backup" / "pg-backup.sh").read_text(encoding="utf-8")
    assert cm["data"] == {"pg-backup.sh": source}


def test_backup_schedule_and_keep_follow_the_settings(tmp_path):
    cron, _ = _backup(dict(ENV, BACKUP_SCHEDULE="*/30 * * * *", BACKUP_KEEP="7"), tmp_path)
    assert cron["spec"]["schedule"] == "*/30 * * * *"
    env = {e["name"]: e for e in cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["BACKUP_KEEP"]["value"] == "7"


@pytest.mark.parametrize(
    "name, value",
    [
        ("BACKUP_KEEP", "0"),
        ("BACKUP_KEEP", "x"),
        ("BACKUP_KEEP", "1000"),
        ("BACKUP_SCHEDULE", "daily"),
        ("BACKUP_SCHEDULE", "17 21 * *"),
        ("BACKUP_SCHEDULE", '17 21 * * *"; rm'),
    ],
)
def test_bad_backup_settings_rejected(tmp_path, name, value):
    with pytest.raises(ValueError) as e:
        render_manifests.render(dict(ENV, **{name: value}), tmp_path)
    assert name in str(e.value)


NODE_SECRET_KEYS = {"HF_TOKEN"}


def test_node_pods_get_a_separate_secret_without_mains_keys(tmp_path):
    """A node's kubelet may read every Secret its pods reference: the inference/rpc pods (portal builders) reference
    llm-node-secrets, which holds only the real HF token with the mirror off. Not the shared backend key (each node has its
    own, SEC-1) and not the mirror key: it opens every repository HF_TOKEN sees and signs the nodes' tokens (SEC-2 L2)."""
    node = lambda out: find(all_docs(out), "Secret", "llm-node-secrets")  # noqa: E731
    render_manifests.render(dict(ENV, HF_TOKEN="hf_real"), tmp_path / "mirror")
    sec = node(tmp_path / "mirror")
    assert sec["metadata"]["namespace"] == "llm" and sec["metadata"]["labels"] == {"app.kubernetes.io/part-of": "nsu-llm"}
    assert set(sec["stringData"]) == NODE_SECRET_KEYS
    assert sec["stringData"]["HF_TOKEN"] == ""  # the nodes download through the mirror: the real token stays on main
    for value in ("hf_real", ENV["LITELLM_MASTER_KEY"], ENV["LITELLM_SALT_KEY"], ENV["POSTGRES_PASSWORD"],
                  ENV["LITELLM_DB_PASSWORD"], ENV["PORTAL_DB_PASSWORD"], ENV["HF_MIRROR_TOKEN"], ENV["BACKEND_API_KEY"],
                  ENV["REDIS_PASSWORD"], ENV["HF_MIRROR_ADMIN_TOKEN"]):
        assert value not in str(sec)
    for off in ("false", "False", "0", "no", "off", "f", "n"):
        render_manifests.render(dict(ENV, HF_TOKEN="hf_real", HF_MIRROR_ENABLED=off), tmp_path / off)
        assert node(tmp_path / off)["stringData"]["HF_TOKEN"] == "hf_real", off  # direct downloads need it
    for on in ("true", "1", "yes", ""):
        render_manifests.render(dict(ENV, HF_TOKEN="hf_real", HF_MIRROR_ENABLED=on), tmp_path / f"on{on}")
        assert node(tmp_path / f"on{on}")["stringData"]["HF_TOKEN"] == "", on


def _registry(out):
    by_file = all_docs(out)
    return find(by_file, "Deployment", "registry")


def test_registry_runs_on_main_localhost_only_from_its_own_volume(out):
    dep = _registry(out)
    assert dep["metadata"]["namespace"] == "llm" and dep["spec"]["strategy"] == {"type": "Recreate"}
    assert dep["spec"]["replicas"] == 1
    spec = dep["spec"]["template"]["spec"]
    assert_main_only(spec)
    assert spec["hostNetwork"] is True
    (c,) = spec["containers"]
    # pinned by version and by the digest of the index (docker buildx imagetools inspect registry:2.8.3)
    assert re.fullmatch(r"registry:2\.8\.3@sha256:[0-9a-f]{64}", c["image"])
    assert c["imagePullPolicy"] == "IfNotPresent"
    env = {e["name"]: e["value"] for e in c["env"]}
    # the registry itself listens on the backend port of main's loopback; Caddy serves reads on REGISTRY_PORT and /v2/
    assert env["REGISTRY_HTTP_ADDR"] == "127.0.0.1:5001"
    assert env["REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY"] == "/var/lib/registry"
    assert env["REGISTRY_STORAGE_DELETE_ENABLED"] == "true"
    # SEC-2 M2: every request needs the password (htpasswd from a Secret); no password itself in the manifest
    assert env["REGISTRY_AUTH"] == "htpasswd" and env["REGISTRY_AUTH_HTPASSWD_PATH"] == "/auth/htpasswd"
    assert env["REGISTRY_AUTH_HTPASSWD_REALM"]
    for probe in ("readinessProbe", "livenessProbe"):
        assert c[probe]["tcpSocket"] == {"host": "127.0.0.1", "port": 5001}, probe
    assert {m["name"]: m["mountPath"] for m in c["volumeMounts"]} == {"data": "/var/lib/registry", "auth": "/auth"}
    data, auth = spec["volumes"]
    # /var/lib/llm-registry is the llm-main-registry volume of the main container (run-main.sh)
    assert data["hostPath"] == {"path": "/var/lib/llm-registry", "type": "DirectoryOrCreate"}
    assert auth["secret"] == {"secretName": "llm-registry-auth", "defaultMode": 0o400}
    secret = find(all_docs(out), "Secret", "llm-registry-auth")
    assert secret["metadata"]["namespace"] == "llm" and secret["stringData"] == {"htpasswd": ENV["REGISTRY_HTPASSWD"]}
    assert re.fullmatch(r"[0-9a-f]{16}", dep["spec"]["template"]["metadata"]["annotations"]["llm.nsu/config-hash"])


def test_registry_settings_follow_the_environment(tmp_path):
    render_manifests.render(dict(ENV, REGISTRY_BACKEND_PORT="5111", REGISTRY_IMAGE="registry:2.8.3"), tmp_path)
    (c,) = _registry(tmp_path)["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e["value"] for e in c["env"]}
    assert c["image"] == "registry:2.8.3" and env["REGISTRY_HTTP_ADDR"] == "127.0.0.1:5111"
    assert c["readinessProbe"]["tcpSocket"]["port"] == 5111 and c["ports"][0]["containerPort"] == 5111


def test_secrets_read_at_start_restart_their_pods_when_they_change(tmp_path):
    def annotations(d, **env):
        render_manifests.render(dict(ENV, **env), tmp_path / d)
        docs = all_docs(tmp_path / d)
        return {name: find(docs, "Deployment", name)["spec"]["template"]["metadata"]["annotations"]
                for name in ("redis", "litellm", "hf-mirror")}

    a, b, c = annotations("a"), annotations("b", REDIS_PASSWORD="other"), annotations("c", HF_MIRROR_TOKEN="hf_" + "cd" * 20)
    assert a["redis"]["llm.nsu/redis-password"] != b["redis"]["llm.nsu/redis-password"]
    assert a["litellm"]["llm.nsu/redis-password"] != b["litellm"]["llm.nsu/redis-password"]
    assert a["hf-mirror"]["llm.nsu/mirror-token"] != c["hf-mirror"]["llm.nsu/mirror-token"]
    for ann in a.values():
        assert ENV["REDIS_PASSWORD"] not in str(ann) and ENV["HF_MIRROR_TOKEN"] not in str(ann)


def test_registry_push_keeps_the_password_out_of_argv():
    """review I2: ctr --user would put the password into /proc/<pid>/cmdline (visible from the host of main); the helper
    hands it over as a header in a hosts.toml of a 0700 temp dir instead."""
    text = (MAIN / "registry-push.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "--user" not in code and "-u " not in code
    assert "--hosts-dir" in code and 'Authorization = "Basic' in code and "umask 077" in code
    # the pause image of k3s is an index with the content of linux/amd64 only: --amd64-manifest pushes that manifest over the registry API
    assert "--amd64-manifest" in code and "push_amd64_manifest" in code and 'user = "%s:%s"' in code and "-K " in code


def test_a_new_registry_password_restarts_the_registry(tmp_path):
    def annotation(htpasswd, d):
        render_manifests.render(dict(ENV, REGISTRY_HTPASSWD=htpasswd), tmp_path / d)
        return _registry(tmp_path / d)["spec"]["template"]["metadata"]["annotations"]["llm.nsu/config-hash"]

    assert annotation(ENV["REGISTRY_HTPASSWD"], "a") != annotation("nsu-push:$2a$06$" + "b" * 53, "b")


@pytest.mark.parametrize("name,bad", [
    ("REGISTRY_PORT", "80"), ("REGISTRY_PORT", "x"), ("REGISTRY_IMAGE", "Registry:2"), ("REGISTRY_IMAGE", 'r:2"x'),
    ("REGISTRY_BACKEND_PORT", "80"), ("REGISTRY_BACKEND_PORT", "5000"), ("REGISTRY_HTPASSWD", ""),
    ("REGISTRY_HTPASSWD", "admin:$2a$06$" + "a" * 53), ("REGISTRY_HTPASSWD", 'nsu-push:"x'),
])
def test_bad_registry_settings_are_rejected(tmp_path, name, bad):
    with pytest.raises(ValueError, match=name):
        render_manifests.render(dict(ENV, **{name: bad}), tmp_path)


def test_a_tag_bump_keeps_the_device_plugin_startable(tmp_path):
    """Found on the 122b bed: with imagePullPolicy Never the plugin needed the new tag in the k3s images dir of every GPU
    node, which only a restart of the node container with a newer image provides; after NODE_IMAGE_TAG 0.1.0 -> 0.1.1 every
    GPU pod got ErrImageNeverPull and no node advertised nvidia.com/gpu. The plugin pod must be able to pull its image."""

    def plugin(tag):
        render_manifests.render({**ENV, "NODE_IMAGE_TAG": tag}, tmp_path)
        dp = find(all_docs(tmp_path), "DaemonSet", "nvidia-device-plugin-daemonset")
        return dp["spec"]["template"]["spec"]["containers"][0]

    before, after = plugin("0.1.0"), plugin("0.1.1")
    assert (before["image"], after["image"]) == ("nsu-llm-node:0.1.0-gpu", "nsu-llm-node:0.1.1-gpu")
    for c in (before, after):
        assert c["imagePullPolicy"] != "Never"  # the image of a new tag is not on the node yet
        assert c["imagePullPolicy"] == "IfNotPresent"  # and the one the node has is not pulled again
