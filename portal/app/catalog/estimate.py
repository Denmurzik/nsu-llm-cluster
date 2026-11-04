"""Memory estimator for model weights plus KV cache, and group-size recommendation."""

import math
from dataclasses import dataclass
from typing import Any

from app.catalog.gguf import GgufMeta

MiB = 2**20
GiB = 2**30
OVERHEAD = 512 * MiB
# VRAM a node never gives to the model: CUDA context, compute buffers and a desktop on the same GPU (Windows PCs)
NVIDIA_RESERVE = 1536 * MiB


# llama-server's micro-batch (n_ubatch 512): a sliding-window cache keeps n_swa + n_ubatch cells per slot
N_UBATCH = 512
SWA_KEYS = ("n_swa", "swa_layers", "head_dim_swa", "value_dim_swa", "kv_layers")


# bytes per KV element of the cache types (--cache-type-k/-v): a ggml block holds 32 values, q8_0 in 34 bytes (an f16
# scale and 32 int8), q4_0 in 18 bytes (an f16 scale and 16 bytes of nibbles)
KV_BYTES_PER_ELEM: dict[str, float] = {"f16": 2.0, "q8_0": 34 / 32, "q4_0": 18 / 32}


def kv_elem_bytes(kv_cache_type: str | None) -> float:
    return KV_BYTES_PER_ELEM.get(kv_cache_type or "f16", 2.0)


def kv_layer_bytes(meta: GgufMeta, ctx_total: int, bytes_per_elem: float = 2) -> int:
    """KV cache of one global (non-MTP) layer: K and V of `n_kv_heads` heads; an MLA model caches K only."""
    v_dim = 0 if meta.mla else (meta.value_dim or meta.head_dim)
    return math.ceil((meta.head_dim + v_dim) * meta.n_kv_heads * ctx_total * bytes_per_elem)


def cached_layers(meta: GgufMeta) -> int:
    """Layers with a KV cache: block_count without the MTP layers (GLM-4.5), which are not loaded by default."""
    return max(meta.n_layers - meta.nextn_layers, 0)


def swa_cells(meta: GgufMeta, ctx_per_request: int) -> int:
    """Cells of a sliding-window layer per slot, as b12033 sizes them (src/llama-kv-cache-iswa.cpp):
    GGML_PAD(min(n_ctx_seq, n_swa + n_ubatch), 256); serve-llama.sh runs --no-kv-unified, so one stream per slot."""
    return -(-min(ctx_per_request, meta.n_swa + N_UBATCH) // 256) * 256


def kv_layers_bytes(meta: GgufMeta, ctx_total: int, parallel: int = 1, bytes_per_elem: float = 2) -> list[int]:
    """KV cache of every block: a global layer holds `ctx_total` cells, a sliding-window layer its window per slot
    (`parallel` slots of ctx_total / parallel), a layer that reuses an earlier layer's KV (Gemma 3n/4,
    `shared_kv_layers`) and an MTP layer none. Without sliding-window data every cached layer is global."""
    n_main = cached_layers(meta)
    owners = n_main if meta.kv_layers is None else min(meta.kv_layers, n_main)
    full = kv_layer_bytes(meta, ctx_total, bytes_per_elem)
    swa = 0
    if meta.swa_layers:
        v_swa = 0 if meta.mla else (meta.value_dim_swa or meta.head_dim_swa or meta.head_dim)
        per_cell = ((meta.head_dim_swa or meta.head_dim) + v_swa) * meta.n_kv_heads * bytes_per_elem
        swa = math.ceil(per_cell * swa_cells(meta, max(ctx_total // max(parallel, 1), 1)) * max(parallel, 1))
    sliding = meta.swa_layers
    return [
        0 if i >= owners else (swa if i < len(sliding) and sliding[i] else full)
        for i in range(meta.n_layers)
    ]


def kv_bytes(meta: GgufMeta, ctx_total: int, bytes_per_elem: float = 2, parallel: int = 1) -> int:
    return sum(kv_layers_bytes(meta, ctx_total, parallel, bytes_per_elem))


def meta_to_dict(meta: GgufMeta) -> dict[str, Any]:
    """The model fields of a stored deployment `meta`."""
    return {
        "arch": meta.arch,
        "n_layers": meta.n_layers,
        "n_heads": meta.n_heads,
        "n_kv_heads": meta.n_kv_heads,
        "head_dim": meta.head_dim,
        "max_ctx": meta.max_ctx,
        "embedding": meta.embedding,
        "value_dim": meta.value_dim,
        "mla": meta.mla,
        "nextn_layers": meta.nextn_layers,
        # only for a model with a sliding window or shared KV layers: the stored meta of the others stays as it was
        **(
            {
                "n_swa": meta.n_swa,
                "swa_layers": list(meta.swa_layers),
                "head_dim_swa": meta.head_dim_swa,
                "value_dim_swa": meta.value_dim_swa,
                "kv_layers": meta.kv_layers,
            }
            if meta.swa_layers or meta.kv_layers is not None
            else {}
        ),
    }


def meta_from_dict(meta: dict[str, Any]) -> GgufMeta:
    """GgufMeta of a stored deployment `meta` (keys added later default to the old behaviour: every layer global)."""
    return GgufMeta(
        meta["arch"], meta["n_layers"], meta["n_heads"], meta["n_kv_heads"], meta["head_dim"],
        meta.get("embedding") or meta["n_heads"] * meta["head_dim"], meta["max_ctx"],
        meta.get("value_dim"), bool(meta.get("mla", False)), int(meta.get("nextn_layers") or 0),
        int(meta.get("n_swa") or 0), tuple(meta.get("swa_layers") or ()), meta.get("head_dim_swa"),
        meta.get("value_dim_swa"), meta.get("kv_layers"),
    )


def device_weights_bytes(meta: dict[str, Any]) -> int | None:
    """Weights a GPU holds for a stored meta with a layer layout: the loaded blocks and the output layer (with the
    copy of a tied token_embd). The input embeddings (token_embd, per_layer_token_embd) stay in host RAM, llama.cpp
    keeps the input layer on the CPU. None without the layout."""
    layers = meta.get("layers")
    if not layers:
        return None
    nextn = int(meta.get("nextn_layers") or 0)
    return sum(layers[: len(layers) - nextn]) + int(meta.get("output_bytes") or 0)


def weights_bytes(meta: dict[str, Any]) -> int:
    """Bytes llama.cpp loads: the files without the MTP layers (exact with the layer layout, else proportional)."""
    total = meta["weights_bytes"]
    nextn = int(meta.get("nextn_layers") or 0)
    if not nextn:
        return total
    layers = meta.get("layers")
    if layers:
        return total - sum(layers[len(layers) - nextn :])
    return total - total * nextn // max(meta["n_layers"], 1)


@dataclass
class Estimate:
    weights_bytes: int
    kv_bytes: int
    total_bytes: int
    per_node_bytes: int
    # on a GPU node: what the GPU holds (weights without the input embeddings, KV, overhead) and what stays in RAM;
    # None without a layer layout
    device_bytes: int | None = None
    host_bytes: int | None = None
    # parts besides the model (task 2-22b), all included in total_bytes: the draft model's weights and its own KV
    # cache (f16, at the same context and slots) and the image projector. In split mode the leader's node holds them.
    draft_weights_bytes: int = 0
    draft_kv_bytes: int = 0
    mmproj_bytes: int = 0

    @property
    def extras_bytes(self) -> int:
        return self.draft_weights_bytes + self.draft_kv_bytes + self.mmproj_bytes


def extra_parts(
    draft_weights: int = 0, draft_meta: GgufMeta | None = None, ctx_total: int = 0, parallel: int = 1, mmproj: int = 0
) -> tuple[int, int, int]:
    """(draft weights, draft KV, mmproj) of the draft model and the projector. The draft keeps an f16 cache: the
    portal passes no `--cache-type-k-draft`, so `--cache-type-k` does not reach it."""
    draft_kv = kv_bytes(draft_meta, ctx_total, parallel=parallel) if draft_meta is not None and draft_weights else 0
    return draft_weights, draft_kv, mmproj


def extras_key(draft_repo: str | None, draft_file: str | None, mmproj_file: str | None) -> str:
    return f"{draft_repo or ''}|{draft_file or ''}|{mmproj_file or ''}"


def stored_extras(draft: dict[str, Any] | None, mmproj_bytes: int, key: str) -> dict[str, Any]:
    """What a deployment's `meta["extras"]` keeps: the draft model's meta (the same dict as `meta`, sizes included) and
    the projector's size, read when the draft or projector was chosen; `key` says which choice they belong to."""
    return {"key": key, "draft": draft, "mmproj_bytes": mmproj_bytes}


def dep_extras(dep) -> tuple[int, int, int]:
    """(draft weights, draft KV, mmproj) of a stored deployment, zero without `meta["extras"]` (nothing chosen, not
    read yet, or Hugging Face was unreachable then)."""
    stored = (dep.meta or {}).get("extras") if dep.meta else None
    if not stored or stored.get("key") != extras_key(
        getattr(dep, "draft_repo", None), getattr(dep, "draft_file", None), getattr(dep, "mmproj_file", None)
    ):
        return 0, 0, 0
    draft = stored.get("draft")
    draft_meta = meta_from_dict(draft) if draft else None
    return extra_parts(
        int(draft["weights_bytes"]) if draft else 0, draft_meta, dep.ctx_per_request * dep.parallel, dep.parallel,
        int(stored.get("mmproj_bytes") or 0),
    )


def estimate(
    weights_bytes: int,
    meta: GgufMeta,
    ctx_per_request: int,
    parallel: int,
    group_size: int = 1,
    device_weights_bytes: int | None = None,
    kv_cache_type: str | None = "f16",
    draft_weights_bytes: int = 0,
    draft_meta: GgufMeta | None = None,
    mmproj_bytes: int = 0,
) -> Estimate:
    ctx_total = ctx_per_request * parallel
    kv = kv_bytes(meta, ctx_total, kv_elem_bytes(kv_cache_type), parallel=parallel)
    d_weights, d_kv, mm = extra_parts(draft_weights_bytes, draft_meta, ctx_total, parallel, mmproj_bytes)
    extra = d_weights + d_kv + mm
    total = weights_bytes + kv + extra
    device = host = None
    if device_weights_bytes is not None:
        device = math.ceil((device_weights_bytes + kv) / group_size) + extra + OVERHEAD
        host = max(weights_bytes - device_weights_bytes, 0)
    per_node = math.ceil((weights_bytes + kv) / group_size) + extra + OVERHEAD
    return Estimate(weights_bytes, kv, total, per_node, device, host, d_weights, d_kv, mm)


def node_capacity_bytes(accel: str, vram_mib: int | None, mem_mib: int | None) -> int | None:
    if accel == "nvidia":
        return None if vram_mib is None else max(0, vram_mib * MiB - NVIDIA_RESERVE)
    if accel == "cpu":
        return None if mem_mib is None else max(0, mem_mib * MiB * 8 // 10 - GiB)
    return None


def recommend_group_size(total_bytes: int, capacities: list[int]) -> int | None:
    """Smallest G such that the G largest nodes each fit their 1/G share, or None."""
    caps = sorted(capacities, reverse=True)
    for g in range(1, len(caps) + 1):
        if math.ceil(total_bytes / g) + OVERHEAD <= caps[g - 1]:
            return g
    return None
