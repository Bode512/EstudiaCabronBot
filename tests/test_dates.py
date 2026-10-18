"""Timezone + Spanish date parsing + pure planner unit tests."""

from __future__ import annotations

from datetime import date, datetime

from app.utils.dates import parse_spanish_datetime
from app.utils.timezones import from_local_to_utc, local_from_utc


def test_spanish_date_numeric():
    p = parse_spanish_datetime("el 12/11", "Europe/Madrid")
    assert p is not None
    today = date.today()
    expected = date(today.year, 11, 12)
    if expected < today:
        expected = date(today.year + 1, 11, 12)
    assert p.day == expected
    assert p.assumed_year


def test_spanish_weekday_word():
    p = parse_spanish_datetime("jueves", "Europe/Madrid")
    assert p is not None
    assert p.day.weekday() == 3


def test_spanish_month_name_with_hour():
    p = parse_spanish_datetime("12 de noviembre a las 9:30", "Europe/Madrid")
    assert p is not None
    assert p.time == (9, 30)


def test_gibberish_returns_none():
    assert parse_spanish_datetime("no se da fecha", "Europe/Madrid") is None


def test_utc_round_trip():
    local = datetime.now()
    utc = from_local_to_utc(local.date(), 16, 0, "Europe/Madrid")
    back = local_from_utc(utc, "Europe/Madrid")
    assert back.hour == 16 and back.minute == 0
