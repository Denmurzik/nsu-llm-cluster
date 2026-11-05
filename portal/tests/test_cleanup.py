"""Cleanup of model weights (task 2-26): what a node needs, the signature, the arguments of the script, its answer."""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.config import Settings
from app.models import ModelDeployment, NodeAssignment
from app.nodes import cleanup
from app.workloads import builders

NOON = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)  # a Wednesday, 12:00 in Novosibirsk
CLOSED = {"days": [1, 2], "start": "09:00", "end": "18:00"}


def dep(id=1, name="m1", repo="org/m", file="m-Q4.gguf", **kw) -> ModelDeployment:
    fields = dict(engine="llamacpp", mode="replicas", accel="any", enabled=True, auto_assign=False, schedule=None)
    return ModelDeployment(id=id, name=name, repo=repo, file=file, **{**fields, **kw})


def row(deployment_id, maintenance=False) -> NodeAssignment:
    return NodeAssignment(node_name="pc01", deployment_id=deployment_id, source="k8s", maintenance=maintenance)


# ---- what a deployment keeps on a node -----------------------------------------------------------------------------------


def test_a_deployment_keeps_its_model_its_draft_and_its_projector():
    d = dep(draft_repo="org/draft", draft_file="d.gguf", mmproj_file="mmproj-F16.gguf")
    assert cleanup.deployment_files(d) == {"org/m:m-Q4.gguf", "org/draft:d.gguf", "org/m:mmproj-F16.gguf"}


def test_an_own_model_and_an_own_projector_are_files_of_the_local_repository():
    d = dep(repo="local/mine", file="mine.gguf", draft_repo="local/small", draft_file="s.gguf", mmproj_file="local/vis/proj.gguf")
    assert cleanup.deployment_files(d) == {"local/mine:mine.gguf", "local/small:s.gguf", "local/vis:proj.gguf"}


def test_a_vllm_deployment_keeps_no_gguf_but_its_cache():
    d = dep(engine="vllm", repo="org/v", file=None)
    assert cleanup.deployment_files(d) == set()
    assert cleanup.keep_for([d]) == cleanup.Keep((), False, True)


def test_a_split_deployment_keeps_the_tensor_cache_unless_told_otherwise():
    d = dep(mode="split")
    assert cleanup.keep_for([d]).rpc is True
    assert cleanup.keep_for([d], rpc=False).rpc is False
    assert cleanup.keep_for([dep()], rpc=True).rpc is True
    assert cleanup.keep_for([]) == cleanup.Keep()


def test_keep_arguments_are_what_the_script_takes():
    keep = cleanup.keep_for([dep(), dep(id=2, name="m2", repo="org/n", file="n.gguf", mode="split", draft_repo="org/d", draft_file="d.gguf")])
    assert keep.args() == ["--keep", "org/d:d.gguf", "--keep", "org/m:m-Q4.gguf", "--keep", "org/n:n.gguf", "--keep-rpc"]
    assert cleanup.Keep().args() == []
    assert cleanup.Keep(vllm=True).args() == ["--keep-vllm"]
    assert keep.order("abc") == {"id": "abc", "keep": list(keep.files), "keep_rpc": True, "keep_vllm": False}


# ---- the signature ---------------------------------------------------------------------------------------------------


def test_signature_round_trips_and_idle_is_empty():
    sig = cleanup.signature(dep(mode="split", draft_repo="org/d", draft_file="d.gguf"))
    assert sig.rpc == "org/m:m-Q4.gguf" and sig.files == ("org/d:d.gguf", "org/m:m-Q4.gguf")
    assert cleanup.Signature.load(sig.dump()) == sig
    assert cleanup.signature(None) == cleanup.IDLE == cleanup.Signature()
    assert cleanup.Signature.load("junk") is None and cleanup.Signature.load('{"files": 1}') is None


def test_signature_ignores_what_does_not_change_the_files():
    base = cleanup.signature(dep())
    assert cleanup.signature(dep(ctx_per_request=1, parallel=3, kv_cache_type="q4_0", revision="a" * 40, name="other")) == base
    assert cleanup.signature(dep(file="m-Q8.gguf")) != base
    assert cleanup.signature(dep(mode="split")) != base  # the tensor cache belongs to the split model
    assert cleanup.signature(dep(mode="split", group_size=5)) == cleanup.signature(dep(mode="split"))


# ---- who needs what -------------------------------------------------------------------------------------------------------


def test_no_deployment_is_unassigned_deleted_or_disabled_not_outside_the_window():
    deps = [dep(id=1), dep(id=2, name="off", enabled=False), dep(id=3, name="closed", schedule=CLOSED)]
    for r in (None, row(None), row(99), row(2)):  # no row, no deployment, a deleted one, a disabled one
        assigned, needed = cleanup.node_deployments(r, deps, "cpu", "k8s", NOON)
        assert (assigned, needed) == (None, [])
    assigned, needed = cleanup.node_deployments(row(1), deps, "cpu", "k8s", NOON)
    assert assigned is deps[0] and needed == [deps[0]]
    assigned, needed = cleanup.node_deployments(row(3), deps, "cpu", "k8s", NOON)  # outside its window: still its files
    assert assigned is deps[2] and deps[2] in needed
    assigned, needed = cleanup.node_deployments(row(1, maintenance=True), deps, "cpu", "k8s", NOON)
    assert assigned is deps[0]  # on maintenance the assignment, and the files, stay


def test_a_node_lent_to_another_deployment_needs_its_files_too():
    mine = dep(id=1, name="mine", schedule=CLOSED)
    lender = dep(id=2, name="lender", repo="org/l", file="l.gguf", auto_assign=True)
    assigned, needed = cleanup.node_deployments(row(1), [mine, lender], "cpu", "k8s", NOON)
    assert assigned is mine and needed == [mine, lender]
    keep = cleanup.keep_for(needed)
    assert keep.files == ("org/l:l.gguf", "org/m:m-Q4.gguf")


# ---- the answer of the script --------------------------------------------------------------------------------------------


LOG = 'removed org__old 7000\nRESULT {"freed_bytes": 7000, "models_bytes": 99, "removed": ["org__old"], "skipped": ["rpc"], "dry_run": false}\n'


def test_the_result_line_is_read_from_a_log():
    o = cleanup.parse_result("pulling image\n" + LOG)
    assert (o.state, o.freed_bytes, o.models_bytes, o.removed, o.skipped) == ("done", 7000, 99, ["org__old"], ["rpc"])
    assert cleanup.parse_result("no result here\n") is None
    assert cleanup.parse_result("RESULT {not json\n") is None


def test_a_result_is_cut_to_a_sane_size():
    huge = '{"freed_bytes": -5, "models_bytes": true, "removed": %s, "skipped": [], "error": ""}' % str(["x" * 900] * 80).replace("'", '"')
    o = cleanup.parse_result("RESULT " + huge)
    assert o.freed_bytes == 0 and o.models_bytes is None
    assert len(o.removed) == 50 and len(o.removed[0]) == 512


async def test_a_job_is_read_through_its_state_and_its_log():
    from tests.fakes.k8s import FakeK8s

    k8s = FakeK8s()
    state = SimpleNamespace(k8s=k8s, settings=Settings())
    ref, error = await cleanup.send(state, "pc01", "k8s", "cpu", cleanup.Keep(("a/b:c.gguf",)))
    assert error is None
    assert (await cleanup.poll(state, "pc01", "k8s", ref)).state == "running"
    k8s.finish_job(ref, ok=True, log=LOG)
    assert (await cleanup.poll(state, "pc01", "k8s", ref)).freed_bytes == 7000
    k8s.finish_job(ref, ok=True, log="no result line")
    assert (await cleanup.poll(state, "pc01", "k8s", ref)).state == "done"  # the Job ran through
    k8s.finish_job(ref, ok=False, log="line1\nmodels-cleanup: bad argument")
    failed = await cleanup.poll(state, "pc01", "k8s", ref)
    assert failed.state == "failed" and "bad argument" in failed.error
    # gone and never seen succeeding: failed, so the cleanup is tried again (it is harmless to repeat)
    assert (await cleanup.poll(state, "pc01", "k8s", "llm-purge-collected")).state == "failed"
    k8s.unavailable = True
    assert (await cleanup.poll(state, "pc01", "k8s", ref)).state == "running"  # the cluster does not answer: ask again


async def test_a_job_killed_at_its_deadline_is_failed_not_running_and_not_done():
    from tests.fakes.k8s import FakeK8s

    k8s = FakeK8s()
    state = SimpleNamespace(k8s=k8s, settings=Settings())
    ref, _ = await cleanup.send(state, "pc01", "k8s", "cpu", cleanup.Keep(()))
    k8s.kill_job(ref)  # status.failed stays 0, the Failed condition says it
    o = await cleanup.poll(state, "pc01", "k8s", ref)
    assert o.state == "failed" and "DeadlineExceeded" in o.error
    k8s.job_conditions[ref] = ("Complete", "")  # a Complete Job without a counted pod still ended well
    k8s.job_logs[ref] = LOG
    assert (await cleanup.poll(state, "pc01", "k8s", ref)).freed_bytes == 7000


def test_the_job_runs_the_script_of_the_node_image_on_the_models_volume():
    s = Settings()
    job = builders.purge_job("pc01", "nvidia", ["--keep", "a/b:c.gguf", "--keep-rpc"], s)
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert job["metadata"]["generateName"] == "llm-purge-" and job["metadata"]["labels"]["llm.nsu/role"] == "purge"
    assert pod["nodeName"] == "pc01" and pod["restartPolicy"] == "Never" and pod["hostNetwork"] is True
    assert c["command"] == ["/opt/llm/bin/models-cleanup.sh", "--keep", "a/b:c.gguf", "--keep-rpc"]
    assert c["image"] == f"{s.node_image_repo}:{s.node_image_tag}-gpu" and c["imagePullPolicy"] == "IfNotPresent"
    assert c["volumeMounts"] == [{"name": "models", "mountPath": "/var/lib/llm-models"}]
    assert pod["volumes"] == [{"name": "models", "hostPath": {"path": "/var/lib/llm-models", "type": "DirectoryOrCreate"}}]
    assert c["env"] == [{"name": "LLAMA_CACHE", "value": "/var/lib/llm-models"}]
    assert c["securityContext"] == {"allowPrivilegeEscalation": False, "privileged": False}
    assert pod["automountServiceAccountToken"] is False and "runtimeClassName" not in pod
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == builders.PURGE_DEADLINE_S
    cpu = builders.purge_job("pc02", None, [], s)["spec"]["template"]["spec"]["containers"][0]
    assert cpu["image"].endswith("-cpu") and cpu["command"] == ["/opt/llm/bin/models-cleanup.sh"]
