"""Admin «Использование»: LiteLLM's global daily records -> totals, top users, top models, daily series, CSV."""

import csv
import io
import uuid
from datetime import date

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import HTTPException

from app.api.admin_usage import build, parse_range
from app.litellm_client import LiteLLMClient, LiteLLMError, LiteLLMUnavailable, UsageRow
from app.models import User
from tests.fakes.litellm import FakeLiteLLM
from tests.helpers.auth import csrf_headers, login

BASE = "http://litellm.test:4000"
ADMIN = {"sub": "adm", "email": "admin@g.nsu.ru", "email_verified": True, "name": "Админ"}
USER = {"sub": "usr", "email": "ivan@g.nsu.ru", "name": "Иван Иванов"}
D1, D2, D3 = date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)


def row(day, model, user_id, prompt, completion, requests, email=""):
    return UsageRow(day, model, user_id, email, prompt, completion, requests)


# ---- the range ---------------------------------------------------------------------------------------------------


def test_the_default_range_is_the_last_seven_days_up_to_today():
    first, last = parse_range(None, None)
    assert (last - first).days == 6


def test_an_explicit_range_is_inclusive_and_checked():
    assert parse_range("2026-11-01", "2026-11-03") == (D1, D3)
    assert parse_range("2026-11-03", "2026-11-03") == (D3, D3)
    for start, end, text in [
        ("11/01/2026", "2026-11-03", "ГГГГ-ММ-ДД"),
        ("2026-11-04", "2026-11-03", "позже"),
        ("2025-01-01", "2026-11-03", "366"),
        ("2026-13-01", None, "ГГГГ-ММ-ДД"),
    ]:
        with pytest.raises(HTTPException) as exc:
            parse_range(start, end)
        assert exc.value.status_code == 422 and text in exc.value.detail


# ---- aggregation ---------------------------------------------------------------------------------------------------


ROWS = [
    row(D1, "gemma", "u1", 10, 5, 2),
    row(D1, "qwen", "u1", 100, 50, 3),
    row(D1, "gemma", "u2", 1, 1, 1),
    row(D3, "gemma", "u2", 20, 20, 4),
    row(date(2026, 10, 1), "gemma", "u1", 999, 999, 9),  # outside the range
]
NAMES = {"u1": ("Иван Иванов", "ivan@g.nsu.ru"), "u2": ("Пётр", "petr@g.nsu.ru")}


def test_totals_top_lists_and_the_zero_filled_daily_series():
    out = build(ROWS, NAMES, D1, D3, "user")
    assert out["totals"] == {"prompt_tokens": 131, "completion_tokens": 76, "tokens": 207, "requests": 10}
    assert [(u["label"], u["email"], u["tokens"], u["requests"]) for u in out["top_users"]] == [
        ("Иван Иванов", "ivan@g.nsu.ru", 165, 5),
        ("Пётр", "petr@g.nsu.ru", 42, 5),
    ]
    assert [(m["key"], m["tokens"], m["requests"]) for m in out["top_models"]] == [("qwen", 150, 3), ("gemma", 57, 7)]
    assert [(d["day"], d["tokens"], d["requests"]) for d in out["by_day"]] == [
        ("2026-11-01", 167, 6),
        ("2026-11-02", 0, 0),
        ("2026-11-03", 40, 4),
    ]
    assert out["items"] == out["top_users"]


@pytest.mark.parametrize("group,key", [("user", "key"), ("model", "key"), ("day", "day")])
def test_the_chosen_grouping_is_returned_in_items(group, key):
    out = build(ROWS, NAMES, D1, D3, group)
    assert out["group"] == group
    assert [i[key] for i in out["items"]] == [i[key] for i in out[{"user": "top_users", "model": "top_models", "day": "by_day"}[group]]]


def test_the_top_lists_are_cut_to_ten_but_the_items_are_not():
    rows = [row(D1, "m", f"u{i}", i + 1, 0, 1) for i in range(15)]
    out = build(rows, {}, D1, D1, "user")
    assert len(out["top_users"]) == 10 and len(out["items"]) == 15
    assert out["items"][0]["tokens"] == 15  # the biggest first


def test_unknown_users_and_models_get_readable_labels():
    out = build([row(D1, "", "", 1, 1, 1)], {"": ("Не определён", "")}, D1, D1, "user")
    assert out["top_users"][0]["label"] == "Не определён"
    assert out["top_models"][0]["label"] == "Без модели"
    out = build([row(D1, "m", "ghost", 1, 1, 1)], {}, D1, D1, "user")
    assert out["top_users"][0]["label"] == "Не определён"


def test_an_empty_period_is_all_zeros():
    out = build([], {}, D1, D2, "model")
    assert out["totals"]["tokens"] == 0 and out["items"] == [] and len(out["by_day"]) == 2


# ---- the LiteLLM client against a faked /user/daily/activity ---------------------------------------------------------


def _metrics(prompt, completion, requests):
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion, "api_requests": requests}


def _day(date_text, models):
    """models: {model: {key_hash: (user_id, email, prompt, completion, requests)}} as LiteLLM v1.106.6 nests them."""
    groups = {
        m: {
            "metrics": {},
            "api_key_breakdown": {
                k: {"metrics": _metrics(*v[2:]), "metadata": {"user_id": v[0], "user_email": v[1], "key_exists": True}}
                for k, v in keys.items()
            },
        }
        for m, keys in models.items()
    }
    return {"date": date_text, "metrics": {}, "breakdown": {"model_groups": groups, "models": {}, "api_keys": {}}}


@pytest.fixture
def router():
    with respx.mock(base_url=BASE, assert_all_called=False) as r:
        yield r


@pytest_asyncio.fixture
async def litellm():
    c = LiteLLMClient(BASE, "sk-test")
    yield c
    await c.aclose()


async def test_global_usage_asks_without_a_user_and_reads_model_groups_per_key_owner(router, litellm):
    route = router.get("/user/daily/activity").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    _day("2026-11-01", {"gemma": {"h1": ("u1", "a@x", 10, 5, 2), "h2": ("u1", "a@x", 1, 1, 1), "h3": ("u2", "b@x", 3, 3, 1)}}),
                    _day("2026-11-02", {"qwen": {"h1": ("u1", "a@x", 7, 0, 1)}}),
                ],
                "metadata": {"page": 1, "has_more": False},
            },
        )
    )
    rows = await litellm.global_usage(D1, D2)
    params = route.calls.last.request.url.params
    assert "user_id" not in params and params["start_date"] == "2026-11-01" and params["end_date"] == "2026-11-02"
    assert [(r.day, r.model, r.user_id, r.user_email, r.prompt_tokens, r.completion_tokens, r.requests) for r in rows] == [
        (D1, "gemma", "u1", "a@x", 11, 6, 3),  # two keys of one user are one row
        (D1, "gemma", "u2", "b@x", 3, 3, 1),
        (D2, "qwen", "u1", "a@x", 7, 0, 1),
    ]


async def test_a_day_split_between_pages_is_summed_not_duplicated(router, litellm):
    def page(request: httpx.Request) -> httpx.Response:
        p = int(request.url.params["page"])
        user = ("u1", "u2")[p - 1]
        return httpx.Response(
            200,
            json={
                "results": [_day("2026-11-01", {"gemma": {f"h{p}": (user, "", 10 * p, 0, p)}}), _day("2026-11-01", {"gemma": {"h9": ("u1", "", 1, 0, 1)}}) if p == 2 else _day("2026-11-01", {})],
                "metadata": {"page": p, "has_more": p < 2},
            },
        )

    router.get("/user/daily/activity").mock(side_effect=page)
    rows = await litellm.global_usage(D1, D1)
    assert [(r.user_id, r.prompt_tokens, r.requests) for r in rows] == [("u1", 11, 2), ("u2", 20, 2)]


async def test_global_usage_falls_back_to_models_and_survives_missing_fields(router, litellm):
    router.get("/user/daily/activity").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "date": "2026-11-01",
                        "breakdown": {
                            "models": {"openai/x": {"api_key_breakdown": {"h": {"metrics": {"prompt_tokens": 4}, "metadata": {}}}}}
                        },
                    },
                    {"date": "2026-11-02"},
                    {"date": "2026-11-03", "breakdown": None},
                ],
            },
        )
    )
    rows = await litellm.global_usage(D1, D3)
    assert [(r.model, r.user_id, r.prompt_tokens, r.completion_tokens, r.requests) for r in rows] == [("openai/x", "", 4, 0, 0)]


async def test_global_usage_errors(router, litellm):
    router.get("/user/daily/activity").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(LiteLLMUnavailable):
        await litellm.global_usage(D1, D1)
    router.get("/user/daily/activity").mock(return_value=httpx.Response(401, text="no"))
    with pytest.raises(LiteLLMError):
        await litellm.global_usage(D1, D1)


# ---- the API ---------------------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api(app):
    app.state.litellm = FakeLiteLLM()
    return app.state.litellm


async def make_user(app, **kw) -> User:
    async with app.state.sessionmaker() as db:
        u = User(id=uuid.uuid4(), sub=kw["email"], email=kw["email"], full_name=kw.get("name", ""), role="user")
        db.add(u)
        await db.commit()
        return u


@pytest.mark.docker
async def test_usage_is_for_admins_only(client, app, api):
    assert (await client.get("/api/admin/usage")).status_code == 401
    assert (await client.get("/api/admin/usage.csv")).status_code == 401
    h = csrf_headers(await login(client, app, USER))
    assert (await client.get("/api/admin/usage", headers=h)).status_code == 403
    assert (await client.get("/api/admin/usage.csv", headers=h)).status_code == 403
    assert api.usage_asked == []


@pytest.mark.docker
async def test_usage_names_users_from_the_portal_table(client, app, api):
    ivan = await make_user(app, email="ivan@g.nsu.ru", name="Иван Иванов")
    nameless = await make_user(app, email="anon@g.nsu.ru")
    api.usage_rows = [
        row(D1, "gemma", str(ivan.id), 10, 5, 2),
        row(D1, "gemma", str(nameless.id), 1, 1, 1),
        row(D2, "qwen", "removed-user", 7, 3, 1, email="gone@g.nsu.ru"),
    ]
    h = csrf_headers(await login(client, app, ADMIN))
    r = await client.get("/api/admin/usage", params={"from": "2026-11-01", "to": "2026-11-03", "group": "user"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert api.usage_asked == [(D1, D3)]
    assert [(u["label"], u["email"], u["tokens"]) for u in body["items"]] == [
        ("Иван Иванов", "ivan@g.nsu.ru", 15),
        ("gone@g.nsu.ru", "gone@g.nsu.ru", 10),
        ("anon@g.nsu.ru", "anon@g.nsu.ru", 2),
    ]
    assert body["totals"]["requests"] == 4
    assert [d["tokens"] for d in body["by_day"]] == [17, 10, 0]
    assert {m["key"] for m in body["top_models"]} == {"gemma", "qwen"}


@pytest.mark.docker
async def test_usage_rejects_a_bad_range_or_group_before_asking_litellm(client, app, api):
    h = csrf_headers(await login(client, app, ADMIN))
    for params in ({"from": "x"}, {"from": "2026-11-05", "to": "2026-11-01"}, {"group": "key"}, {"from": "2020-01-01", "to": "2026-11-01"}):
        r = await client.get("/api/admin/usage", params=params, headers=h)
        assert r.status_code == 422, params
    assert api.usage_asked == []


@pytest.mark.docker
async def test_usage_tells_when_the_gateway_is_down(client, app, api):
    h = csrf_headers(await login(client, app, ADMIN))
    api.usage_error = LiteLLMUnavailable("down")
    r = await client.get("/api/admin/usage", headers=h)
    assert r.status_code == 503 and r.json()["detail"] == "Шлюз моделей недоступен"
    api.usage_error = LiteLLMError(400, "bad")
    assert (await client.get("/api/admin/usage", headers=h)).status_code == 502
    assert (await client.get("/api/admin/usage.csv", headers=h)).status_code == 502


@pytest.mark.docker
async def test_csv_has_a_header_the_rows_of_the_grouping_and_neutralises_formulas(client, app, api):
    evil = await make_user(app, email="e@g.nsu.ru", name='=HYPERLINK("http://x","click")')
    api.usage_rows = [row(D1, "gemma", str(evil.id), 10, 5, 2), row(D2, "gemma", str(evil.id), 1, 1, 1)]
    h = csrf_headers(await login(client, app, ADMIN))
    r = await client.get("/api/admin/usage.csv", params={"from": "2026-11-01", "to": "2026-11-02", "group": "user"}, headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="usage-user-2026-11-01-2026-11-02.csv"' == r.headers["content-disposition"]
    assert r.content.startswith(b"\xef\xbb\xbf")  # a BOM, so that a spreadsheet reads UTF-8
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == ["Пользователь", "Почта", "Токены запроса", "Токены ответа", "Токены всего", "Запросы"]
    assert rows[1] == ["'" + '=HYPERLINK("http://x","click")', "e@g.nsu.ru", "11", "6", "17", "3"]
    r = await client.get("/api/admin/usage.csv", params={"from": "2026-11-01", "to": "2026-11-02", "group": "day"}, headers=h)
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows == [
        ["День", "Токены запроса", "Токены ответа", "Токены всего", "Запросы"],
        ["2026-11-01", "10", "5", "15", "2"],
        ["2026-11-02", "1", "1", "2", "1"],
    ]
    r = await client.get("/api/admin/usage.csv", params={"from": "2026-11-01", "to": "2026-11-02", "group": "model"}, headers=h)
    assert list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))[1] == ["gemma", "11", "6", "17", "3"]
