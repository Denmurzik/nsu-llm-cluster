"""Load progress of inference pods: the mirror's transfer report and /health of the pod -> a stage."""

from types import SimpleNamespace

import httpx

from app.deployments.progress import ProgressSource, file_matches, pod_progress

GB = 10**9


def dep(**kw):
    base = dict(engine="llamacpp", repo="org/m-GGUF", file="m-Q4_K_M.gguf", meta={"weights_bytes": 10 * GB})
    return SimpleNamespace(**{**base, **kw})


def t(path, offset, size, active=True, repo="org/m-GGUF"):
    return {"repo": repo, "path": path, "offset": offset, "size": size, "active": active, "updated": 1.0}


def test_files_of_a_deployment():
    d = dep()
    assert file_matches(d, "m-Q4_K_M.gguf") and not file_matches(d, "m-Q8_0.gguf")
    shards = dep(file="Q2/big-00001-of-00003.gguf")
    assert file_matches(shards, "Q2/big-00003-of-00003.gguf") and file_matches(shards, "Q2/big-00001-of-00003.gguf")
    assert not file_matches(shards, "Q2/big-00001-of-00004.gguf") and not file_matches(shards, "Q4/big-00002-of-00003.gguf")
    assert file_matches(dep(engine="vllm", file=None), "model-00001-of-00002.safetensors")


def test_ready_pod_is_ready_whatever_else_is_known():
    assert pod_progress(dep(), ready=True, phase="Running", health=None, transfers=None) == {
        "stage": "ready", "percent": None, "done_bytes": None, "total_bytes": None,
    }


def test_downloading_reports_the_share_of_the_model_size():
    items = [t("m-Q4_K_M.gguf", 4_250_000_000, 10 * GB), t("m-Q8_0.gguf", 9 * GB, 9 * GB), t("x", 1, 1, repo="other/r")]
    p = pod_progress(dep(), ready=False, phase="Running", health=None, transfers=items)
    assert p == {"stage": "downloading", "percent": 42.5, "done_bytes": 4_250_000_000, "total_bytes": 10 * GB}


def test_shards_add_up_against_the_whole_model():
    d = dep(file="big-00001-of-00002.gguf", meta={"weights_bytes": 8 * GB})
    items = [t("big-00001-of-00002.gguf", 4 * GB, 4 * GB, active=False), t("big-00002-of-00002.gguf", 2 * GB, 4 * GB)]
    p = pod_progress(d, ready=False, phase="Running", health=None, transfers=items)
    assert (p["stage"], p["percent"], p["done_bytes"], p["total_bytes"]) == ("downloading", 75.0, 6 * GB, 8 * GB)


def test_without_meta_the_sizes_from_the_mirror_are_the_total():
    p = pod_progress(dep(meta=None), ready=False, phase="Running", health=None, transfers=[t("m-Q4_K_M.gguf", 1 * GB, 4 * GB)])
    assert (p["percent"], p["total_bytes"]) == (25.0, 4 * GB)


def test_a_503_from_llama_server_means_the_weights_are_loading():
    items = [t("m-Q4_K_M.gguf", 10 * GB, 10 * GB, active=False)]
    for health in (503, 200):
        p = pod_progress(dep(), ready=False, phase="Running", health=health, transfers=items)
        assert p == {"stage": "loading", "percent": None, "done_bytes": None, "total_bytes": None}
    # the download is complete but llama-server does not answer yet: it is starting on the weights
    assert pod_progress(dep(), ready=False, phase="Running", health=None, transfers=items)["stage"] == "loading"


def test_no_transfer_and_no_answer_is_starting():
    assert pod_progress(dep(), ready=False, phase="Running", health=None, transfers=[])["stage"] == "starting"
    assert pod_progress(dep(), ready=False, phase="Running", health=None, transfers=None)["stage"] == "starting"


def test_a_pod_that_is_not_running_has_no_progress():
    assert pod_progress(dep(), ready=False, phase="Pending", health=None, transfers=[t("m-Q4_K_M.gguf", 1, 2)]) is None


async def test_source_reads_the_mirror_report_with_its_key():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/_mirror/transfers":
            return httpx.Response(200, json={"clients": {"n1": [t("m.gguf", 1, 2)]}, "downloads": []})
        if request.url.host == "10.0.0.7":
            return httpx.Response(503, json={"error": {"message": "Loading model"}})
        return httpx.Response(200, json={"status": "ok"})

    src = ProgressSource("http://127.0.0.1:8090", "hf_key", transport=httpx.MockTransport(handler))
    assert (await src.transfers()) == {"n1": [t("m.gguf", 1, 2)]}
    assert seen[0].headers["authorization"] == "Bearer hf_key"
    assert await src.health("http://10.0.0.7:18080/health") == 503
    assert await src.health("http://10.0.0.8:18080/health") == 200


async def test_source_degrades_to_unknown():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    src = ProgressSource("http://127.0.0.1:8090", "k", transport=httpx.MockTransport(down))
    assert await src.transfers() is None
    assert await src.health("http://10.0.0.7:18080/health") is None
    bad = ProgressSource("http://127.0.0.1:8090", "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="nope")))
    assert await bad.transfers() is None
    assert await ProgressSource(None, "k").transfers() is None  # mirror turned off
