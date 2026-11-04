"""Builds GGUF header bytes (v2/v3, little-endian) from a dict of KV pairs, for tests.

Values: bool -> BOOL, int -> UINT32 (or UINT64 when too large), float -> FLOAT32,
str -> STRING, list -> ARRAY (element type from the first item; ints -> UINT32).
Use ``Typed(type_id, value)`` to force a specific GGUF value type.
"""

import struct
from dataclasses import dataclass
from typing import Any

UINT8, INT8, UINT16, INT16, UINT32, INT32, FLOAT32, BOOL, STRING, ARRAY, UINT64, INT64, FLOAT64 = range(13)

_FMT = {
    UINT8: "<B", INT8: "<b", UINT16: "<H", INT16: "<h", UINT32: "<I", INT32: "<i",
    FLOAT32: "<f", BOOL: "<?", UINT64: "<Q", INT64: "<q", FLOAT64: "<d",
}


@dataclass
class Typed:
    type_id: int
    value: Any


def _string(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _infer(value: Any) -> int:
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, int):
        return UINT32 if 0 <= value < 2**32 else UINT64
    if isinstance(value, float):
        return FLOAT32
    if isinstance(value, str):
        return STRING
    if isinstance(value, list):
        return ARRAY
    raise TypeError(f"unsupported value {value!r}")


def _encode(type_id: int, value: Any) -> bytes:
    if type_id == STRING:
        return _string(value)
    if type_id == ARRAY:
        items = value
        elem = _infer(items[0]) if items else UINT32
        return struct.pack("<IQ", elem, len(items)) + b"".join(_encode(elem, v) for v in items)
    return struct.pack(_FMT[type_id], value)


def kv_bytes(key: str, value: Any) -> bytes:
    if isinstance(value, Typed):
        type_id, value = value.type_id, value.value
    else:
        type_id = _infer(value)
    return _string(key) + struct.pack("<I", type_id) + _encode(type_id, value)


def tensor_info(name: str, shape: tuple[int, ...], offset: int, type_id: int = 0) -> bytes:
    """One tensor info entry: name, dims (ggml order, ne[0] first), ggml type, offset in the data section."""
    return _string(name) + struct.pack("<I", len(shape)) + b"".join(struct.pack("<Q", d) for d in shape) + struct.pack(
        "<IQ", type_id, offset
    )


def build_gguf(
    kvs: dict[str, Any],
    *,
    version: int = 3,
    tensor_count: int | None = None,
    magic: bytes = b"GGUF",
    tensors: list[tuple[str, tuple[int, ...], int]] = (),
    alignment: int = 32,
) -> bytes:
    """Header bytes; with `tensors` ((name, shape, offset) each) the tensor infos follow the KV pairs and the result is
    padded to `alignment`, so the data section starts right after it."""
    count = len(tensors) if tensor_count is None else tensor_count
    head = magic + struct.pack("<I", version) + struct.pack("<QQ", count, len(kvs))
    data = head + b"".join(kv_bytes(k, v) for k, v in kvs.items())
    if tensors:
        data += b"".join(tensor_info(name, shape, offset) for name, shape, offset in tensors)
        data += b"\0" * (-len(data) % alignment)
    return data


def arch_kvs(
    arch: str = "llama",
    *,
    n_layers: int = 32,
    n_heads: int = 32,
    n_kv_heads: Any = 8,
    embedding: int = 4096,
    max_ctx: int = 8192,
    key_length: int | None = None,
) -> dict[str, Any]:
    kvs: dict[str, Any] = {
        "general.architecture": arch,
        "general.name": "test",
        f"{arch}.context_length": max_ctx,
        f"{arch}.embedding_length": embedding,
        f"{arch}.block_count": n_layers,
        f"{arch}.attention.head_count": n_heads,
        f"{arch}.attention.head_count_kv": n_kv_heads,
    }
    if key_length is not None:
        kvs[f"{arch}.attention.key_length"] = key_length
    return kvs
