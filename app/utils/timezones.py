"""Timezone helpers. All DB datetimes are UTC; user-facing conversion happens here only."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo
from functools import lru_cache

SPANISH_WEEKDAYS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
SPANISH_WEEKDAYS_SHORT = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]


@lru_cache(maxsize=64)
def get_zone(name: str) -> tzinfo:
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def to_user_tz(dt: datetime, tz_name: str) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(get_zone(tz_name))


def from_local_to_utc(d: date, hour: int, minute: int, tz_name: str) -> datetime:
    """Combine a local date + wall time in user's tz into an aware UTC datetime."""
    local = datetime(d.year, d.month, d.day, hour, minute, tzinfo=get_zone(tz_name))
    return local.astimezone(UTC)


def local_from_utc(dt: datetime, tz_name: str) -> datetime:
    return to_user_tz(dt, tz_name)


def to_utc(local_dt: datetime) -> datetime:
    """Convert an aware local datetime to UTC; naive inputs are assumed UTC."""
    if local_dt.tzinfo is None:
        return local_dt
    return local_dt.astimezone(UTC)


def midnight_local_utc(d: date, tz_name: str) -> datetime:
    """Start-of-day in user timezone, expressed as UTC datetime."""
    return from_local_to_utc(d, 0, 0, tz_name)


def today_in_zone(tz_name: str) -> date:
    return to_user_tz(now_utc(), tz_name).date()


def weekday_name(d: date) -> str:
    return SPANISH_WEEKDAYS[d.weekday()]


def weekday_short(d: date) -> str:
    return SPANISH_WEEKDAYS_SHORT[d.weekday()]


def format_local_day(dt: datetime, tz_name: str) -> str:
    """lun 12 ene 2026"""
    l = local_from_utc(dt, tz_name)
    months = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    return f"{weekday_short(l.date())} {l.day:02d} {months[l.month - 1]} {l.year}"


def format_local_datetime(dt: datetime, tz_name: str) -> str:
    return f"{format_local_day(dt, tz_name)} {local_from_utc(dt, tz_name).strftime('%H:%M')}"


def days_until(target_day: date, tz_name: str) -> int:
    return (target_day - today_in_zone(tz_name)).days


def relative_day_label(days: int) -> str:
    if days == 0:
        return "hoy"
    if days == 1:
        return "manana"
    if days == 2:
        return "pasado manana"
    if days > 2:
        return f"en {days} dias"
    if days == -1:
        return "ayer"
    return f"hace {-days} dias"


def utc_offset_minutes(tz_name: str, for_date: date | None = None) -> int:
    d = for_date or today_in_zone(tz_name)
    dt = datetime(d.year, d.month, d.day, 12, 0, tzinfo=get_zone(tz_name))
    off = dt.utcoffset() or timedelta(0)
    return int(off.total_seconds() // 60)


def zone_abbr(dt: datetime, tz_name: str) -> str:
    l = local_from_utc(dt, tz_name)
    abbr = l.strftime("%Z") or str(l.utcoffset())
    return str(abbr)
