"""«Мои модели»: resumable chunked upload, the drop folder, the catalog, deletion, deployments of own files."""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from app.catalog.hf import HfFile
from app.controllers.local_models import LocalModelsController
from app.localmodels import service as lm
from app.models import LocalModelFile, ModelDeployment, ModelUpload, NodeAssignment, PortalFlag
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login
from tests.test_local_store import gguf_bytes, later_shard_bytes, put

pytestmark = pytest.mark.docker

ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru"}
API = "/api/admin/local-models"


@pytest.fixture(autouse=True)
def _disk_reserve(monkeypatch):
    """Uploads keep 15 % of the volume free: the test host's own disk may be fuller than that."""
    from app.localmodels.store import DISK_RESERVE, LocalStore

    monkeypatch.setattr(LocalStore, "reserve_bytes", lambda self: DISK_RESERVE)


@pytest.fixture
def settings(settings, tmp_path):
    settings.hf_mirror_data_dir = str(tmp_path / "hf")
    settings.upload_chunk_mib = 1
    settings.local_models_settle_s = 0
    return settings


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    async with app.state.sessionmaker() as db:
        for model in (NodeAssignment, ModelDeployment, PortalFlag, ModelUpload, LocalModelFile):
            await db.execute(delete(model))
        await db.commit()
    app.state.hf = FakeHf(files={"org/x": [HfFile("x.gguf", 10)]})
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()


@pytest.fixture
def data(settings):
    from pathlib import Path

    return Path(settings.hf_mirror_data_dir)


async def admin(client, app) -> dict:
    return csrf_headers(await login(client, app, ADMIN))


async def create(client, h, model="m", file="m-Q4_K_M.gguf", content=b"", **kw):
    body = {"model": model, "file": file, "size_bytes": len(content), **kw}
    return await client.post(f"{API}/uploads", json=body, headers=h)


async def put_chunk(client, h, upload_id, offset, chunk):
    headers = {**h, "Content-Type": "application/octet-stream"}
    return await client.put(f"{API}/uploads/{upload_id}?offset={offset}", content=chunk, headers=headers)


async def upload_all(client, h, upload_id, content, chunk=400_000):
    for off in range(0, len(content), chunk):
        r = await put_chunk(client, h, upload_id, off, content[off : off + chunk])
        assert r.status_code == 200, r.text
        assert r.json()["received_bytes"] == min(off + chunk, len(content))
    return await client.post(f"{API}/uploads/{upload_id}/finish", headers=h)


async def verify(app):
    async with app.state.sessionmaker() as db:
        await lm.verify_pending(db, app.state.local_models)


async def scan(app):
    async with app.state.sessionmaker() as db:
        await lm.scan(db, app.state.local_models, app.state.settings)


def asgi_put(app, path: str, cookies: str, csrf: str, length: int, parts: list[bytes]):
    """A PUT straight through ASGI whose client goes away after `parts` (httpx cannot break a body off)."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT", "scheme": "https",
        "path": path.split("?")[0], "raw_path": path.split("?")[0].encode(), "query_string": path.split("?")[1].encode(),
        "root_path": "", "server": ("testserver", 443), "client": ("127.0.0.1", 5000),
        "headers": [
            (b"host", b"testserver"), (b"cookie", cookies.encode()), (b"x-csrf-token", csrf.encode()),
            (b"content-type", b"application/octet-stream"), (b"content-length", str(length).encode()),
        ],
    }
    messages = [{"type": "http.request", "body": p, "more_body": True} for p in parts] + [{"type": "http.disconnect"}]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    return app(scope, receive, send), sent


# ---------- access ----------


async def test_only_admins_reach_the_local_models_api(client, app):
    h = csrf_headers(await login(client, app, USER))
    assert (await client.get(API, headers=h)).status_code == 403
    assert (await create(client, h, content=b"x" * 100)).status_code == 403
    h = await admin(client, app)
    assert (await client.post(f"{API}/uploads", json={"model": "m", "file": "m.gguf", "size_bytes": 100})).status_code == 403


# ---------- upload ----------


async def test_chunked_upload_is_verified_and_lands_in_the_catalog(client, app, data):
    h = await admin(client, app)
    content = gguf_bytes(max_ctx=32768, pad=1_000_000)
    digest = hashlib.sha256(content).hexdigest()
    r = await create(client, h, content=content, sha256=digest.upper())
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["status"] == "uploading" and up["received_bytes"] == 0 and up["chunk_bytes"] == 2**20
    r = await upload_all(client, h, up["id"], content)
    assert r.status_code == 202 and r.json()["status"] == "verifying"

    await verify(app)
    r = await client.get(f"{API}/uploads/{up['id']}", headers=h)
    assert r.json()["status"] == "done" and r.json()["sha256"] == digest and r.json()["verified_bytes"] == len(content)
    target = data / "models" / "local" / "m" / "m-Q4_K_M.gguf"
    assert target.read_bytes() == content
    assert app.state.local_models.manifest("m")["m-Q4_K_M.gguf"]["sha256"] == digest
    assert not list((data / ".uploads").glob("*.part"))

    cat = (await client.get(API, headers=h)).json()
    [model] = cat["models"]
    assert model["name"] == "m" and model["repo"] == "local/m" and model["size_bytes"] == len(content)
    [f] = model["files"]
    assert f["file"] == "m-Q4_K_M.gguf" and f["quant"] == "Q4_K_M" and f["status"] == "ready" and f["origin"] == "upload"
    assert f["sha256"] == digest and f["meta"]["arch"] == "llama" and f["meta"]["max_ctx"] == 32768
    assert f["estimate"]["total_bytes"] > len(content) and f["used_by"] == []
    assert cat["uploads"] == []  # done uploads leave the list
    assert cat["storage"]["free_bytes"] > 0 and cat["storage"]["chunk_bytes"] == 2**20


async def test_a_new_create_of_the_same_file_resumes_the_upload(client, app):
    h = await admin(client, app)
    content = gguf_bytes(pad=900_000)
    up = (await create(client, h, content=content)).json()
    assert (await put_chunk(client, h, up["id"], 0, content[:300_000])).status_code == 200
    again = await create(client, h, content=content)
    assert again.status_code == 200 and again.json()["id"] == up["id"] and again.json()["received_bytes"] == 300_000
    other = await create(client, h, content=content + b"x")
    assert other.status_code == 409 and "размер" in other.json()["detail"]


async def test_a_chunk_at_the_wrong_offset_is_refused_with_the_current_one(client, app):
    h = await admin(client, app)
    content = gguf_bytes(pad=900_000)
    up = (await create(client, h, content=content)).json()
    await put_chunk(client, h, up["id"], 0, content[:100_000])
    r = await put_chunk(client, h, up["id"], 50_000, content[50_000:150_000])
    assert r.status_code == 409 and r.headers["Upload-Offset"] == "100000"
    assert "100000" in r.json()["detail"].replace(" ", "")
    r = await put_chunk(client, h, up["id"], 100_000, content[100_000:200_000])
    assert r.status_code == 200 and r.json()["received_bytes"] == 200_000


async def test_a_second_part_while_one_is_arriving_is_refused(client, app):
    h = await admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create(client, h, content=content)).json()

    async def parts():
        yield content[:100]

    async with app.state.sessionmaker() as holder, app.state.sessionmaker() as other:
        await holder.scalar(select(ModelUpload).where(ModelUpload.id == uuid.UUID(up["id"])).with_for_update())
        with pytest.raises(lm.LocalModelError) as e:
            await lm.receive_chunk(other, app.state.local_models, app.state.settings, uuid.UUID(up["id"]), 0, 100, parts())
        assert e.value.status == 409 and "другом запросе" in e.value.detail
        await holder.rollback()
    assert (await put_chunk(client, h, up["id"], 0, content[:100])).status_code == 200


async def test_a_part_broken_off_mid_way_keeps_the_bytes_that_arrived(client, app):
    h = await admin(client, app)
    content = gguf_bytes(pad=900_000)
    up = (await create(client, h, content=content)).json()
    cookies = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    call, _ = asgi_put(app, f"{API}/uploads/{up['id']}?offset=0", cookies, h["X-CSRF-Token"], 500_000,
                       [content[:120_000], content[120_000:200_000]])
    await call
    st = (await client.get(f"{API}/uploads/{up['id']}", headers=h)).json()
    assert st["status"] == "uploading" and st["received_bytes"] == 200_000
    assert (await put_chunk(client, h, up["id"], 200_000, content[200_000:300_000])).status_code == 200


async def test_a_full_disk_while_writing_is_507(client, app, monkeypatch):
    h = await admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create(client, h, content=content)).json()

    def full(f, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(lm, "_append", full)
    r = await put_chunk(client, h, up["id"], 0, content[:1000])
    assert r.status_code == 507 and "закончилось место" in r.json()["detail"]
    assert (await client.get(f"{API}/uploads/{up['id']}", headers=h)).json()["status"] == "uploading"


async def test_mutations_need_the_csrf_token(client, app):
    h = await admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create(client, h, content=content)).json()
    plain = {"Content-Type": "application/octet-stream"}
    assert (await client.put(f"{API}/uploads/{up['id']}?offset=0", content=content[:100], headers=plain)).status_code == 403
    assert (await client.delete(f"{API}/uploads/{up['id']}")).status_code == 403
    assert (await client.post(f"{API}/uploads/{up['id']}/finish")).status_code == 403
    assert (await client.delete(f"{API}/m")).status_code == 403
    assert (await client.post(f"{API}/scan")).status_code == 403


async def test_a_chunk_past_the_end_or_over_the_chunk_limit_is_refused(client, app):
    h = await admin(client, app)
    content = gguf_bytes(pad=2_500_000)
    up = (await create(client, h, content=content)).json()
    assert (await put_chunk(client, h, up["id"], 0, content[: 2**20 + 1])).status_code == 413
    small = (await create(client, h, model="s", content=gguf_bytes(pad=10))).json()
    r = await put_chunk(client, h, small["id"], 0, gguf_bytes(pad=10) + b"extra")
    assert r.status_code == 413
    assert (await client.get(f"{API}/uploads/{small['id']}", headers=h)).json()["received_bytes"] == 0


async def test_a_file_that_is_not_gguf_fails_on_its_first_chunk(client, app):
    h = await admin(client, app)
    content = b"PK\x03\x04" + b"\0" * 10_000
    up = (await create(client, h, content=content)).json()
    r = await put_chunk(client, h, up["id"], 0, content)
    assert r.status_code == 422 and r.json()["detail"] == "Файл не является GGUF"
    st = (await client.get(f"{API}/uploads/{up['id']}", headers=h)).json()
    assert st["status"] == "failed" and st["error"] == "Файл не является GGUF"


async def test_sha256_mismatch_fails_the_upload_and_keeps_the_folder_clean(client, app, data):
    h = await admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create(client, h, content=content, sha256="0" * 64)).json()
    await upload_all(client, h, up["id"], content)
    await verify(app)
    st = (await client.get(f"{API}/uploads/{up['id']}", headers=h)).json()
    assert st["status"] == "failed" and st["error"].startswith("sha256 не совпадает")
    assert not (data / "models" / "local" / "m").exists()
    assert not list((data / ".uploads").glob("*.part"))
    cat = (await client.get(API, headers=h)).json()
    assert cat["models"] == [] and [u["status"] for u in cat["uploads"]] == ["failed"]


async def test_cancel_during_verification_stops_it(client, app, data, monkeypatch):
    h = await admin(client, app)
    content = gguf_bytes(pad=50_000)
    up = (await create(client, h, content=content)).json()
    await upload_all(client, h, up["id"], content)
    monkeypatch.setattr(lm, "HASH_BLOCK", 1000)
    monkeypatch.setattr(lm, "PROGRESS_EVERY_S", 0)
    status = lm._status

    async def cancelled_meanwhile(db, upload_id):
        assert (await client.delete(f"{API}/uploads/{up['id']}", headers=h)).status_code == 204
        return await status(db, upload_id)

    monkeypatch.setattr(lm, "_status", cancelled_meanwhile)
    await verify(app)
    assert (await client.get(f"{API}/uploads/{up['id']}", headers=h)).json()["status"] == "cancelled"
    assert not (data / "models" / "local" / "m").exists() and not list((data / ".uploads").glob("*.part"))


async def test_cancel_right_before_the_rename_wins(client, app, data, monkeypatch):
    """The last step takes the row FOR UPDATE: a cancel that got in just before it is seen, nothing is moved."""
    h = await admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create(client, h, content=content)).json()
    await upload_all(client, h, up["id"], content)
    check = lm.check_gguf

    async def cancel():
        assert (await client.delete(f"{API}/uploads/{up['id']}", headers=h)).status_code == 204

    def check_then_cancel(path, name=None):  # runs in a worker thread: the cancel goes through the event loop
        result = check(path, name)
        asyncio.run_coroutine_threadsafe(cancel(), loop).result(10)
        return result

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(lm, "check_gguf", check_then_cancel)
    await verify(app)
    assert (await client.get(f"{API}/uploads/{up['id']}", headers=h)).json()["status"] == "cancelled"
    assert not (data / "models" / "local" / "m" / "m-Q4_K_M.gguf").exists()


async def test_verification_wins_over_a_scan_that_saw_the_file_first(client, app, data):
    """A scan can list the renamed file before verification commits its row: the upload still records its sha256,
    and later scans keep it."""
    h = await admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create(client, h, content=content)).json()
    await upload_all(client, h, up["id"], content)
    async with app.state.sessionmaker() as db:
        db.add(LocalModelFile(model="m", path="m-Q4_K_M.gguf", size_bytes=1, mtime=0.0, origin="folder", status="ready"))
        await db.commit()
    await verify(app)
    await scan(app)
    [f] = (await client.get(API, headers=h)).json()["models"][0]["files"]
    assert f["origin"] == "upload" and f["sha256"] == hashlib.sha256(content).hexdigest()
    manifest = app.state.local_models.manifest("m")
    assert manifest["m-Q4_K_M.gguf"]["sha256"] == hashlib.sha256(content).hexdigest()


async def test_finish_needs_the_whole_file(client, app):
    h = await admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create(client, h, content=content)).json()
    await put_chunk(client, h, up["id"], 0, content[:1000])
    r = await client.post(f"{API}/uploads/{up['id']}/finish", headers=h)
    assert r.status_code == 409 and "не полностью" in r.json()["detail"]


async def test_cancel_removes_the_part_and_stops_the_upload(client, app, data):
    h = await admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create(client, h, content=content)).json()
    await put_chunk(client, h, up["id"], 0, content[:1000])
    assert (await client.delete(f"{API}/uploads/{up['id']}", headers=h)).status_code == 204
    assert not list((data / ".uploads").glob("*.part"))
    r = await put_chunk(client, h, up["id"], 1000, content[1000:2000])
    assert r.status_code == 409
    assert (await client.get(f"{API}/uploads/{up['id']}", headers=h)).json()["status"] == "cancelled"
    assert (await create(client, h, content=content)).status_code == 201  # a new upload of the same file


@pytest.mark.parametrize(
    "body,detail",
    [
        ({"model": "a/b", "file": "m.gguf", "size_bytes": 100}, "Имя модели"),
        ({"model": "m", "file": "m.bin", "size_bytes": 100}, "Имя файла"),
        ({"model": "m", "file": "m.gguf", "size_bytes": 3}, "Размер файла"),
        ({"model": "m", "file": "m.gguf", "size_bytes": 100, "sha256": "xyz"}, "sha256"),
    ],
)
async def test_create_validates_the_request(client, app, body, detail):
    h = await admin(client, app)
    r = await client.post(f"{API}/uploads", json=body, headers=h)
    assert r.status_code == 422 and detail in r.json()["detail"]


async def test_create_refuses_a_file_the_model_already_has(client, app, data):
    put(data / "models" / "local" / "m" / "m.gguf", gguf_bytes())
    h = await admin(client, app)
    r = await create(client, h, file="m.gguf", content=b"x" * 100)
    assert r.status_code == 409 and "уже есть" in r.json()["detail"]


async def test_size_limit_and_free_space_are_checked(client, app, settings, monkeypatch):
    h = await admin(client, app)
    settings.local_models_max_gib = 1
    r = await client.post(f"{API}/uploads", json={"model": "m", "file": "m.gguf", "size_bytes": 2 * 2**30}, headers=h)
    assert r.status_code == 413 and "Лимит своих моделей" in r.json()["detail"]
    settings.local_models_max_gib = 0
    monkeypatch.setattr(type(app.state.local_models), "free_bytes", lambda self: 3 * 2**30)
    r = await client.post(f"{API}/uploads", json={"model": "m", "file": "m.gguf", "size_bytes": 2 * 2**30}, headers=h)
    assert r.status_code == 507 and "не хватает места" in r.json()["detail"]
    # 15 % of the volume stays free (kubelet evicts main's pods below 5 %): 200 GiB free of 1000 leave 50 GiB
    monkeypatch.setattr(type(app.state.local_models), "free_bytes", lambda self: 200 * 2**30)
    monkeypatch.setattr(type(app.state.local_models), "reserve_bytes", lambda self: 150 * 2**30)
    r = await client.post(f"{API}/uploads", json={"model": "m", "file": "m.gguf", "size_bytes": 51 * 2**30}, headers=h)
    assert r.status_code == 507 and "запас 150,0 ГБ" in r.json()["detail"], r.json()
    r = await client.post(f"{API}/uploads", json={"model": "m", "file": "m.gguf", "size_bytes": 49 * 2**30}, headers=h)
    assert r.status_code == 201, r.json()


async def test_stale_uploads_expire_and_stray_parts_are_removed(client, app, data):
    h = await admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create(client, h, content=content)).json()
    stray = put(data / ".uploads" / f"{uuid.uuid4()}.part", b"x", age_s=7200)
    async with app.state.sessionmaker() as db:
        await db.execute(update(ModelUpload).values(updated_at=datetime.now(UTC) - timedelta(days=8)))
        await db.commit()
        await lm.expire_uploads(db, app.state.local_models)
    st = (await client.get(f"{API}/uploads/{up['id']}", headers=h)).json()
    assert st["status"] == "cancelled" and "7 дней" in st["error"]
    assert not stray.exists() and not list((data / ".uploads").glob("*.part"))


# ---------- the drop folder ----------


async def test_scan_finds_dropped_files_checks_them_and_forgets_removed_ones(client, app, data):
    models = data / "models"
    put(models / "Big-Q2_K-00001-of-00002.gguf", gguf_bytes())
    put(models / "Big-Q2_K-00002-of-00002.gguf", later_shard_bytes())
    put(models / "local" / "half" / "half-00001-of-00003.gguf", gguf_bytes())
    put(models / "local" / "bad" / "bad.gguf", b"not a model" * 10)
    put(models / "local" / "bad" / "with space.gguf", gguf_bytes())
    h = await admin(client, app)
    cat = (await client.post(f"{API}/scan", headers=h)).json()
    by_name = {m["name"]: m for m in cat["models"]}
    assert set(by_name) == {"Big-Q2_K", "half", "bad"}
    [big] = by_name["Big-Q2_K"]["files"]
    assert big["status"] == "ready" and big["origin"] == "folder" and len(big["shards"]) == 2 and big["meta"]["n_layers"] == 2
    [half] = by_name["half"]["files"]
    assert half["status"] == "incomplete" and half["error"] == "Не хватает частей: есть 1 из 3"
    bad = {f["file"]: f for f in by_name["bad"]["files"]}
    assert bad["bad.gguf"]["status"] == "invalid" and bad["bad.gguf"]["error"] == "Файл не является GGUF"
    assert bad["with space.gguf"]["status"] == "invalid" and "Недопустимое имя файла" in bad["with space.gguf"]["error"]
    assert (models / "local" / "Big-Q2_K" / "Big-Q2_K-00001-of-00002.gguf").exists()
    # the mirror serves what the manifest lists: ready files only
    assert set(app.state.local_models.manifest("Big-Q2_K")) == {"Big-Q2_K-00001-of-00002.gguf", "Big-Q2_K-00002-of-00002.gguf"}
    assert app.state.local_models.manifest("bad") == {}

    for f in ("bad.gguf", "with space.gguf"):
        (models / "local" / "bad" / f).unlink()
    cat = (await client.post(f"{API}/scan", headers=h)).json()
    assert "bad" not in {m["name"] for m in cat["models"]}


async def test_a_model_dir_with_a_bad_name_is_listed_and_can_be_deleted(client, app, data):
    put(data / "models" / "local" / "my model" / "x.gguf", gguf_bytes())
    h = await admin(client, app)
    cat = (await client.post(f"{API}/scan", headers=h)).json()
    [m] = cat["models"]
    assert m["name"] == "my model" and m["files"][0]["status"] == "invalid"
    assert "Недопустимое имя каталога" in m["files"][0]["error"]
    assert (await client.delete(f"{API}/my%20model", headers=h)).status_code == 204
    assert not (data / "models" / "local" / "my model").exists()
    assert (await client.delete(f"{API}/..", headers=h)).status_code in (404, 405)


async def test_scan_leaves_a_file_that_is_still_being_copied(client, app, data, settings):
    settings.local_models_settle_s = 600
    put(data / "models" / "local" / "m" / "m.gguf", gguf_bytes(), age_s=5)
    h = await admin(client, app)
    cat = (await client.post(f"{API}/scan", headers=h)).json()
    assert cat["models"] == []


async def test_controller_verifies_uploads_and_scans_the_folder(client, app, data):
    h = await admin(client, app)
    content = gguf_bytes(pad=10_000)
    up = (await create(client, h, content=content)).json()
    await upload_all(client, h, up["id"], content)
    put(data / "models" / "dropped.gguf", gguf_bytes())
    await LocalModelsController(app.state).reconcile_once()
    assert (await client.get(f"{API}/uploads/{up['id']}", headers=h)).json()["status"] == "done"
    names = {m["name"] for m in (await client.get(API, headers=h)).json()["models"]}
    assert names == {"m", "dropped"}


# ---------- deletion and deployments ----------


async def test_delete_is_refused_while_a_deployment_uses_the_model(client, app, data):
    put(data / "models" / "local" / "m" / "m.gguf", gguf_bytes(max_ctx=16384))
    put(data / "models" / "local" / "m" / "other.gguf", gguf_bytes())
    h = await admin(client, app)
    await client.post(f"{API}/scan", headers=h)
    assert set(app.state.local_models.manifest("m")) == {"m.gguf", "other.gguf"}
    r = await client.post(
        "/api/admin/deployments", json={"name": "mine", "repo": "local/m", "file": "m.gguf", "ctx_per_request": 4096}, headers=h
    )
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["max_ctx"] == 16384

    cat = (await client.get(API, headers=h)).json()
    files = {f["file"]: f["used_by"] for f in cat["models"][0]["files"]}
    assert files == {"m.gguf": ["mine"], "other.gguf": []} and cat["models"][0]["used_by"] == ["mine"]

    r = await client.delete(f"{API}/m", headers=h)
    assert r.status_code == 409 and r.json()["detail"] == "Модель используется развёртываниями: mine"
    r = await client.delete(f"{API}/m?file=m.gguf", headers=h)
    assert r.status_code == 409
    assert (await client.delete(f"{API}/m?file=other.gguf", headers=h)).status_code == 204
    assert not (data / "models" / "local" / "m" / "other.gguf").exists()
    assert set(app.state.local_models.manifest("m")) == {"m.gguf"}

    dep_id = r = (await client.get("/api/admin/deployments", headers=h)).json()[0]["id"]
    await client.delete(f"/api/admin/deployments/{dep_id}", headers=h)
    assert (await client.delete(f"{API}/m", headers=h)).status_code == 204
    assert not (data / "models" / "local" / "m").exists()
    assert (await client.get(API, headers=h)).json()["models"] == []
    assert (await client.delete(f"{API}/m", headers=h)).status_code == 404


async def test_inspector_and_estimate_work_on_local_files(client, app, data):
    put(data / "models" / "local" / "m" / "m-Q8_0.gguf", gguf_bytes(max_ctx=8192))
    h = await admin(client, app)
    r = await client.get("/api/admin/hf/files?repo=local/m", headers=h)
    assert r.status_code == 200 and r.json() == []  # not checked by a scan yet: not offered
    await scan(app)
    r = await client.get("/api/admin/hf/files?repo=local/m", headers=h)
    assert r.status_code == 200 and [f["file"] for f in r.json()] == ["m-Q8_0.gguf"]
    r = await client.get(
        "/api/admin/hf/estimate?repo=local/m&file=m-Q8_0.gguf&ctx_per_request=4096&parallel=1&mode=split&group_size=2",
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["layers"] and r.json()["ctx_ok"] is True
    r = await client.get("/api/admin/hf/files?repo=local/none", headers=h)
    assert r.status_code == 404 and r.json()["detail"] == "Своя модель не найдена"
    assert app.state.hf.calls == []  # Hugging Face was never asked about local models


async def test_local_files_run_on_llamacpp_only(client, app, data):
    put(data / "models" / "local" / "m" / "m.gguf", gguf_bytes())
    h = await admin(client, app)
    r = await client.post(
        "/api/admin/deployments",
        json={"name": "v", "engine": "vllm", "repo": "local/m", "accel": "nvidia"},
        headers=h,
    )
    assert r.status_code == 422 and r.json()["detail"] == "Свои файлы моделей запускаются только на llama.cpp"


async def test_missing_meta_of_a_local_deployment_is_refreshed_from_the_file(app, data):
    put(data / "models" / "local" / "m" / "m.gguf", gguf_bytes(max_ctx=2048))
    await scan(app)
    async with app.state.sessionmaker() as db:
        db.add(ModelDeployment(name="mine", repo="local/m", file="m.gguf", ctx_per_request=1024, parallel=1))
        await db.commit()
        from app.catalog.sources import model_sources
        from app.deployments import service

        await service.refresh_missing_meta(db, model_sources(app.state), {})
        dep = await db.scalar(select(ModelDeployment).where(ModelDeployment.name == "mine"))
        assert dep.meta["max_ctx"] == 2048


async def test_workload_controller_refreshes_local_meta_through_the_sources(app, data):
    from app.controllers.workloads import WorkloadController

    put(data / "models" / "local" / "m" / "m.gguf", gguf_bytes(max_ctx=2048))
    await scan(app)
    async with app.state.sessionmaker() as db:
        db.add(ModelDeployment(name="mine", repo="local/m", file="m.gguf", ctx_per_request=1024, parallel=1))
        await db.commit()
    await WorkloadController(app.state).reconcile_once()
    async with app.state.sessionmaker() as db:
        dep = await db.scalar(select(ModelDeployment).where(ModelDeployment.name == "mine"))
        assert dep.meta is not None and dep.meta["max_ctx"] == 2048
    assert app.state.hf.calls == []
