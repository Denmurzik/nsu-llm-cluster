"""Own model files on disk (app.localmodels.store): names, the HfClient-like reader, GGUF checks, the drop folder."""

import os
import shutil
import time

import pytest

from app.catalog.gguf import GgufInvalid, GgufMeta
from app.catalog.hf import HfFile, HfNotFound
from app.catalog.sources import ModelSources
from app.localmodels import store as st
from app.localmodels.store import LocalNotFound, LocalStore
from tests.fakes.hf import FakeHf
from tests.helpers.gguf_builder import arch_kvs, build_gguf

TENSORS = [("token_embd.weight", (64, 100), 0), ("blk.0.attn_q.weight", (64, 64), 6400), ("blk.1.attn_q.weight", (64, 64), 10496)]


def gguf_bytes(n_layers: int = 2, max_ctx: int = 4096, pad: int = 20000, tensors=TENSORS) -> bytes:
    head = build_gguf(arch_kvs("llama", n_layers=n_layers, n_heads=8, n_kv_heads=2, embedding=64, max_ctx=max_ctx), tensors=tensors)
    return head + b"\0" * pad


def later_shard_bytes(pad: int = 4096) -> bytes:
    return build_gguf({"split.no": 1}, tensors=[("blk.1.ffn.weight", (64, 64), 0)]) + b"\0" * pad


def put(path, data: bytes, age_s: float = 3600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path)


def ready(store: LocalStore, name: str, sha256: str | None = None) -> None:
    """What the portal writes after a scan: every current file of the model is in its manifest."""
    store.write_manifests({name: {f.path: {"size": f.size, "mtime": f.mtime, "sha256": sha256} for f in store.files(name)}})


def symlink_or_skip(link, target, is_dir=False):
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges here")


# ---------- names ----------


@pytest.mark.parametrize(
    "repo,local", [("local/qwen", True), ("local/a.b-c_d", True), ("unsloth/x", False), ("localx/y", False)]
)
def test_is_local(repo, local):
    assert st.is_local(repo) is local


@pytest.mark.parametrize("name,ok", [("qwen3-235b", True), ("Q.5_x", True), ("", False), ("-x", False), ("a/b", False), ("a..b", False), ("x" * 97, False)])
def test_valid_name(name, ok):
    assert st.valid_name(name) is ok


@pytest.mark.parametrize(
    "file,ok",
    [("m-Q4_K_M.gguf", True), ("M.GGUF", True), ("m.bin", False), ("../m.gguf", False), ("a/m.gguf", False), (".m.gguf", False)],
)
def test_valid_file(file, ok):
    assert st.valid_file(file) is ok


@pytest.mark.parametrize(
    "file,name",
    [
        ("Qwen3-235B-A22B-Q2_K-00001-of-00003.gguf", "Qwen3-235B-A22B-Q2_K"),
        ("gemma 3 (1).gguf", "gemma-3-1"),
        ("model.gguf", "model"),
        ("___.gguf", "model"),
    ],
)
def test_suggest_name(file, name):
    assert st.suggest_name(file) == name


def test_paths_live_on_the_mirror_volume(store, tmp_path):
    assert store.local_dir == tmp_path / "models" / "local"
    assert store.uploads_dir == tmp_path / ".uploads"
    assert store.path_of("m", "f.gguf") == tmp_path / "models" / "local" / "m" / "f.gguf"
    with pytest.raises(LocalNotFound):
        store.path_of("..", "f.gguf")
    with pytest.raises(LocalNotFound):
        store.path_of("m", "../f.gguf")


# ---------- GGUF checks ----------


def test_check_gguf_reads_the_header_of_a_local_file(store, tmp_path):
    f = put(tmp_path / "m.gguf", gguf_bytes(max_ctx=32768))
    check = st.check_gguf(f)
    assert check.error is None
    assert (check.meta.arch, check.meta.n_layers, check.meta.max_ctx) == ("llama", 2, 32768)


def test_check_gguf_rejects_a_file_that_is_not_gguf(tmp_path):
    check = st.check_gguf(put(tmp_path / "m.gguf", b"PK\x03\x04" + b"\0" * 100))
    assert check.meta is None and check.error == "Файл не является GGUF"


def test_check_gguf_reports_a_file_cut_inside_the_header(tmp_path):
    check = st.check_gguf(put(tmp_path / "m.gguf", gguf_bytes(pad=0)[:60]))
    assert check.meta is None and check.error == "Файл обрывается внутри заголовка GGUF"


def test_check_gguf_later_shard_only_needs_the_signature(tmp_path):
    f = put(tmp_path / "m-00002-of-00002.gguf", later_shard_bytes())
    assert st.check_gguf(f) == st.FileCheck(None, None)
    bad = put(tmp_path / "x-00002-of-00002.gguf", b"nope" * 10)
    assert st.check_gguf(bad).error == "Файл не является GGUF"


def test_check_gguf_unsupported_version(tmp_path):
    data = bytearray(gguf_bytes())
    data[4:8] = (7).to_bytes(4, "little")
    assert st.check_gguf(put(tmp_path / "m.gguf", bytes(data))).error == "Неподдерживаемая версия GGUF: 7"


# ---------- the HfClient-like reader ----------


async def test_gguf_files_lists_the_model_directory_with_shards(store, tmp_path):
    d = tmp_path / "models" / "local" / "big"
    put(d / "big-Q2_K-00001-of-00002.gguf", gguf_bytes())
    put(d / "big-Q2_K-00002-of-00002.gguf", later_shard_bytes())
    put(d / "small-Q8_0.gguf", gguf_bytes())
    put(d / ".hidden.gguf", gguf_bytes())
    put(d / "README.md", b"x")
    ready(store, "big")
    choices = await store.gguf_files("local/big")
    assert [(c.file, c.quant, c.shards) for c in choices] == [
        ("big-Q2_K-00001-of-00002.gguf", "Q2_K", ["big-Q2_K-00001-of-00002.gguf", "big-Q2_K-00002-of-00002.gguf"]),
        ("small-Q8_0.gguf", "Q8_0", ["small-Q8_0.gguf"]),
    ]
    assert choices[0].size_bytes == (d / "big-Q2_K-00001-of-00002.gguf").stat().st_size + (d / "big-Q2_K-00002-of-00002.gguf").stat().st_size


async def test_unknown_local_model_is_not_found(store):
    with pytest.raises(LocalNotFound):
        await store.gguf_files("local/none")
    with pytest.raises(HfNotFound):  # the API maps both, the deployments service already handles HfNotFound
        await store.gguf_meta("local/none", "x.gguf")


async def test_gguf_meta_and_inspect_read_local_files(store, tmp_path):
    d = tmp_path / "models" / "local" / "m"
    f = put(d / "m.gguf", gguf_bytes(max_ctx=8192))
    ready(store, "m")
    meta = await store.gguf_meta("local/m", "m.gguf")
    assert isinstance(meta, GgufMeta) and meta.max_ctx == 8192
    meta2, layout = await store.gguf_inspect("local/m", [HfFile("m.gguf", f.stat().st_size)])
    assert meta2 == meta
    assert len(layout.layers) == 2 and layout.layers[0] == 10496 - 6400 and layout.embd_bytes == 6400


async def test_gguf_meta_of_a_non_gguf_file_is_invalid(store, tmp_path):
    put(tmp_path / "models" / "local" / "m" / "m.gguf", b"garbage" * 100)
    ready(store, "m")
    with pytest.raises(GgufInvalid):
        await store.gguf_meta("local/m", "m.gguf")


async def test_only_ready_settled_files_are_offered(store, tmp_path):
    """Deployments and the inspector see what the catalog calls ready: files in the manifest, unchanged since."""
    d = tmp_path / "models" / "local" / "m"
    put(d / "a.gguf", gguf_bytes())
    ready(store, "m")
    put(d / "b.gguf", gguf_bytes())  # copied in after the last scan
    assert [c.file for c in await store.gguf_files("local/m")] == ["a.gguf"]
    with pytest.raises(LocalNotFound):
        await store.gguf_meta("local/m", "b.gguf")
    put(d / "a.gguf", gguf_bytes(pad=5))  # replaced: not ready until the next scan checks it
    assert await store.gguf_files("local/m") == []


def test_manifests_are_written_only_on_change_and_removed_with_the_last_ready_file(store, tmp_path):
    d = tmp_path / "models" / "local" / "m"
    put(d / "a.gguf", b"1")
    ready(store, "m", sha256="ab" * 32)
    manifest = d / st.MANIFEST
    assert store.manifest("m") == {"a.gguf": {"size": 1, "mtime": (d / "a.gguf").stat().st_mtime, "sha256": "ab" * 32}}
    before = manifest.stat().st_mtime_ns
    os.utime(manifest, ns=(before - 10**9, before - 10**9))
    ready(store, "m", sha256="ab" * 32)
    assert manifest.stat().st_mtime_ns == before - 10**9  # same content: not rewritten
    store.write_manifests({})
    assert not manifest.exists()


async def test_vllm_is_not_offered_for_local_files(store):
    with pytest.raises(LocalNotFound):
        await store.vllm_info("local/m")


async def test_model_sources_route_the_local_namespace(store, tmp_path):
    put(tmp_path / "models" / "local" / "m" / "m.gguf", gguf_bytes())
    hf = FakeHf(files={"org/x": [HfFile("x.gguf", 10)]})
    ready(store, "m")
    sources = ModelSources(hf, store)
    assert [c.file for c in await sources.gguf_files("local/m")] == ["m.gguf"]
    assert [c.file for c in await sources.gguf_files("org/x")] == ["x.gguf"]
    assert hf.calls == [("list_files", "org/x")]  # the local model never reached Hugging Face


# ---------- the drop folder ----------


def test_adopt_moves_settled_loose_files_into_their_model_dirs(store, tmp_path):
    models = tmp_path / "models"
    put(models / "Big-Q2_K-00001-of-00002.gguf", gguf_bytes())
    put(models / "local" / "Big-Q2_K-00002-of-00002.gguf", later_shard_bytes())
    put(models / "fresh.gguf", gguf_bytes(), age_s=1)  # still being copied
    put(models / "notes.txt", b"x")
    moved = store.adopt_loose(settle_s=30, now=time.time())
    assert sorted(dst.relative_to(models).as_posix() for _, dst in moved) == [
        "local/Big-Q2_K/Big-Q2_K-00001-of-00002.gguf", "local/Big-Q2_K/Big-Q2_K-00002-of-00002.gguf",
    ]
    assert (models / "fresh.gguf").exists() and (models / "notes.txt").exists()


def test_adopt_keeps_a_loose_file_whose_target_exists(store, tmp_path):
    models = tmp_path / "models"
    put(models / "local" / "m" / "m.gguf", gguf_bytes())
    put(models / "m.gguf", gguf_bytes(pad=5))
    assert store.adopt_loose(settle_s=30, now=time.time()) == []
    assert (models / "m.gguf").exists()


def test_walk_lists_gguf_files_of_model_dirs_and_flags_names_the_mirror_cannot_serve(store, tmp_path):
    local = tmp_path / "models" / "local"
    put(local / "m" / "m.gguf", gguf_bytes())
    put(local / "m" / "sub" / "q.gguf", gguf_bytes())
    put(local / "m" / ".part.gguf", gguf_bytes())
    put(local / ".tmp" / "x.gguf", gguf_bytes())
    put(local / "m" / "readme.txt", b"x")
    put(local / "m" / "my model.gguf", gguf_bytes())
    put(local / "bad name" / "x.gguf", gguf_bytes())
    found = {(f.model, f.path): f.problem for f in store.walk()}
    assert found == {
        ("m", "m.gguf"): None,
        ("m", "sub/q.gguf"): None,
        ("m", "my model.gguf"): "Недопустимое имя файла «my model.gguf»: только латинские буквы, цифры, точка, дефис и "
        "подчёркивание. Переименуйте файл",
        ("bad name", "x.gguf"): "Недопустимое имя каталога модели «bad name»: только латинские буквы, цифры, точка, "
        "дефис и подчёркивание. Переименуйте каталог",
    }


def test_walk_and_adopt_skip_symlinks(store, tmp_path):
    outside = put(tmp_path / "elsewhere" / "secret.gguf", gguf_bytes())
    local = tmp_path / "models" / "local"
    put(local / "m" / "m.gguf", gguf_bytes())
    symlink_or_skip(local / "m" / "link.gguf", outside)
    symlink_or_skip(local / "linked", outside.parent, is_dir=True)
    symlink_or_skip(tmp_path / "models" / "loose.gguf", outside)
    assert [(f.model, f.path) for f in store.walk()] == [("m", "m.gguf")]
    assert store.adopt_loose(settle_s=0, now=time.time() + 10) == []
    assert (tmp_path / "models" / "loose.gguf").is_symlink()


def test_remove_deletes_files_and_the_empty_model_dir(store, tmp_path):
    local = tmp_path / "models" / "local"
    put(local / "m" / "a.gguf", b"1")
    put(local / "m" / "b.gguf", b"2")
    store.remove("m", ["a.gguf"])
    assert not (local / "m" / "a.gguf").exists() and (local / "m" / "b.gguf").exists()
    store.remove("m", ["b.gguf"])
    assert not (local / "m").exists()


def test_used_and_free_bytes(store, tmp_path):
    put(tmp_path / "models" / "local" / "m" / "a.gguf", b"1" * 1000)
    put(tmp_path / "models" / "loose.gguf", b"1" * 300)  # dropped, not moved yet: on the disk all the same
    put(tmp_path / "models" / "local" / "loose2.gguf", b"1" * 20)
    assert store.used_bytes() == 1320
    assert store.free_bytes() > 0


def test_the_disk_reserve_stays_above_the_kubelet_eviction_threshold(store, monkeypatch):
    """kubelet on llm-main evicts Postgres, LiteLLM and the mirror below 5 % free: uploads leave at least 15 % of the
    volume free, never less than DISK_RESERVE."""
    usage = lambda total, free: lambda _p: shutil._ntuple_diskusage(total, total - free, free)  # noqa: E731
    monkeypatch.setattr(st.shutil, "disk_usage", usage(1000 * st.GiB, 200 * st.GiB))
    assert (store.free_bytes(), store.reserve_bytes()) == (200 * st.GiB, 150 * st.GiB)
    monkeypatch.setattr(st.shutil, "disk_usage", usage(4 * st.GiB, 3 * st.GiB))
    assert store.reserve_bytes() == st.DISK_RESERVE
