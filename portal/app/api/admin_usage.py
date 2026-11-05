"""Admin «Использование»: tokens and requests of everybody, from LiteLLM's daily spend records.

  GET /api/admin/usage?from=&to=&group=user|model|day   totals, top users, top models, the daily series and the
                                                        rows of the chosen grouping (dates are UTC days, inclusive)
  GET /api/admin/usage.csv?from=&to=&group=...          the same rows as a CSV file

One LiteLLM call (`LiteLLMClient.global_usage`) feeds all of it. Users are named from the portal's table.
"""

import csv
import io
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.keys import gateway_down, gateway_error
from app.auth.sessions import require_admin
from app.db import get_session
from app.deps import get_litellm
from app.litellm_client import LiteLLMClient, LiteLLMError, LiteLLMUnavailable, UsageRow
from app.models import User

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

DEFAULT_DAYS = 7
MAX_DAYS = 366
TOP = 10
UNKNOWN_USER = "Не определён"
NO_MODEL = "Без модели"
Group = Literal["user", "model", "day"]


@dataclass
class _Sum:
    prompt: int = 0
    completion: int = 0
    requests: int = 0

    def add(self, row: UsageRow) -> None:
        self.prompt += row.prompt_tokens
        self.completion += row.completion_tokens
        self.requests += row.requests

    def out(self, **extra: Any) -> dict[str, Any]:
        return {
            **extra,
            "prompt_tokens": self.prompt,
            "completion_tokens": self.completion,
            "tokens": self.prompt + self.completion,
            "requests": self.requests,
        }


def parse_range(start: str | None, end: str | None) -> tuple[date, date]:
    """`from`/`to` as YYYY-MM-DD; by default the last 7 days up to today (UTC)."""
    try:
        last = date.fromisoformat(end) if end else datetime.now(UTC).date()
        first = date.fromisoformat(start) if start else last - timedelta(days=DEFAULT_DAYS - 1)
    except ValueError:
        raise HTTPException(status_code=422, detail="Дата в формате ГГГГ-ММ-ДД") from None
    if first > last:
        raise HTTPException(status_code=422, detail="Начало периода позже его конца")
    if (last - first).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=422, detail=f"Период не длиннее {MAX_DAYS} дней")
    return first, last


async def _names(db: AsyncSession, rows: Iterable[UsageRow]) -> dict[str, tuple[str, str]]:
    """LiteLLM user id -> (name, email) from the portal's users; LiteLLM's own email when the user is gone."""
    ids: dict[uuid.UUID, str] = {}
    for r in rows:
        try:
            ids[uuid.UUID(r.user_id)] = r.user_id
        except ValueError:
            pass
    found = {}
    if ids:
        result = await db.execute(select(User).where(User.id.in_(list(ids))))
        found = {str(u.id): u for u in result.scalars()}
    names: dict[str, tuple[str, str]] = {}
    for r in rows:
        if r.user_id in names:
            continue
        u = found.get(r.user_id)
        if u is not None:
            names[r.user_id] = (u.full_name or u.email, u.email)
        else:
            names[r.user_id] = (r.user_email or (r.user_id if r.user_id else UNKNOWN_USER), r.user_email)
    return names


def build(rows: list[UsageRow], names: dict[str, tuple[str, str]], first: date, last: date, group: Group) -> dict[str, Any]:
    total = _Sum()
    users: dict[str, _Sum] = defaultdict(_Sum)
    models: dict[str, _Sum] = defaultdict(_Sum)
    days: dict[date, _Sum] = defaultdict(_Sum)
    for r in rows:
        if not first <= r.day <= last:
            continue
        total.add(r)
        users[r.user_id].add(r)
        models[r.model].add(r)
        days[r.day].add(r)

    def user_item(uid: str, s: _Sum) -> dict[str, Any]:
        name, email = names.get(uid, (UNKNOWN_USER, ""))
        return s.out(key=uid, label=name, email=email)

    def model_item(model: str, s: _Sum) -> dict[str, Any]:
        return s.out(key=model, label=model or NO_MODEL)

    by_tokens = lambda kv: (-(kv[1].prompt + kv[1].completion), -kv[1].requests, kv[0])  # noqa: E731
    user_items = [user_item(k, v) for k, v in sorted(users.items(), key=by_tokens)]
    model_items = [model_item(k, v) for k, v in sorted(models.items(), key=by_tokens)]
    by_day = [
        days.get(d, _Sum()).out(day=d.isoformat(), key=d.isoformat(), label=d.isoformat())
        for d in (first + timedelta(days=i) for i in range((last - first).days + 1))
    ]
    return {
        "from": first.isoformat(),
        "to": last.isoformat(),
        "group": group,
        "totals": total.out(),
        "top_users": user_items[:TOP],
        "top_models": model_items[:TOP],
        "by_day": by_day,
        "items": {"user": user_items, "model": model_items, "day": by_day}[group],
    }


async def _usage(
    start: str | None, end: str | None, group: Group, db: AsyncSession, litellm: LiteLLMClient
) -> dict[str, Any]:
    first, last = parse_range(start, end)
    try:
        rows = await litellm.global_usage(first, last)
    except LiteLLMUnavailable:
        raise gateway_down() from None
    except LiteLLMError:
        raise gateway_error() from None
    return build(rows, await _names(db, rows), first, last, group)


@router.get("/usage")
async def usage(
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    group: Group = "user",
    db: AsyncSession = Depends(get_session),
    litellm: LiteLLMClient = Depends(get_litellm),
) -> dict[str, Any]:
    return await _usage(from_, to, group, db, litellm)


def _cell(value: Any) -> Any:
    """A text that a spreadsheet would run as a formula (a name from the login provider) is shown as text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@router.get("/usage.csv")
async def usage_csv(
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    group: Group = "user",
    db: AsyncSession = Depends(get_session),
    litellm: LiteLLMClient = Depends(get_litellm),
) -> Response:
    data = await _usage(from_, to, group, db, litellm)
    head = {"user": ["Пользователь", "Почта"], "model": ["Модель"], "day": ["День"]}[group]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([*head, "Токены запроса", "Токены ответа", "Токены всего", "Запросы"])
    for item in data["items"]:
        lead = {"user": [item.get("label"), item.get("email")], "model": [item["label"]], "day": [item["label"]]}[group]
        writer.writerow(
            [*(_cell(c) for c in lead), item["prompt_tokens"], item["completion_tokens"], item["tokens"], item["requests"]]
        )
    filename = f"usage-{group}-{data['from']}-{data['to']}.csv"
    return Response(
        content="\ufeff" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )
