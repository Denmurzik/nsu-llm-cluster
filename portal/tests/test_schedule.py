from datetime import UTC, datetime, timedelta

import pytest

from app.deployments import schedule
from app.deployments.schedule import active, problem

NSK = timedelta(hours=7)  # Asia/Novosibirsk, no daylight saving time


def at(weekday: int, hhmm: str) -> datetime:
    """UTC instant of a Novosibirsk wall time on an ISO weekday of the week of Monday 2026-11-02."""
    h, m = map(int, hhmm.split(":"))
    local = datetime(2026, 11, 1 + weekday, h, m)
    return (local - NSK).replace(tzinfo=UTC)


WORKDAYS = {"days": [1, 2, 3, 4, 5], "start": "08:00", "end": "20:00"}
NIGHTS = {"days": [1, 2, 3, 4, 5], "start": "22:00", "end": "06:00"}


def test_no_schedule_is_always_active():
    assert active(None, at(7, "03:00"))


def test_daytime_window_uses_novosibirsk_time():
    assert active(WORKDAYS, at(1, "08:00"))
    assert active(WORKDAYS, at(5, "19:59"))
    assert not active(WORKDAYS, at(1, "20:00"))  # the end is not part of the window
    assert not active(WORKDAYS, at(1, "07:59"))
    assert not active(WORKDAYS, at(6, "12:00"))  # Saturday is not listed
    # 08:30 in Novosibirsk is 01:30 UTC: the UTC clock alone would say the window is closed
    assert active(WORKDAYS, datetime(2026, 11, 2, 1, 30, tzinfo=UTC))


def test_overnight_window_belongs_to_the_day_it_starts():
    assert active(NIGHTS, at(5, "23:00"))  # Friday night
    assert active(NIGHTS, at(6, "05:59"))  # ... runs into Saturday morning
    assert not active(NIGHTS, at(6, "22:30"))  # Saturday night is not listed
    assert not active(NIGHTS, at(1, "05:00"))  # Monday morning belongs to Sunday night
    assert not active(NIGHTS, at(2, "12:00"))


def test_whole_day_window():
    weekend = {"days": [6, 7], "start": "00:00", "end": "24:00"}
    assert active(weekend, at(6, "00:00")) and active(weekend, at(7, "23:59"))
    assert not active(weekend, at(1, "00:00"))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"days": [], "start": "08:00", "end": "20:00"}, "Выберите хотя бы один день расписания"),
        ({"days": [0], "start": "08:00", "end": "20:00"}, "Дни расписания: числа от 1 (понедельник) до 7 (воскресенье)"),
        ({"days": [8], "start": "08:00", "end": "20:00"}, "Дни расписания: числа от 1 (понедельник) до 7 (воскресенье)"),
        ({"days": [1], "start": "8:00", "end": "20:00"}, "Время расписания указывается как ЧЧ:ММ"),
        ({"days": [1], "start": "24:00", "end": "20:00"}, "Время расписания указывается как ЧЧ:ММ"),
        ({"days": [1], "start": "08:00", "end": "20:61"}, "Время расписания указывается как ЧЧ:ММ"),
        ({"days": [1], "start": "08:00", "end": "08:00"}, "Начало и конец окна расписания совпадают"),
    ],
)
def test_problems_are_russian(value, message):
    assert problem(value) == message


def test_valid_schedules_have_no_problem():
    assert problem(WORKDAYS) is None
    assert problem(NIGHTS) is None
    assert problem({"days": [7], "start": "00:00", "end": "24:00"}) is None


def test_normalized_sorts_and_dedupes_days():
    assert schedule.normalized({"days": [5, 1, 5], "start": "08:00", "end": "20:00"}) == {
        "days": [1, 5], "start": "08:00", "end": "20:00",
    }
    assert schedule.normalized(None) is None


@pytest.mark.parametrize(
    "broken",
    [
        {"days": [1]},  # no start and end
        {"days": None, "start": "08:00", "end": "20:00"},
        {"days": [1], "start": 8, "end": "20:00"},
        {"days": ["1"], "start": "08:00", "end": "20:00"},
        ["08:00", "20:00"],
    ],
)
def test_a_broken_stored_schedule_counts_as_active_and_is_logged(broken, caplog):
    schedule._warned.clear()
    assert active(broken, at(1, "03:00"))
    assert "invalid working window" in caplog.text
    caplog.clear()
    assert active(broken, at(1, "04:00"))
    assert caplog.text == ""  # logged once, not on every pass


def test_running_covers_the_drain_after_the_window():
    assert schedule.running(WORKDAYS, at(1, "12:00"), 30)
    end = at(1, "20:00")
    assert not active(WORKDAYS, end + timedelta(seconds=10))
    assert schedule.running(WORKDAYS, end + timedelta(seconds=10), 30)  # streams in progress finish
    assert not schedule.running(WORKDAYS, end + timedelta(seconds=31), 30)
    assert not schedule.running(WORKDAYS, at(1, "07:59"), 30)  # nothing before the window opens
    assert not schedule.running(WORKDAYS, end + timedelta(seconds=10), 0)
    assert schedule.running(None, end, 30)
