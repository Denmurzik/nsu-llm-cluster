"""The llama.cpp settings of a deployment (task 2-22a): the admin API round trip."""

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.catalog.gguf import GgufMeta
from app.catalog.hf import HfFile
from app.models import ModelDeployment, NodeAssignment, PortalFlag
from tests.fakes.hf import FakeHf
from tests.fakes.k8s import FakeK8s
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

pytestmark = pytest.mark.docker

GiB = 2**30
REPO = "org/model-GGUF"
FILE = "model-Q4_K_M.gguf"
DRAFT_REPO = "org/draft-GGUF"
SHA = "c" * 40
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru"}
META = GgufMeta("llama", 32, 32, 8, 128, 4096, 32768)
NEW_FIELDS = (
    "reasoning", "kv_cache_type", "sampling", "draft_repo", "draft_file", "draft_revision", "mmproj_file",
    "extra_args", "system_prompt", "experimental",
)


@pytest.fixture
def settings(settings):
    settings.hf_endpoint = "http://127.0.0.1:9"
    return settings


@pytest_asyncio.fixture(autouse=True)
async def env(app):
    async with app.state.sessionmaker() as session:
        await session.execute(delete(NodeAssignment))
        await session.execute(delete(ModelDeployment))
        await session.execute(delete(PortalFlag))
        await session.commit()
    app.state.hf = FakeHf(
        files={REPO: [HfFile(FILE, 4 * GiB), HfFile("mmproj-f16.gguf", 100)], DRAFT_REPO: [HfFile("draft-Q8_0.gguf", GiB)]},
        metas={(REPO, FILE): META},
        commits={REPO: SHA, DRAFT_REPO: "d" * 40},
    )
    app.state.k8s = FakeK8s()
    app.state.litellm = FakeLiteLLM()


def body(**kw) -> dict:
    return {"name": "m1", "repo": REPO, "file": FILE, **kw}


async def admin(client, app) -> dict:
    return csrf_headers(await login(client, app, ADMIN))


async def test_create_without_the_settings_gives_the_defaults(client, app):
    h = await admin(client, app)
    r = await client.post("/api/admin/deployments", json=body(), headers=h)
    assert r.status_code == 201, r.text
    got = r.json()
    assert {k: got[k] for k in NEW_FIELDS} == {
        "reasoning": "off", "kv_cache_type": "q8_0", "sampling": None, "draft_repo": None, "draft_file": None,
        "draft_revision": None, "mmproj_file": None, "extra_args": None, "system_prompt": None, "experimental": False,
    }
    assert (await client.get(f"/api/admin/deployments/{got['id']}")).json() == got
    assert (await client.get("/api/admin/deployments")).json() == [got]


async def test_round_trip_of_every_setting(client, app):
    h = await admin(client, app)
    settings = {
        "reasoning": "auto", "kv_cache_type": "q4_0", "sampling": {"temperature": 0.6, "top_k": 30},
        "draft_repo": DRAFT_REPO, "draft_file": "draft-Q8_0.gguf", "mmproj_file": "mmproj-f16.gguf",
        "extra_args": "--mlock --no-context-shift", "system_prompt": "Отвечай по-русски.", "experimental": True,
    }
    r = await client.post("/api/admin/deployments", json=body(**settings), headers=h)
    assert r.status_code == 201, r.text
    created = r.json()
    assert {k: created[k] for k in settings} == settings
    assert created["draft_revision"] == "d" * 40
    hashed = created["spec_hash"]

    # the gateway-side fields change no pods
    r = await client.patch(
        f"/api/admin/deployments/{created['id']}", json={"system_prompt": "Иначе.", "experimental": False}, headers=h
    )
    assert r.status_code == 200 and r.json()["system_prompt"] == "Иначе." and r.json()["experimental"] is False
    assert r.json()["spec_hash"] == hashed
    # the pod-side ones do
    r = await client.patch(f"/api/admin/deployments/{created['id']}", json={"kv_cache_type": "f16"}, headers=h)
    assert r.json()["kv_cache_type"] == "f16" and r.json()["spec_hash"] != hashed
    # the rest was not sent: unchanged; null and {} clear
    assert r.json()["reasoning"] == "auto" and r.json()["sampling"] == {"temperature": 0.6, "top_k": 30}
    r = await client.patch(
        f"/api/admin/deployments/{created['id']}",
        json={"sampling": {}, "mmproj_file": None, "extra_args": None, "system_prompt": None,
              "draft_repo": None, "draft_file": None},
        headers=h,
    )
    got = r.json()
    assert (got["sampling"], got["mmproj_file"], got["extra_args"], got["system_prompt"]) == (None, None, None, None)
    assert (got["draft_repo"], got["draft_file"], got["draft_revision"]) == (None, None, None)


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"extra_args": "--port 9"}, "Флаг --port задаётся порталом"),
        ({"extra_args": "--api_key x"}, "Флаг --api_key задаётся порталом"),
        ({"extra_args": "--n_gpu_layers 0"}, "Флаг --n_gpu_layers задаётся порталом"),
        ({"extra_args": "--my-secret-file x"}, "Флаги с файлами, путями и ключами задаются порталом"),
        ({"sampling": [1]}, "Сэмплинг: объект с параметрами"),
        ({"draft_repo": DRAFT_REPO, "draft_file": "absent.gguf"}, "Файл черновой модели не найден"),
        ({"mmproj_file": "a b.gguf"}, "проектора"),
        ({"extra_args": "-m /models/x.gguf"}, "Флаг -m задаётся порталом"),
        ({"extra_args": "--mlock /tmp/x"}, "Пути к файлам в своих флагах запрещены"),
        ({"sampling": {"mirostat": 1}}, "Неизвестный параметр сэмплинга"),
        ({"sampling": {"top_p": 2}}, "top_p"),
        ({"draft_repo": DRAFT_REPO}, "нужны и репозиторий, и файл"),
        ({"mmproj_file": "../x.gguf"}, "проектора"),
        ({"reasoning": "full"}, "Рассуждения"),
        ({"kv_cache_type": "q2_k"}, "KV-кэша"),
        ({"system_prompt": "я" * 8001}, "8000"),
    ],
)
async def test_invalid_settings_are_422_in_russian(client, app, kw, reason):
    h = await admin(client, app)
    r = await client.post("/api/admin/deployments", json=body(**kw), headers=h)
    assert r.status_code == 422 and reason in r.json()["detail"], r.text
    assert (await client.get("/api/admin/deployments")).json() == []
    ok = (await client.post("/api/admin/deployments", json=body(), headers=h)).json()
    r = await client.patch(f"/api/admin/deployments/{ok['id']}", json=kw, headers=h)
    assert r.status_code == 422 and reason in r.json()["detail"], r.text
