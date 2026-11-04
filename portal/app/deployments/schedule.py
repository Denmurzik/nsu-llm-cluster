"""Working window of a deployment: weekdays and hours in Novosibirsk time.

Stored as `{"days": [1..7], "start": "HH:MM", "end": "HH:MM"}` (ISO weekdays, Monday is 1). The window starts at
`start` and ends before `end`; an `end` not after `start` runs past midnight and belongs to the day it starts on.
`end` may be "24:00" (the end of the day). No schedule means the deployment runs around the clock.
"""

import logging
import re
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DAY_MINUTES = 24 * 60
_warned: set[str] = set()  # broken schedules already logged by this process


def _zone() -> tzinfo:
    try:
        return ZoneInfo("Asia/Novosibirsk")
    except ZoneInfoNotFoundError:  # no tz database: Novosibirsk has kept UTC+7 without daylight saving since 2016
        return timezone(timedelta(hours=7), "Asia/Novosibirsk")


TZ = _zone()


def now_utc() -> datetime:
    """The current instant; tests replace it."""
    return datetime.now(UTC)


def _minutes(value: str, *, end: bool = False) -> int | None:
    if end and value == "24:00":
        return DAY_MINUTES
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        return None
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def problem(schedule: dict[str, Any]) -> str | None:
    """The Russian reason the schedule is invalid, or None."""
    days = schedule.get("days")
    if not isinstance(days, list) or not days:
        return "Выберите хотя бы один день расписания"
    if any(isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 7 for d in days):
        return "Дни расписания: числа от 1 (понедельник) до 7 (воскресенье)"
    start, end = _minutes(schedule.get("start")), _minutes(schedule.get("end"), end=True)
    if start is None or end is None:
        return "Время расписания указывается как ЧЧ:ММ"
    if start == end:
        return "Начало и конец окна расписания совпадают"
    return None


def normalized(schedule: dict[str, Any] | None) -> dict[str, Any] | None:
    if schedule is None:
        return None
    return {"days": sorted(set(schedule["days"])), "start": schedule["start"], "end": schedule["end"]}


def active(schedule: dict[str, Any] | None, now: datetime) -> bool:
    """The deployment should run at the instant `now` (timezone-aware).

    A stored schedule that breaks the rules (edited past the API) counts as no schedule: the deployment keeps
    running and a warning is logged, rather than every page and pass failing on it.
    """
    if not schedule:
        return True
    if not isinstance(schedule, dict) or problem(schedule) is not None:
        if repr(schedule) not in _warned:
            _warned.add(repr(schedule))
            log.warning("schedule: invalid working window %r ignored, the deployment runs around the clock", schedule)
        return True
    local = now.astimezone(TZ)
    minute = local.hour * 60 + local.minute
    start, end = _minutes(schedule["start"]), _minutes(schedule["end"], end=True)
    days = set(schedule["days"])
    today, yesterday = local.isoweekday(), (local.isoweekday() - 2) % 7 + 1
    if start < end:
        return today in days and start <= minute < end
    return (today in days and minute >= start) or (yesterday in days and minute < end)


def running(schedule: dict[str, Any] | None, now: datetime, drain_s: float) -> bool:
    """The deployment's pods should still exist at `now`: inside the window, or less than `drain_s` after its end
    (the backends are already out of LiteLLM, streams in progress finish)."""
    return active(schedule, now) or (drain_s > 0 and active(schedule, now - timedelta(seconds=drain_s)))
