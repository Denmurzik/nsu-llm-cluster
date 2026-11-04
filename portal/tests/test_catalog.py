import inspect
import json

import httpx
import pytest
import respx

from app.catalog import estimate as est
from app.catalog.gguf import GgufInvalid, GgufLayout, GgufMeta, GgufTruncated, gguf_layout, parse_gguf, parse_gguf_header
from app.catalog.hf import GgufChoice, HfClient, HfFile, HfNotFound, HfUnavailable, VllmInfo, parse_quant
from tests.fakes.hf import FakeHf
from tests.helpers.gguf_builder import (
    BOOL, FLOAT32, FLOAT64, INT8, INT16, INT32, INT64, UINT8, UINT16, UINT64, Typed, arch_kvs, build_gguf,
)

GiB = 2**30
MiB = 2**20
EP = "https://hf.test"


# ---------- GGUF parser ----------


def test_parse_header_scalar_kv():
    data = build_gguf(arch_kvs("llama", n_layers=32, n_heads=32, n_kv_heads=8, embedding=4096, max_ctx=131072))
    assert parse_gguf_header(data) == GgufMeta(
        arch="llama", n_layers=32, n_heads=32, n_kv_heads=8, head_dim=128, embedding=4096, max_ctx=131072
    )


def test_parse_header_v2_and_all_value_types():
    kvs = {
        "a.u8": Typed(UINT8, 1), "a.i8": Typed(INT8, -1), "a.u16": Typed(UINT16, 2), "a.i16": Typed(INT16, -2),
        "a.i32": Typed(INT32, -3), "a.f32": Typed(FLOAT32, 1.5), "a.bool": Typed(BOOL, True),
        "a.u64": Typed(UINT64, 2**40), "a.i64": Typed(INT64, -5), "a.f64": Typed(FLOAT64, 2.5),
        "a.strs": ["x", "yy"], "a.floats": [1.0, 2.0],
        **arch_kvs("qwen3", n_layers=28, n_heads=16, n_kv_heads=8, embedding=2048, max_ctx=40960, key_length=128),
    }
    meta = parse_gguf_header(build_gguf(kvs, version=2))
    assert (meta.arch, meta.n_layers, meta.head_dim, meta.max_ctx) == ("qwen3", 28, 128, 40960)


def test_parse_header_array_kv_heads_uses_max():
    data = build_gguf(arch_kvs("gemma3n", n_layers=4, n_heads=8, n_kv_heads=[2, 4, 2, 4], embedding=2048))
    meta = parse_gguf_header(data)
    assert meta.n_kv_heads == 4
    assert meta.n_heads == 8


def test_parse_header_key_length_fallback():
    with_key = parse_gguf_header(build_gguf(arch_kvs("gemma3", n_heads=4, embedding=1152, key_length=256)))
    without = parse_gguf_header(build_gguf(arch_kvs("gemma3", n_heads=4, embedding=1152)))
    assert with_key.head_dim == 256
    assert without.head_dim == 288


def test_missing_kv_heads_falls_back_to_head_count():
    kvs = arch_kvs("llama", n_heads=16)
    del kvs["llama.attention.head_count_kv"]
    kvs["tokenizer.ggml.model"] = "gpt2"
    assert parse_gguf_header(build_gguf(kvs)).n_kv_heads == 16


def test_truncated_reports_needed_bytes():
    data = build_gguf(arch_kvs("llama"))
    cut = data[: len(data) - 10]
    with pytest.raises(GgufTruncated) as exc:
        parse_gguf_header(cut)
    assert len(cut) < exc.value.needed <= len(data)
    with pytest.raises(GgufTruncated) as exc:
        parse_gguf_header(b"GGUF\x03")
    assert exc.value.needed > 5


def test_truncated_inside_string_needs_full_string():
    data = build_gguf({**arch_kvs(), "general.name": "x" * 1000})
    with pytest.raises(GgufTruncated) as exc:
        parse_gguf_header(data[:200])
    assert exc.value.needed >= 1000


def test_bad_magic_raises():
    with pytest.raises(GgufInvalid):
        parse_gguf_header(build_gguf(arch_kvs(), magic=b"GGML"))
    with pytest.raises(GgufInvalid):
        parse_gguf_header(build_gguf(arch_kvs(), version=1))


def test_parse_stops_before_huge_tokenizer_array():
    kvs = arch_kvs("llama", key_length=128)
    head = build_gguf(kvs)
    # tokenizer array: 5 MB of 10-byte strings; the data is cut inside it
    kvs["tokenizer.ggml.tokens"] = ["tokentoken"] * 500_000
    data = build_gguf(kvs)
    assert len(data) > 5 * 10**6
    meta = parse_gguf_header(data[: len(head) + 2000])
    assert (meta.arch, meta.n_layers, meta.head_dim) == ("llama", 32, 128)


def test_truncated_inside_non_tokenizer_key_after_required_keys_returns_meta():
    kvs = arch_kvs("llama", key_length=128)
    head = build_gguf(kvs)
    kvs["general.description"] = "d" * 100_000
    data = build_gguf(kvs)
    meta = parse_gguf_header(data[: len(head) + 5000])
    assert (meta.arch, meta.n_layers, meta.head_dim) == ("llama", 32, 128)
    # cut in the middle of the next key's name or type id as well
    assert parse_gguf_header(data[: len(head) + 3]).arch == "llama"


def test_incomplete_required_keys_before_tokenizer_still_truncated():
    kvs = {"general.architecture": "llama", "tokenizer.ggml.tokens": ["a"] * 1000}
    with pytest.raises(GgufTruncated):
        parse_gguf_header(build_gguf(kvs)[:200])


@pytest.mark.network
def test_real_gemma3_270m_header():
    url = "https://huggingface.co/ggml-org/gemma-3-270m-it-GGUF/resolve/main/gemma-3-270m-it-Q8_0.gguf"
    try:
        r = httpx.get(url, headers={"Range": "bytes=0-2097151"}, follow_redirects=True, timeout=30)
    except httpx.TransportError:
        pytest.skip("huggingface.co is not reachable")
    assert r.status_code in (200, 206)
    meta = parse_gguf_header(r.content[: 2 * MiB])
    assert meta.arch == "gemma3"
    assert meta.max_ctx == 32768
    assert meta.n_layers > 0 and meta.n_kv_heads > 0 and meta.head_dim > 0


# ---------- HF client ----------


def tree_entry(path, size, type_="file"):
    return {"type": type_, "path": path, "size": size, "oid": "x"}


@respx.mock
async def test_gguf_files_filters_mmproj_mtp_and_groups_shards():
    respx.get(f"{EP}/api/models/o/r/tree/main").mock(
        return_value=httpx.Response(
            200,
            json=[
                tree_entry("README.md", 100),
                tree_entry("mmproj-F16.gguf", 7),
                tree_entry("sub/mmproj-BF16.gguf", 7),
                tree_entry("mtp-x.gguf", 9),
                tree_entry("model-Q4_K_M.gguf", 4000),
                tree_entry("big-00002-of-00002.gguf", 300),
                tree_entry("big-00001-of-00002.gguf", 200),
                tree_entry("UD-Q4_K_XL", 0, "directory"),
                tree_entry("UD-Q4_K_XL/m-UD-Q4_K_XL.gguf", 5000),
            ],
        )
    )
    files = await HfClient(EP, None).gguf_files("o/r")
    by_file = {c.file: c for c in files}
    assert set(by_file) == {"model-Q4_K_M.gguf", "big-00001-of-00002.gguf", "UD-Q4_K_XL/m-UD-Q4_K_XL.gguf"}
    big = by_file["big-00001-of-00002.gguf"]
    assert big.size_bytes == 500
    assert big.shards == ["big-00001-of-00002.gguf", "big-00002-of-00002.gguf"]
    single = by_file["model-Q4_K_M.gguf"]
    assert (single.size_bytes, single.quant, single.shards) == (4000, "Q4_K_M", ["model-Q4_K_M.gguf"])
    assert by_file["UD-Q4_K_XL/m-UD-Q4_K_XL.gguf"].quant == "UD-Q4_K_XL"


@pytest.mark.parametrize(
    "name,quant",
    [
        ("gemma-4-E4B-it-Q4_K_M.gguf", "Q4_K_M"),
        ("gemma-3-270m-it-Q8_0.gguf", "Q8_0"),
        ("Qwen3-30B-A3B-UD-Q4_K_XL.gguf", "UD-Q4_K_XL"),
        ("UD-Q4_K_XL/Qwen3-UD-Q4_K_XL-00001-of-00003.gguf", "UD-Q4_K_XL"),
        ("model-IQ4_XS.gguf", "IQ4_XS"),
        ("model-BF16.gguf", "BF16"),
        ("model-F16.gguf", "F16"),
        ("model-q5_k_s.gguf", "Q5_K_S"),
        ("model-Q6_K-00001-of-00002.gguf", "Q6_K"),
        ("model-UD-IQ2_M.gguf", "UD-IQ2_M"),
        ("model-MXFP4.gguf", "MXFP4"),
        ("plain-model.gguf", None),
    ],
)
def test_quant_parsing(name, quant):
    assert parse_quant(name) == quant


@respx.mock
async def test_list_files_follows_pagination_and_keeps_subfolders():
    # the cursor route is registered first so that it wins over the generic one
    page2 = respx.get(f"{EP}/api/models/o/r/tree/main", params={"recursive": "true", "cursor": "abc"}).mock(
        return_value=httpx.Response(200, json=[tree_entry("c.gguf", 3)])
    )
    page1 = respx.get(f"{EP}/api/models/o/r/tree/main", params={"recursive": "true"}).mock(
        return_value=httpx.Response(
            200,
            json=[tree_entry("a.gguf", 1), tree_entry("UD-Q4_K_XL/b.gguf", 2)],
            headers={"Link": f'<{EP}/api/models/o/r/tree/main?recursive=true&cursor=abc>; rel="next"'},
        )
    )
    files = await HfClient(EP, None).list_files("o/r")
    assert [(f.path, f.size) for f in files] == [("a.gguf", 1), ("UD-Q4_K_XL/b.gguf", 2), ("c.gguf", 3)]
    assert page1.called and page2.called


@respx.mock
async def test_list_files_does_not_follow_a_next_link_to_another_host():
    evil = respx.get("https://evil.example/steal").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{EP}/api/models/o/r/tree/main").mock(
        return_value=httpx.Response(
            200, json=[tree_entry("a.gguf", 1)], headers={"Link": '<https://evil.example/steal>; rel="next"'}
        )
    )
    files = await HfClient(EP, "hf_secret").list_files("o/r")
    assert [f.path for f in files] == ["a.gguf"]
    assert not evil.called  # the Authorization header never leaves the endpoint's host


@respx.mock
async def test_list_files_follows_a_relative_next_link_on_the_same_host():
    page2 = respx.get(f"{EP}/api/models/o/r/tree/main", params={"recursive": "true", "cursor": "z"}).mock(
        return_value=httpx.Response(200, json=[tree_entry("b.gguf", 2)])
    )
    respx.get(f"{EP}/api/models/o/r/tree/main", params={"recursive": "true"}).mock(
        return_value=httpx.Response(
            200,
            json=[tree_entry("a.gguf", 1)],
            headers={"Link": '</api/models/o/r/tree/main?recursive=true&cursor=z>; rel="next"'},
        )
    )
    files = await HfClient(EP, "hf_secret").list_files("o/r")
    assert [f.path for f in files] == ["a.gguf", "b.gguf"]
    assert page2.calls.last.request.headers["authorization"] == "Bearer hf_secret"


@respx.mock
async def test_hf_sends_bearer_token_only_when_set():
    route = respx.get(f"{EP}/api/models/o/r/tree/main").mock(return_value=httpx.Response(200, json=[]))
    await HfClient(EP, "hf_secret").list_files("o/r")
    assert route.calls.last.request.headers["authorization"] == "Bearer hf_secret"
    await HfClient(EP, None).list_files("o/r")
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_hf_404_raises_not_found():
    respx.get(f"{EP}/api/models/o/missing/tree/main").mock(return_value=httpx.Response(404, json={"error": "x"}))
    with pytest.raises(HfNotFound):
        await HfClient(EP, None).list_files("o/missing")
    respx.get(f"{EP}/o/r/resolve/main/nope.gguf").mock(return_value=httpx.Response(404))
    with pytest.raises(HfNotFound):
        await HfClient(EP, None).gguf_meta("o/r", "nope.gguf")


@respx.mock
async def test_hf_connect_error_raises_unavailable():
    respx.get(f"{EP}/api/models/o/r/tree/main").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(HfUnavailable):
        await HfClient(EP, None).list_files("o/r")


@respx.mock
async def test_hf_server_error_raises_unavailable():
    respx.get(f"{EP}/api/models/o/r/tree/main").mock(return_value=httpx.Response(503))
    with pytest.raises(HfUnavailable):
        await HfClient(EP, None).list_files("o/r")


def _ranged(data: bytes, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers["range"]
        calls.append(rng)
        start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
        return httpx.Response(206, content=data[start : end + 1])

    return handler


@respx.mock
async def test_gguf_meta_reads_with_range_and_follows_redirect():
    data = build_gguf(arch_kvs("llama", key_length=128))
    calls: list[str] = []
    respx.get(f"{EP}/o/r/resolve/main/sub/m.gguf").mock(
        return_value=httpx.Response(302, headers={"Location": "https://cdn.test/blob"})
    )
    respx.get("https://cdn.test/blob").mock(side_effect=_ranged(data, calls))
    meta = await HfClient(EP, None).gguf_meta("o/r", "sub/m.gguf")
    assert meta.arch == "llama" and meta.head_dim == 128
    assert calls == ["bytes=0-2097151"]


@respx.mock
async def test_gguf_meta_rereads_when_truncated():
    # the required keys sit behind a ~3 MB string, so the first 2 MiB window is truncated
    kvs = {"general.description": "d" * (3 * MiB), **arch_kvs("llama")}
    data = build_gguf(kvs)
    calls: list[str] = []
    respx.get(f"{EP}/o/r/resolve/main/m.gguf").mock(side_effect=_ranged(data, calls))
    meta = await HfClient(EP, None).gguf_meta("o/r", "m.gguf")
    assert meta.arch == "llama"
    assert calls[0] == "bytes=0-2097151"
    assert len(calls) == 2
    assert int(calls[1].split("-")[1]) + 1 >= 3 * MiB


@respx.mock
async def test_gguf_meta_gives_up_after_32_mib():
    kvs = {"general.description": "d" * (40 * MiB), **arch_kvs("llama")}
    data = build_gguf(kvs)
    calls: list[str] = []
    respx.get(f"{EP}/o/r/resolve/main/m.gguf").mock(side_effect=_ranged(data, calls))
    with pytest.raises(HfUnavailable):
        await HfClient(EP, None).gguf_meta("o/r", "m.gguf")
    assert int(calls[-1].split("-")[1]) + 1 == 32 * MiB


@respx.mock
async def test_gguf_meta_does_not_buffer_whole_file_when_range_ignored():
    data = build_gguf(arch_kvs("llama")) + b"\0" * (6 * MiB)
    respx.get(f"{EP}/o/r/resolve/main/m.gguf").mock(return_value=httpx.Response(200, content=data))
    meta = await HfClient(EP, None).gguf_meta("o/r", "m.gguf")
    assert meta.arch == "llama"


@respx.mock
async def test_vllm_info_reads_config_and_sizes():
    respx.get(f"{EP}/api/models/o/v/tree/main").mock(
        return_value=httpx.Response(
            200,
            json=[
                tree_entry("config.json", 10),
                tree_entry("model-00001-of-00002.safetensors", 1000),
                tree_entry("model-00002-of-00002.safetensors", 500),
                tree_entry("tokenizer.json", 5),
            ],
        )
    )
    respx.get(f"{EP}/o/v/resolve/main/config.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "model_type": "qwen3",
                "num_hidden_layers": 28,
                "num_attention_heads": 16,
                "num_key_value_heads": 8,
                "hidden_size": 2048,
                "max_position_embeddings": 40960,
            },
        )
    )
    info = await HfClient(EP, None).vllm_info("o/v")
    assert info == VllmInfo(
        size_bytes=1500,
        meta=GgufMeta(arch="qwen3", n_layers=28, n_heads=16, n_kv_heads=8, head_dim=128, embedding=2048, max_ctx=40960),
    )


@respx.mock
async def test_vllm_info_nested_text_config_and_explicit_head_dim():
    respx.get(f"{EP}/api/models/o/v/tree/main").mock(
        return_value=httpx.Response(200, json=[tree_entry("m.safetensors", 700)])
    )
    cfg = {
        "model_type": "gemma3",
        "max_position_embeddings": 131072,
        "text_config": {
            "num_hidden_layers": 34,
            "num_attention_heads": 8,
            "num_key_value_heads": 4,
            "head_dim": 256,
            "hidden_size": 2560,
        },
    }
    respx.get(f"{EP}/o/v/resolve/main/config.json").mock(return_value=httpx.Response(200, content=json.dumps(cfg)))
    info = await HfClient(EP, None).vllm_info("o/v")
    assert info.size_bytes == 700
    assert (info.meta.n_layers, info.meta.n_kv_heads, info.meta.head_dim, info.meta.max_ctx) == (34, 4, 256, 131072)


# ---------- estimator ----------

# Gemma 4 E4B (unsloth/gemma-4-E4B-it-GGUF, gemma-4-E4B-it-Q4_K_M.gguf): the keys of its header and the bytes of its
# tensor table (layers, output incl. the tied copy of token_embd, input embeddings token_embd + per_layer_token_embd)
E4B_PATTERN = [i % 6 != 5 for i in range(42)]  # 5 sliding-window layers, then one global layer
E4B_KVS = {
    **arch_kvs("gemma4", n_layers=42, n_heads=8, n_kv_heads=2, embedding=2560, max_ctx=131072, key_length=512),
    "gemma4.attention.value_length": 512,
    "gemma4.attention.sliding_window": 512,
    "gemma4.attention.shared_kv_layers": 18,
    "gemma4.attention.sliding_window_pattern": E4B_PATTERN,
    "gemma4.attention.key_length_swa": 256,
    "gemma4.attention.value_length_swa": 256,
}
E4B_FILE_BYTES = 4977171584
E4B_LAYERS_BYTES, E4B_OUTPUT_BYTES, E4B_EMBD_BYTES = 2591026496, 432549888, 2315255808


def test_sliding_window_and_shared_kv_keys_are_read():
    meta = parse_gguf_header(build_gguf(E4B_KVS))
    assert (meta.n_swa, meta.head_dim_swa, meta.value_dim_swa, meta.kv_layers) == (512, 256, 256, 24)
    assert meta.swa_layers == tuple(E4B_PATTERN)
    # Gemma 3: no pattern in the file, llama.cpp's period 6 (src/models/gemma3.cpp, load_swa_pattern(ml, 6))
    g3 = parse_gguf_header(build_gguf({**arch_kvs("gemma3", n_layers=12), "gemma3.attention.sliding_window": 1024}))
    assert g3.n_swa == 1024 and g3.swa_layers == tuple(i % 6 != 5 for i in range(12)) and g3.kv_layers is None
    assert parse_gguf_header(build_gguf(arch_kvs("gemma3", n_layers=12))).swa_layers == ()  # no window: all global
    # Gemma 2: window 4096 by default, every other layer (src/models/gemma2.cpp)
    g2 = parse_gguf_header(build_gguf(arch_kvs("gemma2", n_layers=4)))
    assert (g2.n_swa, g2.swa_layers) == (4096, (True, False, True, False))
    # an architecture llama.cpp gives no sliding-window cache: a window key in the file changes nothing
    other = parse_gguf_header(build_gguf({**arch_kvs("qwen2", n_layers=4), "qwen2.attention.sliding_window": 4096}))
    assert (other.n_swa, other.swa_layers) == (0, ())


def test_kv_of_sliding_window_and_shared_layers_follows_llama_cpp():
    """b12033 (src/llama-kv-cache-iswa.cpp): global layers cache ctx_per_request cells per slot, sliding-window layers
    GGML_PAD(min(ctx_per_request, n_swa + n_ubatch), 256) per slot (serve-llama.sh runs --no-kv-unified, n_ubatch 512);
    Gemma 4 layers past n_layer - shared_kv_layers reuse the KV of earlier layers and cache nothing (src/models/gemma4.cpp)."""
    meta = parse_gguf_header(build_gguf(E4B_KVS))
    full = (512 + 512) * 2 * 2  # K + V of 2 heads, f16, per cell
    swa = (256 + 256) * 2 * 2
    assert est.kv_bytes(meta, 131072) == 4 * full * 131072 + 20 * swa * 1024
    assert est.kv_bytes(meta, 16384, parallel=2) == 4 * full * 16384 + 20 * swa * 1024 * 2
    assert est.kv_bytes(meta, 512) == 4 * full * 512 + 20 * swa * 512  # a context below the window caps it
    old = est.meta_from_dict({k: v for k, v in est.meta_to_dict(meta).items() if k not in est.SWA_KEYS})
    assert est.kv_bytes(old, 131072) == 42 * full * 131072  # a stored meta from before: every layer in full


def test_gemma4_e4b_estimate_matches_the_rtx_4070_measurement():
    """Measured on the RTX 4070 Laptop (8 GB), b12033, -fa auto: n_ctx 131072 x 1 -> 5.4 GB VRAM, 8192 x 2 -> 3.6 GB.
    On a GPU llama.cpp keeps the input embeddings (token_embd, per_layer_token_embd: 2.3 GB) in host RAM, so the device
    estimate is the layers, the output layer, the KV cache and the overhead; it must be within 25 % of the measurement."""
    meta = parse_gguf_header(build_gguf(E4B_KVS))
    device = E4B_LAYERS_BYTES + E4B_OUTPUT_BYTES
    for ctx, parallel, measured in ((131072, 1, 5.4e9), (8192, 2, 3.6e9)):
        e = est.estimate(E4B_FILE_BYTES, meta, ctx, parallel, device_weights_bytes=device)
        assert abs(e.device_bytes - measured) <= 0.25 * measured, (ctx, e)
        assert e.host_bytes == E4B_FILE_BYTES - device  # what stays in RAM on a GPU node
    e = est.estimate(E4B_FILE_BYTES, meta, 131072, 1)
    assert e.kv_bytes < 2.3 * GiB and e.device_bytes is None  # was 22.5 GB with every layer at full context
    stored = {"weights_bytes": E4B_FILE_BYTES, "layers": [E4B_LAYERS_BYTES // 42] * 42, "output_bytes": E4B_OUTPUT_BYTES,
              "nextn_layers": 0, "n_layers": 42}
    assert est.device_weights_bytes(stored) == E4B_LAYERS_BYTES // 42 * 42 + E4B_OUTPUT_BYTES
    assert est.device_weights_bytes({"weights_bytes": 1, "n_layers": 2}) is None  # no layer layout


def test_meta_round_trips_through_the_stored_dict():
    meta = parse_gguf_header(build_gguf(E4B_KVS))
    assert est.meta_from_dict(est.meta_to_dict(meta)) == meta


def test_kv_and_estimate_math():
    meta = GgufMeta("llama", n_layers=32, n_heads=32, n_kv_heads=8, head_dim=128, embedding=4096, max_ctx=131072)
    assert est.kv_bytes(meta, 16384) == 2147483648 == 2 * 32 * 8 * 128 * 16384 * 2
    assert est.kv_bytes(meta, 16384, bytes_per_elem=1) == 2147483648 // 2
    e = est.estimate(5 * GiB, meta, ctx_per_request=8192, parallel=2)
    assert e.kv_bytes == 2147483648
    assert e.weights_bytes == 5 * GiB
    assert e.total_bytes == 5 * GiB + 2147483648
    assert e.per_node_bytes == e.total_bytes + est.OVERHEAD
    e3 = est.estimate(5 * GiB, meta, 8192, 2, group_size=3)
    assert e3.per_node_bytes == -(-e3.total_bytes // 3) + est.OVERHEAD
    assert est.OVERHEAD == 512 * MiB


def test_capacity_rules():
    # a fixed reserve for the CUDA context, compute buffers and a desktop sharing the GPU
    assert est.node_capacity_bytes("nvidia", 12288, None) == 12288 * MiB - 1536 * MiB
    assert est.node_capacity_bytes("nvidia", 12288, 65536) == 12288 * MiB - 1536 * MiB
    assert est.node_capacity_bytes("nvidia", 1024, None) == 0
    assert est.node_capacity_bytes("cpu", None, 32768) == 32768 * MiB * 8 // 10 - GiB
    assert est.node_capacity_bytes("nvidia", None, 32768) is None
    assert est.node_capacity_bytes("cpu", 8192, None) is None
    assert est.node_capacity_bytes("weird", 1, 1) is None
    assert est.node_capacity_bytes("cpu", None, 512) == 0


def test_recommend_group_size():
    cap = est.node_capacity_bytes("nvidia", 12288, None)
    assert est.recommend_group_size(int(17.6 * GiB), [cap] * 4) == 2
    assert est.recommend_group_size(GiB, [cap] * 4) == 1
    assert est.recommend_group_size(100 * GiB, [cap] * 4) is None
    assert est.recommend_group_size(GiB, []) is None
    # unsorted input: sorted descending, the G largest nodes must each fit their share
    big, small = 10 * GiB, 2 * GiB
    assert est.recommend_group_size(15 * GiB, [small, big, big]) == 2
    assert est.recommend_group_size(15 * GiB, [small, small, big]) is None


# ---------- fake ----------


def _public_methods(cls):
    return {n: f for n, f in inspect.getmembers(cls, inspect.isfunction) if not n.startswith("_")}


def test_fake_hf_matches_client_interface():
    real, fake = _public_methods(HfClient), _public_methods(FakeHf)
    assert set(real) <= set(fake)
    for name, f in real.items():
        assert list(inspect.signature(f).parameters) == list(inspect.signature(fake[name]).parameters), name
        assert inspect.iscoroutinefunction(fake[name]), name


async def test_fake_hf_serves_fixtures_and_errors():
    meta = GgufMeta("llama", 32, 32, 8, 128, 4096, 8192)
    fake = FakeHf(
        files={"o/r": [HfFile("m-Q4_K_M.gguf", 100), HfFile("mmproj-F16.gguf", 1), HfFile("README.md", 2)]},
        metas={("o/r", "m-Q4_K_M.gguf"): meta},
        vllm={"o/v": VllmInfo(10, meta)},
    )
    assert await fake.gguf_files("o/r") == [GgufChoice("m-Q4_K_M.gguf", 100, "Q4_K_M", ["m-Q4_K_M.gguf"], [100])]
    assert await fake.gguf_meta("o/r", "m-Q4_K_M.gguf") == meta
    assert (await fake.vllm_info("o/v")).size_bytes == 10
    with pytest.raises(HfNotFound):
        await fake.list_files("x/y")
    with pytest.raises(HfNotFound):
        await fake.gguf_meta("o/r", "other.gguf")
    fake.unavailable = True
    with pytest.raises(HfUnavailable):
        await fake.list_files("o/r")


# ---------- full header: tensor table and layer layout (split placement) ----------


def _model_tensors(n_blocks: int, block: int, *, tied: bool = False, start: int = 0):
    """(name, shape, offset) of a toy model whose tensors are laid out back to back: token_embd, blocks, output."""
    entries, offset = [], start
    sizes = [("token_embd.weight", (64, 1000), 4000)]
    for i in range(n_blocks):
        sizes += [(f"blk.{i}.attn_q.weight", (64, 64), block // 4), (f"blk.{i}.ffn_up.weight", (64, 256), block - block // 4)]
    sizes += [("output_norm.weight", (64,), 256)]
    if not tied:
        sizes += [("output.weight", (64, 1000), 5000)]
    for name, shape, size in sizes:
        entries.append((name, shape, offset))
        offset += size
    return entries, offset - start


def test_parse_full_header_reads_the_tensor_table_after_the_tokenizer():
    kvs = {**arch_kvs("llama", n_layers=2), "tokenizer.ggml.tokens": ["tok"] * 5000, "general.alignment": 64}
    tensors, _ = _model_tensors(2, 1000)
    data = build_gguf(kvs, tensors=tensors, alignment=64)
    header = parse_gguf(data)
    assert header.meta.arch == "llama" and header.meta.n_layers == 2
    assert header.data_offset == len(data) and header.data_offset % 64 == 0
    assert [t.name for t in header.tensors][:2] == ["token_embd.weight", "blk.0.attn_q.weight"]
    assert header.tensors[0].shape == (64, 1000) and header.tensors[1].offset == 4000
    with pytest.raises(GgufTruncated) as exc:
        parse_gguf(data[: len(data) - 40])
    assert exc.value.needed > len(data) - 40


def test_parse_full_header_of_a_later_shard_without_architecture_keys():
    data = build_gguf({"split.no": 1, "split.count": 2}, tensors=[("blk.1.ffn_up.weight", (4, 4), 0)])
    header = parse_gguf(data)
    assert header.meta is None and [t.name for t in header.tensors] == ["blk.1.ffn_up.weight"]


def test_meta_knows_mla_value_length_and_mtp_layers():
    kvs = {
        **arch_kvs("deepseek2", n_layers=61, n_heads=128, n_kv_heads=1, embedding=7168, key_length=576),
        "deepseek2.attention.value_length": 512,
        "deepseek2.attention.key_length_mla": 192,
        "deepseek2.attention.value_length_mla": 128,
    }
    meta = parse_gguf_header(build_gguf(kvs))
    assert (meta.head_dim, meta.value_dim, meta.mla, meta.nextn_layers) == (576, 512, True, 0)
    glm = {**arch_kvs("glm4moe", n_layers=93, n_heads=96, n_kv_heads=8, embedding=5120, key_length=128),
           "glm4moe.nextn_predict_layers": 1}
    meta = parse_gguf_header(build_gguf(glm))
    assert (meta.mla, meta.nextn_layers, meta.value_dim) == (False, 1, None)


def test_layout_sums_blocks_output_and_embeddings_across_shards():
    first, size1 = _model_tensors(2, 1000)
    first = first[:3]  # token_embd + blk.0 in shard 1
    size1 = 4000 + 1000
    second = [("blk.1.attn_q.weight", (64, 64), 0), ("blk.1.ffn_up.weight", (64, 256), 250),
              ("output_norm.weight", (64,), 1000), ("output.weight", (64, 1000), 1256)]
    h1 = parse_gguf(build_gguf(arch_kvs("llama", n_layers=2), tensors=first))
    h2 = parse_gguf(build_gguf({"split.no": 1}, tensors=second))
    layout = gguf_layout([(h1, h1.data_offset + size1), (h2, h2.data_offset + 1256 + 5000)], n_blocks=2)
    assert layout == GgufLayout(layers=[1000, 1000], output_bytes=256 + 5000, embd_bytes=4000, max_tensor_bytes=5000, n_vocab=1000)


def test_layout_of_tied_embeddings_loads_a_copy_on_the_output_device():
    tensors, total = _model_tensors(2, 1000, tied=True)
    h = parse_gguf(build_gguf(arch_kvs("gemma3", n_layers=2), tensors=tensors))
    layout = gguf_layout([(h, h.data_offset + total)], n_blocks=2)
    assert layout.output_bytes == 256 + 4000 and layout.embd_bytes == 4000


def test_layout_rejects_a_file_shorter_than_its_tensor_table():
    tensors, total = _model_tensors(2, 1000)
    h = parse_gguf(build_gguf(arch_kvs("llama", n_layers=2), tensors=tensors))
    with pytest.raises(GgufInvalid):
        gguf_layout([(h, h.data_offset + 10)], n_blocks=2)


@respx.mock
async def test_gguf_inspect_reads_every_shard_and_continues_where_the_last_range_ended():
    tokens = ["tokentoken"] * 300_000  # ~5.4 MB: the first 2 MiB window ends inside the tokenizer
    t1 = [("token_embd.weight", (64, 1000), 0), ("blk.0.ffn_up.weight", (64, 64), 4000)]
    t2 = [("blk.1.ffn_up.weight", (64, 64), 0), ("output.weight", (64, 1000), 1000)]
    d1 = build_gguf({**arch_kvs("llama", n_layers=2), "tokenizer.ggml.tokens": tokens}, tensors=t1)
    d2 = build_gguf({"split.no": 1}, tensors=t2)
    calls: list[str] = []
    respx.get(f"{EP}/o/r/resolve/main/m-00001-of-00002.gguf").mock(side_effect=_ranged(d1 + b"\0" * 5000, calls))
    respx.get(f"{EP}/o/r/resolve/main/m-00002-of-00002.gguf").mock(side_effect=_ranged(d2 + b"\0" * 6000, []))
    shards = [HfFile("m-00001-of-00002.gguf", len(d1) + 5000), HfFile("m-00002-of-00002.gguf", len(d2) + 6000)]
    meta, layout = await HfClient(EP, None).gguf_inspect("o/r", shards)
    assert meta.arch == "llama" and meta.n_layers == 2
    assert layout.layers == [1000, 1000] and layout.embd_bytes == 4000 and layout.output_bytes == 5000
    starts = [int(c.removeprefix("bytes=").split("-")[0]) for c in calls]
    assert starts[0] == 0 and len(calls) >= 2
    assert all(b > a for a, b in zip(starts, starts[1:], strict=False))  # nothing is downloaded twice


@respx.mock
async def test_gguf_inspect_when_range_is_ignored_skips_what_it_already_has():
    tokens = ["tokentoken"] * 300_000
    data = build_gguf({**arch_kvs("llama", n_layers=1), "tokenizer.ggml.tokens": tokens},
                      tensors=[("blk.0.ffn_up.weight", (4, 4), 0), ("output.weight", (4, 4), 100)])
    respx.get(f"{EP}/o/r/resolve/main/m.gguf").mock(return_value=httpx.Response(200, content=data + b"\0" * 300))
    meta, layout = await HfClient(EP, None).gguf_inspect("o/r", [HfFile("m.gguf", len(data) + 300)])
    assert layout.layers == [100] and layout.output_bytes == 200


def test_kv_counts_mla_once_value_length_apart_and_skips_mtp_layers():
    plain = GgufMeta("llama", 32, 32, 8, 128, 4096, 8192)
    assert est.kv_bytes(plain, 1000) == 32 * (128 + 128) * 8 * 1000 * 2
    mla = GgufMeta("deepseek2", 61, 128, 1, 576, 7168, 163840, value_dim=512, mla=True)
    assert est.kv_bytes(mla, 1000) == 61 * 576 * 1 * 1000 * 2  # K only: the compressed latent plus rope
    split_kv = GgufMeta("deepseek2", 61, 128, 128, 192, 7168, 163840, value_dim=128)
    assert est.kv_bytes(split_kv, 1000) == 61 * (192 + 128) * 128 * 1000 * 2
    glm = GgufMeta("glm4moe", 93, 96, 8, 128, 5120, 131072, nextn_layers=1)
    assert est.kv_bytes(glm, 1000) == 92 * 256 * 8 * 1000 * 2


# ---------- inspector through the mirror on main, Hugging Face directly when the mirror is down ----------


async def test_mirror_first_client_falls_back_to_hugging_face_when_the_mirror_is_down(caplog):
    from app.catalog.hf import MirrorFirstHf

    def mirror_down(request):
        raise httpx.ConnectError("refused", request=request)

    def hub(request):
        return httpx.Response(200, json=[{"type": "file", "path": "m-Q4_K_M.gguf", "size": 7}])

    client = MirrorFirstHf(
        HfClient("http://127.0.0.1:8090", "hf_mirror", transport=httpx.MockTransport(mirror_down)),
        HfClient("https://huggingface.co", "hf_real", transport=httpx.MockTransport(hub)),
    )
    with caplog.at_level("WARNING"):
        assert await client.list_files("o/r") == [HfFile("m-Q4_K_M.gguf", 7)]
    assert "mirror" in caplog.text and "huggingface.co" in caplog.text
    assert [c.file for c in await client.gguf_files("o/r")] == ["m-Q4_K_M.gguf"]


async def test_mirror_first_client_keeps_a_not_found_answer_of_the_mirror():
    from app.catalog.hf import MirrorFirstHf

    asked = []

    def hub(request):
        asked.append(request)
        return httpx.Response(200, json=[])

    client = MirrorFirstHf(
        HfClient("http://127.0.0.1:8090", None, transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        HfClient("https://huggingface.co", None, transport=httpx.MockTransport(hub)),
    )
    with pytest.raises(HfNotFound):
        await client.list_files("o/missing")
    assert asked == []  # the mirror asked Hugging Face already: the repository does not exist
