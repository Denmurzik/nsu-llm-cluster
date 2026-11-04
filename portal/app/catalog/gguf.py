"""GGUF header parser: the metadata needed for memory estimation and, from the full header, the tensor table that
gives the bytes of every layer (split placement)."""

import re
import struct
from dataclasses import dataclass
from typing import Any

MAGIC = b"GGUF"

T_STRING = 8
T_ARRAY = 9
# GGUF value type id -> struct format (little-endian)
_SCALARS = {
    0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d",
}
_MAX_STORED_ARRAY = 4096
DEFAULT_ALIGNMENT = 32
_BLOCK_RE = re.compile(r"^blk\.(\d+)\.")
# tensors of the input layer: llama.cpp keeps them on the CPU of the process that reads the model (the split leader)
_INPUT_PREFIXES = ("token_embd.", "per_layer_token_embd.", "position_embd.", "token_types.")


class GgufInvalid(ValueError):
    """The data is not a supported GGUF file."""


class GgufTruncated(Exception):
    """The data ended before the required keys were parsed; `needed` is a lower bound of the bytes required."""

    def __init__(self, needed: int):
        super().__init__(f"GGUF header truncated, at least {needed} bytes are needed")
        self.needed = needed


@dataclass
class GgufMeta:
    arch: str
    n_layers: int
    n_heads: int
    n_kv_heads: int
    head_dim: int
    embedding: int
    max_ctx: int
    value_dim: int | None = None  # attention.value_length; None: same as head_dim (key_length)
    mla: bool = False  # MLA (DeepSeek V3/R1, Kimi K2): the KV cache holds K only
    nextn_layers: int = 0  # MTP layers counted in block_count: neither loaded nor cached by default
    # sliding-window attention as llama.cpp b12033 runs it (llama_kv_cache_iswa): the window, which layers use it
    # (empty: none), their K/V head sizes, and how many layers from the start own a KV cache (Gemma 3n/4 share the rest)
    n_swa: int = 0
    swa_layers: tuple[bool, ...] = ()
    head_dim_swa: int | None = None
    value_dim_swa: int | None = None
    kv_layers: int | None = None


@dataclass(frozen=True)
class GgufTensor:
    name: str
    shape: tuple[int, ...]  # ggml order: ne[0] first
    offset: int  # relative to the start of the data section


@dataclass
class GgufHeader:
    """A fully parsed header. `meta` is None for a later shard of a split model (it carries no architecture keys)."""

    meta: GgufMeta | None
    tensors: list[GgufTensor]
    data_offset: int  # absolute file offset of the tensor data


@dataclass
class GgufLayout:
    """Bytes of the model by layer, as llama.cpp places them."""

    layers: list[int]  # tensors `blk.<i>.*` for every block index of block_count (MTP layers included)
    output_bytes: int  # the output layer: output norm and head, or a copy of token_embd when the head is tied
    embd_bytes: int  # input layer (token_embd): stays on the CPU of the split leader
    max_tensor_bytes: int
    n_vocab: int


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> memoryview:
        end = self.pos + n
        if end > len(self.data):
            raise GgufTruncated(end)
        view = memoryview(self.data)[self.pos : end]
        self.pos = end
        return view

    def unpack(self, fmt: str) -> Any:
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def string(self) -> str:
        n = self.unpack("<Q")
        return bytes(self.take(n)).decode("utf-8", errors="replace")

    def value(self, type_id: int) -> Any:
        """Reads a value; large arrays are skipped (returned as None)."""
        if type_id in _SCALARS:
            return self.unpack(_SCALARS[type_id])
        if type_id == T_STRING:
            return self.string()
        if type_id == T_ARRAY:
            elem = self.unpack("<I")
            count = self.unpack("<Q")
            if elem in _SCALARS:
                size = struct.calcsize(_SCALARS[elem])
                raw = self.take(count * size)
                if count > _MAX_STORED_ARRAY:
                    return None
                return list(struct.unpack(f"<{count}{_SCALARS[elem][1]}", raw))
            if elem == T_STRING and count > _MAX_STORED_ARRAY:
                for _ in range(count):  # a tokenizer vocabulary: stepped over without decoding
                    self.take(self.unpack("<Q"))
                return None
            items = [self.value(elem) for _ in range(count)]
            return items if count <= _MAX_STORED_ARRAY else None
        raise GgufInvalid(f"unknown GGUF value type {type_id}")


def _int(value: Any) -> int | None:
    """Scalar int, or the maximum of a per-layer array."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, list) and value and all(isinstance(v, int) for v in value):
        return max(value)
    return None


# Architectures llama.cpp b12033 runs with a sliding-window KV cache, with the period of global layers it assumes when
# the file has no `sliding_window_pattern` array (src/models/<arch>.cpp, load_swa_pattern; None: the array is required)
SWA_PERIOD: dict[str, int | None] = {"gemma2": 2, "gemma3": 6, "gemma3n": 5, "cohere2": 4, "gemma4": None}
_SWA_DEFAULT_WINDOW = {"gemma2": 4096}  # gemma2.cpp: n_swa = 4096 unless the file says otherwise
# layers from the start with their own KV cache when the file does not say: gemma3n.cpp n_layer_kv_from_start = 20
_KV_LAYERS_FIXED = {"gemma3n": 20}


def _swa(arch: str, kv: dict[str, Any], n_layers: int) -> tuple[int, tuple[bool, ...]]:
    """(window, per-layer flags) the way llama.cpp reads them; (0, ()) for a model without a sliding-window cache.
    A flag array wins; else an integer period (`set_swa_pattern`: layer il slides unless il % n == n - 1)."""
    if arch not in SWA_PERIOD:
        return 0, ()
    window = _int(kv.get(f"{arch}.attention.sliding_window")) or _SWA_DEFAULT_WINDOW.get(arch, 0)
    if not window:
        return 0, ()  # gemma3 without the key: llama.cpp turns the sliding window off
    pattern = kv.get(f"{arch}.attention.sliding_window_pattern")
    if isinstance(pattern, list) and len(pattern) >= n_layers and all(isinstance(p, bool) for p in pattern):
        return window, tuple(pattern[:n_layers])
    period = pattern if isinstance(pattern, int) and not isinstance(pattern, bool) else SWA_PERIOD[arch]
    if period is None:
        return 0, ()  # not readable: every layer counted at full context (an overestimate, never less)
    return window, tuple(period == 0 or i % period < period - 1 for i in range(n_layers))


def _build(kv: dict[str, Any], *, need_kv_heads: bool = False) -> GgufMeta | None:
    """Builds the meta when the required keys are present, else None.

    `head_count_kv` falls back to `head_count` unless `need_kv_heads` (the data ended, it may still follow)."""
    arch = kv.get("general.architecture")
    if not isinstance(arch, str):
        return None
    n_layers = _int(kv.get(f"{arch}.block_count"))
    n_heads = _int(kv.get(f"{arch}.attention.head_count"))
    embedding = _int(kv.get(f"{arch}.embedding_length"))
    max_ctx = _int(kv.get(f"{arch}.context_length"))
    if None in (n_layers, n_heads, embedding, max_ctx) or not n_heads:
        return None
    n_kv_heads = _int(kv.get(f"{arch}.attention.head_count_kv"))
    if n_kv_heads is None and need_kv_heads:
        return None
    n_kv_heads = n_kv_heads or n_heads
    head_dim = _int(kv.get(f"{arch}.attention.key_length")) or embedding // n_heads
    value_dim = _int(kv.get(f"{arch}.attention.value_length"))
    # llama.cpp treats a model as MLA when both *_mla lengths are present (src/llama-hparams.cpp), then caches no V
    mla = f"{arch}.attention.key_length_mla" in kv and f"{arch}.attention.value_length_mla" in kv
    nextn = _int(kv.get(f"{arch}.nextn_predict_layers")) or 0
    n_swa, swa_layers = _swa(arch, kv, n_layers)
    head_dim_swa = value_dim_swa = None
    if swa_layers:
        head_dim_swa = _int(kv.get(f"{arch}.attention.key_length_swa")) or head_dim
        value_dim_swa = _int(kv.get(f"{arch}.attention.value_length_swa")) or value_dim or head_dim
    shared = _int(kv.get(f"{arch}.attention.shared_kv_layers"))
    kv_layers = n_layers - shared if shared else _KV_LAYERS_FIXED.get(arch)
    return GgufMeta(
        arch, n_layers, n_heads, n_kv_heads, head_dim, embedding, max_ctx, value_dim, mla, nextn,
        n_swa, swa_layers, head_dim_swa, value_dim_swa, kv_layers,
    )


def parse_gguf_header(data: bytes) -> GgufMeta:
    """Parses a GGUF v2/v3 header prefix.

    Returns as soon as all required keys are parsed and either a `tokenizer.*` key is reached (the huge
    tokenizer arrays follow the architecture keys) or the data ends; `GgufTruncated` is raised only when
    the data ends before a required key was seen.
    """
    r = _Reader(data)
    if bytes(r.take(4)) != MAGIC:
        raise GgufInvalid("not a GGUF file")
    version = r.unpack("<I")
    if version not in (2, 3):
        raise GgufInvalid(f"unsupported GGUF version {version}")
    r.unpack("<Q")  # tensor count
    kv_count = r.unpack("<Q")
    kv: dict[str, Any] = {}
    for _ in range(kv_count):
        try:
            key = r.string()
            if key.startswith("tokenizer."):
                meta = _build(kv)
                if meta is not None:
                    return meta
            type_id = r.unpack("<I")
            kv[key] = r.value(type_id)
        except GgufTruncated:
            # the data ends inside some later key: enough if the required keys were already parsed
            meta = _build(kv, need_kv_heads=True)
            if meta is not None:
                return meta
            raise
    meta = _build(kv)
    if meta is None:
        raise GgufInvalid("GGUF header lacks required architecture keys")
    return meta


def parse_gguf(data: bytes) -> GgufHeader:
    """Parses the whole header: every KV pair and the tensor table. `GgufTruncated` until the data covers both."""
    r = _Reader(data)
    if bytes(r.take(4)) != MAGIC:
        raise GgufInvalid("not a GGUF file")
    version = r.unpack("<I")
    if version not in (2, 3):
        raise GgufInvalid(f"unsupported GGUF version {version}")
    n_tensors = r.unpack("<Q")
    kv_count = r.unpack("<Q")
    kv: dict[str, Any] = {}
    for _ in range(kv_count):
        key = r.string()
        kv[key] = r.value(r.unpack("<I"))
    tensors = []
    for _ in range(n_tensors):
        name = r.string()
        n_dims = r.unpack("<I")
        shape = tuple(r.unpack("<Q") for _ in range(n_dims))
        r.unpack("<I")  # ggml type: the size comes from the offsets, so new quantization types need no table
        tensors.append(GgufTensor(name, shape, r.unpack("<Q")))
    alignment = _int(kv.get("general.alignment")) or DEFAULT_ALIGNMENT
    data_offset = -(-r.pos // alignment) * alignment
    meta = _build(kv)
    if meta is None and "general.architecture" in kv:
        raise GgufInvalid("GGUF header lacks required architecture keys")
    return GgufHeader(meta, tensors, data_offset)


def _tensor_sizes(header: GgufHeader, file_size: int) -> dict[str, int]:
    """Tensor data sizes from the offsets: each tensor runs to the next one, the last one to the end of the file
    (alignment padding included)."""
    ordered = sorted(header.tensors, key=lambda t: t.offset)
    end = file_size - header.data_offset
    sizes: dict[str, int] = {}
    for t, nxt in zip(ordered, [*ordered[1:], None], strict=True):
        stop = nxt.offset if nxt is not None else end
        if stop < t.offset:
            raise GgufInvalid(f"tensor {t.name} runs past the end of the file")
        sizes[t.name] = stop - t.offset
    return sizes


def gguf_layout(shards: list[tuple[GgufHeader, int]], n_blocks: int) -> GgufLayout:
    """Layer bytes over all shards, each given as (parsed header, file size)."""
    layers = [0] * n_blocks
    output = embd = biggest = n_vocab = 0
    head = tok_embd = None
    for header, file_size in shards:
        sizes = _tensor_sizes(header, file_size)
        for t in header.tensors:
            size = sizes[t.name]
            biggest = max(biggest, size)
            block = _BLOCK_RE.match(t.name)
            if block:
                index = int(block.group(1))
                if index >= len(layers):
                    layers += [0] * (index + 1 - len(layers))
                layers[index] += size
            elif t.name.startswith(_INPUT_PREFIXES):
                embd += size
                if t.name == "token_embd.weight":
                    tok_embd = size
                    n_vocab = t.shape[1] if len(t.shape) > 1 else n_vocab
            else:
                output += size
                if t.name == "output.weight":
                    head = size
    if head is None and tok_embd is not None:
        output += tok_embd  # tied head: llama.cpp loads token_embd a second time, on the output layer's device
    return GgufLayout(layers, output, embd, biggest, n_vocab)
