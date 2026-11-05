"""Task 2-22b: the llama.cpp settings reach the pods (environment of serve-llama.sh), the memory estimate and the
command preview. The flags themselves are made by serve-llama.sh (node/tests/test_scripts.sh); here the portal's side
and the parity of the preview with the script."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.catalog import estimate as est
from app.catalog import placement as pl
from app.catalog.gguf import parse_gguf_header
from app.config import Settings
from app.deployments.llama_args import FORBIDDEN_FLAGS
from app.workloads import builders
from app.workloads.builders import (
    RpcWorker,
    command_preview,
    llama_settings_env,
    replicas_daemonset,
    rpc_daemonset,
    serve_llama_argv,
    split_leader,
    template_hash,
)
from tests.helpers.gguf_builder import build_gguf
from tests.test_catalog import E4B_FILE_BYTES, E4B_KVS, E4B_LAYERS_BYTES, E4B_OUTPUT_BYTES

GiB = 2**30
MiB = 2**20
SHA = "a" * 40
SHA2 = "b" * 40
NODE_BIN = Path(__file__).resolve().parents[2] / "node" / "rootfs" / "opt" / "llm"

S = Settings(node_image_tag="0.2.0", hf_mirror_enabled=False)
M = Settings(node_image_tag="0.2.0", public_hostname="llm.example")


def dep(**kw):
    base = dict(
        name="gemma-4-e4b", engine="llamacpp", repo="org/model-GGUF", file="model-Q4_K_M.gguf", mode="replicas",
        group_size=2, accel="any", ctx_per_request=8192, parallel=2, meta={"arch": "gemma4", "weights_bytes": 5 * 10**9},
        use_node_ram=True, ram_budget_pct=70, revision=SHA,
        reasoning="off", kv_cache_type="f16", sampling=None, draft_repo=None, draft_file=None, draft_revision=None,
        mmproj_file=None, extra_args=None, system_prompt=None, experimental=False,
    )
    return SimpleNamespace(**{**base, **kw})


def envs(manifest):
    return {e["name"]: e["value"] for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"] if "value" in e}


# ---------- the environment of the pods ----------


def test_defaults_add_nothing_so_existing_deployments_keep_their_pods():
    new = {"reasoning", "kv_cache_type", "sampling", "draft_repo", "draft_file", "draft_revision", "mmproj_file",
           "extra_args", "system_prompt", "experimental"}
    plain = SimpleNamespace(**{k: v for k, v in dep().__dict__.items() if k not in new})  # a row from before: no such attributes
    assert llama_settings_env(plain) == []
    assert llama_settings_env(dep()) == []  # reasoning off, f16 and nothing else: the same environment
    before = template_hash(replicas_daemonset(plain, "cpu", S))
    assert template_hash(replicas_daemonset(dep(), "cpu", S)) == before


def test_every_setting_becomes_a_variable():
    d = dep(
        reasoning="auto", kv_cache_type="q8_0",
        sampling={"temperature": 0.7, "top_p": 0.95, "top_k": 40, "min_p": 0.05, "repeat_penalty": 1.1},
        draft_repo="org/draft", draft_file="d-Q8_0.gguf", draft_revision=SHA2, mmproj_file="mmproj-F16.gguf",
        extra_args="--no-warmup --seed 42",
    )
    assert dict(llama_settings_env(d)) == {
        "THINKING": "auto",
        "KV_CACHE_TYPE": "q8_0",
        "SAMPLING_ARGS": "--temp 0.7 --top-p 0.95 --top-k 40 --min-p 0.05 --repeat-penalty 1.1",
        "DRAFT_REPO": "org/draft", "DRAFT_FILE": "d-Q8_0.gguf", "DRAFT_REVISION": SHA2,
        "MMPROJ_FILE": "mmproj-F16.gguf",
        "EXTRA_ARGS": "--no-warmup\n--seed\n42",
    }
    assert envs(replicas_daemonset(d, "nvidia", S))["KV_CACHE_TYPE"] == "q8_0"


def test_sampling_values_are_plain_decimals():
    got = dict(llama_settings_env(dep(sampling={"temperature": 1.0, "top_k": 0, "min_p": 0.00001, "top_p": 0.123456789})))
    assert got["SAMPLING_ARGS"] == "--temp 1 --top-p 0.123457 --top-k 0 --min-p 0.00001"


def test_an_own_flag_with_a_space_stays_one_token():
    d = dep(extra_args='--override-kv "tokenizer.ggml.add_bos_token=bool:false" --chat-template-kwargs \'{"a": 1}\'')
    tokens = dict(llama_settings_env(d))["EXTRA_ARGS"].split("\n")
    assert tokens == ["--override-kv", "tokenizer.ggml.add_bos_token=bool:false", "--chat-template-kwargs", '{"a": 1}']


def test_own_flags_that_the_list_now_forbids_are_dropped_not_fatal():
    assert llama_settings_env(dep(extra_args="--port 1")) == []  # stored before a stricter list: no crash in reconcile


def test_a_changed_setting_changes_the_template_but_the_gateway_fields_do_not():
    base = template_hash(replicas_daemonset(dep(), "cpu", S))
    assert template_hash(replicas_daemonset(dep(kv_cache_type="q4_0"), "cpu", S)) != base
    assert template_hash(replicas_daemonset(dep(system_prompt="hi", experimental=True), "cpu", S)) == base


def test_the_draft_and_the_projector_are_on_the_leader_only():
    d = dep(mode="split", draft_repo="org/draft", draft_file="d.gguf", draft_revision=SHA2, mmproj_file="mmproj.gguf")
    group = [RpcWorker("a", "10.0.0.1", 18050, "cpu"), RpcWorker("b", "10.0.0.2", 18050, "cpu")]
    leader = envs(split_leader(d, 0, group, S))
    assert leader["DRAFT_REPO"] == "org/draft" and leader["MMPROJ_FILE"] == "mmproj.gguf" and leader["NGL"] == "999"
    rpc = envs(rpc_daemonset(d, "cpu", S))
    assert not {"DRAFT_REPO", "DRAFT_FILE", "MMPROJ_FILE", "KV_CACHE_TYPE", "EXTRA_ARGS"} & set(rpc)


def test_the_forbidden_list_of_the_image_is_the_portals():
    """serve-llama.sh refuses own flags by /opt/llm/forbidden-flags: regenerate it from FORBIDDEN_FLAGS when it differs."""
    text = (NODE_BIN / "forbidden-flags").read_text(encoding="utf-8")
    assert "\r" not in text
    assert text.split("\n")[:-1] == sorted(FORBIDDEN_FLAGS)


def test_the_forms_forbidden_list_is_the_portals():
    """The form checks own flags while typing with a copy of FORBIDDEN_FLAGS (web/src/lib/llm.ts): keep them equal."""
    import re

    web = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "llm.ts"
    if not web.is_file():
        pytest.skip("web sources are not present")
    text = web.read_text(encoding="utf-8")
    block = text[text.index("export const FORBIDDEN_FLAGS"):]
    block = block[: block.index("];")]
    assert sorted(re.findall(r'"(-[^"]+)"', block)) == sorted(FORBIDDEN_FLAGS)


# ---------- the preview of the command ----------


def test_preview_of_a_replica_shows_the_flags_and_masks_the_keys():
    d = dep(
        kv_cache_type="q8_0", reasoning="on", sampling={"temperature": 0.6}, mmproj_file="mmproj-F16.gguf",
        draft_repo="org/draft", draft_file="d.gguf", draft_revision=SHA2, extra_args="--no-warmup", accel="nvidia",
    )
    p = command_preview(d, M)
    assert p["rpc"] is None
    leader = p["leader"]
    assert leader.startswith("LLAMA_API_KEY=*** HF_ENDPOINT=") and " HF_TOKEN=*** llama-server " in leader
    for fragment in (
        "--cache-type-k q8_0 --cache-type-v q8_0", "--reasoning on", "--temp 0.6", "-ngl 99", "-ngld 99",
        "--mmproj /var/lib/llm-models/org__model-GGUF/mmproj-F16.gguf", "-md /var/lib/llm-models/org__draft/d.gguf",
        "--spec-type draft-simple --spec-draft-n-max 4 --spec-draft-n-min 1", "--no-warmup",
    ):
        assert fragment in leader, fragment
    assert leader.endswith("--no-warmup")  # own flags come last
    assert "--no-mmproj" not in leader
    assert any("скачивает" in n and "org/draft/d.gguf" in n for n in p["notes"])
    d2 = dep(accel="cpu")
    assert "--no-mmproj" in command_preview(d2, M)["leader"] and "-ngl 0" in command_preview(d2, M)["leader"]


def test_preview_masks_every_secret():
    tokened = Settings(node_image_tag="0.2.0", hf_mirror_enabled=False, hf_token="hf_secret_value")
    p = command_preview(dep(), tokened)
    assert "hf_secret_value" not in p["leader"] and "HF_TOKEN=***" in p["leader"] and "LLAMA_API_KEY=***" in p["leader"]
    assert "--api-key" not in p["leader"]


def test_preview_of_a_split_group_has_the_leader_and_the_rpc_server():
    d = dep(
        mode="split", group_size=3, accel="nvidia", kv_cache_type="q8_0", draft_repo="org/draft", draft_file="d.gguf",
        draft_revision=SHA2,
    )
    p = command_preview(d, S)
    assert "--rpc 127.0.0.1:18050,<ip-2>:18050,<ip-3>:18050 --fit off" in p["leader"]
    assert "-ngl 999" in p["leader"] and "-ngld 0" in p["leader"] and "--tensor-split <layers>" in p["leader"]
    assert p["rpc"] == "LLAMA_CACHE=/var/lib/llm-models ggml-rpc-server -H 0.0.0.0 -p 18050 -t $THREADS -d CUDA0,CPU -c"
    assert any("только лидером" in n for n in p["notes"])


def test_preview_of_vllm_uses_the_manifest_arguments():
    p = command_preview(dep(engine="vllm", accel="nvidia", repo="org/model", file=None, meta={"arch": "llama"}), S)
    assert p["leader"].startswith("VLLM_API_KEY=*** vllm serve org/model --served-model-name gemma-4-e4b")


# ---------- the preview equals what serve-llama.sh builds ----------

BASH = shutil.which("bash")
GROUP = [RpcWorker("a", "10.0.0.1", 18050, "cpu"), RpcWorker("b", "10.0.0.2", 18050, "cpu")]
SETTINGS = dict(
    reasoning="auto", kv_cache_type="q4_0", sampling={"temperature": 0.2, "top_k": 20}, mmproj_file="mmproj-F16.gguf",
    extra_args="--no-warmup --seed 7",
)
DRAFT = dict(draft_repo="org/draft", draft_file="d.gguf", draft_revision=SHA2)
# name -> (deployment fields, split?)
CASES = {
    "plain": (dict(), False),
    "unpinned": (dict(revision=None), False),
    "local": (dict(repo="local/own", file="m.gguf", revision=None), False),
    "settings": (SETTINGS, False),
    "draft-cpu": (DRAFT, False),
    "draft-gpu": (dict(**DRAFT, accel="nvidia"), False),
    "own-mmproj": (dict(mmproj_file="local/vision/proj.gguf"), False),
    "shards": (dict(file="big-00001-of-00003.gguf", mmproj_file="mmproj.gguf"), False),
    "split-plain": (dict(mode="split"), True),
    "split-everything": (dict(mode="split", accel="nvidia", **SETTINGS, **DRAFT), True),
}


def pod_env(d, split: bool) -> dict[str, str]:
    """The environment serve-llama.sh gets in the pods the builders make (the variables with a plain value)."""
    manifest = split_leader(d, 0, GROUP, S, tensor_split=[10, 17, 4]) if split else replicas_daemonset(d, "cpu", S)
    return envs(manifest)


@pytest.mark.skipif(BASH is None, reason="bash is needed to run serve-llama.sh")
@pytest.mark.parametrize("case", sorted(CASES))
def test_preview_flags_equal_serve_llama_print_cmd(case):
    fields, split = CASES[case]
    env = pod_env(dep(**fields), split)
    run_env = {
        "PATH": "/usr/bin:/bin", "LLAMA_CACHE": builders.MODELS_DIR, "THREADS": "7", "HF_ENDPOINT": "https://hf.example",
        "FORBIDDEN_FLAGS_FILE": str(NODE_BIN / "forbidden-flags"), **env,
    }
    done = subprocess.run(
        [BASH, str(NODE_BIN / "bin" / "serve-llama.sh"), "--print-cmd"], env=run_env, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    printed = next(line for line in done.stdout.splitlines() if line.startswith("llama-server "))
    assert printed == " ".join(serve_llama_argv(env, threads="7"))


def test_a_projector_stays_on_the_split_leader():
    """b12033 offloads the projector to a GPU-type device by default; on a leader those are the rpc servers."""
    mm = dict(mmproj_file="mmproj-F16.gguf")
    split = serve_llama_argv(pod_env(dep(mode="split", **mm), True))
    assert "--mmproj" in split and "--no-mmproj-offload" in split
    assert "--no-mmproj-offload" not in serve_llama_argv(pod_env(dep(**mm), False))  # a replica may offload it
    assert "--no-mmproj-offload" not in serve_llama_argv(pod_env(dep(mode="split"), True))  # no projector, no flag


def test_own_draft_or_projector_files_need_the_mirror_even_when_it_is_off():
    """With HF_MIRROR_ENABLED=false the nodes use huggingface.co, where local/<name> does not exist."""
    assert not builders.uses_mirror(dep(), S)
    assert builders.uses_mirror(dep(repo="local/own"), S)
    assert builders.uses_mirror(dep(draft_repo="local/small", draft_file="d.gguf"), S)
    assert builders.uses_mirror(dep(mmproj_file="local/vision/p.gguf"), S)
    assert not builders.uses_mirror(dep(draft_repo="org/draft", draft_file="d.gguf", mmproj_file="mmproj.gguf"), S)
    assert builders.uses_mirror(dep(), M)
    manifest = replicas_daemonset(dep(draft_repo="local/small", draft_file="d.gguf"), "cpu", S)
    assert "HF_ENDPOINT" in envs(manifest)  # the pod is pointed at the mirror, not at Hugging Face


def test_every_repo_of_a_deployment_is_kept_in_the_cache():
    assert builders.deployment_repos(dep()) == {"org/model-GGUF"}
    d = dep(draft_repo="org/draft", draft_file="d.gguf", mmproj_file="local/vision/p.gguf")
    assert builders.deployment_repos(d) == {"org/model-GGUF", "org/draft", "local/vision"}
    assert builders.deployment_repos(dep(mmproj_file="mmproj-F16.gguf")) == {"org/model-GGUF"}


# ---------- the memory estimate ----------

E4B_DEVICE = E4B_LAYERS_BYTES + E4B_OUTPUT_BYTES


def e4b():
    return parse_gguf_header(build_gguf(E4B_KVS))


def test_kv_bytes_scale_with_the_cache_type():
    assert est.kv_elem_bytes("f16") == 2.0
    assert est.kv_elem_bytes("q8_0") == 1.0625
    assert est.kv_elem_bytes("q4_0") == 0.5625
    assert est.kv_elem_bytes(None) == 2.0 and est.kv_elem_bytes("weird") == 2.0
    meta = e4b()
    f16 = est.kv_bytes(meta, 131072, est.kv_elem_bytes("f16"))
    q8 = est.kv_bytes(meta, 131072, est.kv_elem_bytes("q8_0"))
    q4 = est.kv_bytes(meta, 131072, est.kv_elem_bytes("q4_0"))
    assert q8 == pytest.approx(f16 * 1.0625 / 2, abs=64) and q4 == pytest.approx(f16 * 0.5625 / 2, abs=64)


def test_e4b_at_131072_with_q8_0_halves_the_context_part_of_the_measured_5_4_gb():
    """RTX 4070 measurement: 131072 x 1, f16 -> 5.4 GB VRAM. The context memory is the KV; q8_0 holds about half."""
    meta = e4b()
    f16 = est.estimate(E4B_FILE_BYTES, meta, 131072, 1, device_weights_bytes=E4B_DEVICE, kv_cache_type="f16")
    q8 = est.estimate(E4B_FILE_BYTES, meta, 131072, 1, device_weights_bytes=E4B_DEVICE, kv_cache_type="q8_0")
    assert abs(f16.device_bytes - 5.4e9) <= 0.25 * 5.4e9  # the same fit as the f16 test of test_catalog
    assert q8.kv_bytes / f16.kv_bytes == pytest.approx(0.53125, abs=0.001)  # about half
    assert q8.device_bytes == f16.device_bytes - (f16.kv_bytes - q8.kv_bytes)
    assert q8.device_bytes < f16.device_bytes
    assert est.estimate(E4B_FILE_BYTES, meta, 131072, 1).kv_bytes == f16.kv_bytes  # f16 is the default of the function


def test_draft_and_projector_are_parts_of_the_estimate():
    meta = e4b()
    draft_meta = est.meta_from_dict({
        "arch": "llama", "n_layers": 12, "n_heads": 8, "n_kv_heads": 2, "head_dim": 64, "max_ctx": 8192, "embedding": 512,
    })
    plain = est.estimate(5 * GiB, meta, 8192, 2, device_weights_bytes=3 * GiB, kv_cache_type="q8_0")
    both = est.estimate(
        5 * GiB, meta, 8192, 2, device_weights_bytes=3 * GiB, kv_cache_type="q8_0",
        draft_weights_bytes=700 * MiB, draft_meta=draft_meta, mmproj_bytes=900 * MiB,
    )
    draft_kv = est.kv_bytes(draft_meta, 8192 * 2, parallel=2)  # the draft keeps an f16 cache
    assert (both.draft_weights_bytes, both.draft_kv_bytes, both.mmproj_bytes) == (700 * MiB, draft_kv, 900 * MiB)
    extra = 700 * MiB + draft_kv + 900 * MiB
    assert both.total_bytes == plain.total_bytes + extra
    assert both.per_node_bytes == plain.per_node_bytes + extra
    assert both.device_bytes == plain.device_bytes + extra  # on a GPU node all three sit in VRAM
    assert both.host_bytes == plain.host_bytes
    assert both.kv_bytes == plain.kv_bytes  # kv_bytes stays the model's own
    assert (plain.draft_weights_bytes, plain.draft_kv_bytes, plain.mmproj_bytes) == (0, 0, 0)


def split_meta(**kw):
    base = {
        "weights_bytes": 10 * GiB + GiB // 2 + GiB // 4, "arch": "llama", "n_layers": 10, "n_heads": 1, "n_kv_heads": 1,
        "head_dim": 8, "max_ctx": 8192, "value_dim": None, "mla": False, "nextn_layers": 0, "layers": [GiB] * 10,
        "output_bytes": GiB // 2, "embd_bytes": GiB // 4, "max_tensor_bytes": GiB // 2, "n_vocab": 1000,
    }
    return {**base, **kw}


def test_split_placement_uses_the_kv_type():
    big_kv = split_meta(n_kv_heads=8, head_dim=128, n_heads=8, max_ctx=131072)
    nodes = [pl.NodeMemory("a", "cpu", None, 28000), pl.NodeMemory("b", "cpu", None, 28000)]
    f16 = pl.plan_split(big_kv, nodes, 131072, True, 70, kv_cache_type="f16")
    q8 = pl.plan_split(big_kv, nodes, 131072, True, 70, kv_cache_type="q8_0")
    assert q8.need_bytes < f16.need_bytes
    assert f16.need_bytes - q8.need_bytes == pytest.approx(
        est.kv_bytes(est.meta_from_dict(big_kv), 131072) * (1 - 1.0625 / 2), rel=0.001
    )
    assert pl.layer_costs(big_kv, 4096, 1, "q4_0") != pl.layer_costs(big_kv, 4096, 1, "f16")


def test_split_leader_node_pays_for_the_draft_and_the_projector():
    nodes = [pl.NodeMemory("a", "cpu", None, 28000), pl.NodeMemory("b", "cpu", None, 28000)]
    m = split_meta()
    base = pl.plan_split(m, nodes, 1024, True, 70)
    with_extra = pl.plan_split(m, nodes, 1024, True, 70, leader_extra_bytes=3 * GiB)
    assert with_extra.devices[0].budget_bytes == base.devices[0].budget_bytes - 3 * GiB  # the leader's node
    assert with_extra.devices[1].budget_bytes == base.devices[1].budget_bytes  # the other nodes are untouched
    assert with_extra.need_bytes == base.need_bytes  # the layers cost the same, only the leader's room is smaller


def stored_dep(**kw):
    draft = {
        "weights_bytes": 700 * MiB, "arch": "llama", "n_layers": 12, "n_heads": 8, "n_kv_heads": 2, "head_dim": 64,
        "max_ctx": 8192, "embedding": 512,
    }
    d = dep(
        mode="split", meta={**split_meta(), "extras": est.stored_extras(draft, 900 * MiB, est.extras_key("org/d", "d.gguf", "p.gguf"))},
        draft_repo="org/d", draft_file="d.gguf", mmproj_file="p.gguf", ctx_per_request=1024, parallel=1,
        kv_cache_type="q8_0",
    )
    return SimpleNamespace(**{**d.__dict__, **kw})


def test_a_saved_deployment_counts_its_stored_extras_on_the_leader():
    nodes = [pl.NodeMemory("a", "cpu", None, 28000), pl.NodeMemory("b", "cpu", None, 28000)]
    d = stored_dep()
    w, kv, mm = est.dep_extras(d)
    assert (w, mm) == (700 * MiB, 900 * MiB) and kv == est.kv_bytes(est.meta_from_dict(d.meta["extras"]["draft"]), 1024)
    plan = pl.plan_for(d, nodes)
    plain = pl.plan_for(stored_dep(draft_repo=None, draft_file=None, mmproj_file=None), nodes)
    assert plan.devices[0].budget_bytes == plain.devices[0].budget_bytes - (w + kv + mm)


def test_extras_of_another_choice_are_not_counted():
    d = stored_dep(draft_file="other.gguf")  # the stored sizes belong to d.gguf: stale until read again
    assert est.dep_extras(d) == (0, 0, 0)
    assert est.dep_extras(stored_dep(meta=None)) == (0, 0, 0)
