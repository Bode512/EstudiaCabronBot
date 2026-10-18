"""Deterministic parser for Spanish date/time expressions used by the bot.

Design rules:
- "mañana", "el jueves", "el jueves que viene", "12 de noviembre", "12/11", "en 3 dias".
- Never invents a past date: dates falling in the past roll to the next plausible occurrence.
- When the year is missing and had to be inferred, `assumed_year=True` so handlers can
  confirm the assumption to the user ("12 de noviembre" -> next November, marked assumed).
- Situations that are genuinely ambiguous (e.g. bare weekday words after they already passed
  could be understood as "next week's") are resolved by the documented rule:
  "el <weekday>" = the NEXT occurrence strictly after today (if today matches, next week).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.utils.timezones import today_in_zone

WEEKDAYS = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "miércoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "sábado": 5,
    "domingo": 6,
}

MONTHS = {
    "enero": 1, "feb": 2, "febrero": 2, "marzo": 3, "mar": 3, "abril": 4, "abr": 4,
    "mayo": 5, "may": 5, "junio": 6, "jun": 6, "julio": 7, "jul": 7, "agosto": 8, "ago": 8,
    "septiembre": 9, "sept": 9, "setiembre": 9, "sep": 9, "octubre": 10, "oct": 10,
    "noviembre": 11, "nov": 11, "diciembre": 12, "dic": 12,
}

MONTHS_BY_NUMBER = {v: k for k, v in MONTHS.items() if len(k) > 3}


@dataclass(frozen=True)
class ParsedDate:
    day: date
    time: tuple[int, int] | None  # (hour, minute) or None if date-only
    assumed_year: bool
    matched_text: str

    def __str__(self) -> str:  # pragma: no cover - debug helper
        base = self.day.isoformat()
        if self.time:
            base += f" {self.time[0]:02d}:{self.time[1]:02d}"
        return base + (" (anio asumido)" if self.assumed_year else "")


def normalize(text: str) -> str:
    t = text.lower()
    t = t.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    t = re.sub(r"\s+", " ", t).strip()
    return t


_WEEKDAY_WORD = r"(?:lunes|martes|miercoles|jueves|viernes|sabado|domingo)"
_MONTH_WORD = r"(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre|ene|feb|mar|abr|may|jun|jul|ago|sep|sept|oct|nov|dic)"


def next_weekday_occurrence(base: date, weekday: int, allow_today: bool = False) -> date:
    delta = (weekday - base.weekday()) % 7
    if delta == 0 and not allow_today:
        delta = 7
    return base + timedelta(days=delta)


def _match_relative_words(text: str, base: date) -> date | None:
    if re.search(r"\bpasado manana\b", text):
        return base + timedelta(days=2)
    if re.search(r"\bmanana\b", text):
        return base + timedelta(days=1)
    if re.search(r"\bhoy\b|\beste dia\b", text):
        return base
    if re.search(r"\bayer\b", text):
        return base - timedelta(days=1)
    return None


def _match_weekday(text: str, base: date) -> date | None:
    m = re.search(rf"(?:el\s+|este\s+|proximo\s+|next\s+)?{_WEEKDAY_WORD}", text)
    if not m:
        return None
    wd = WEEKDAYS[m.group(0)]
    next_week = bool(re.search(r"\b(?:que\s+viene|proximo|siguiente|semana\s+que\s+viene)\b", text))
    d = next_weekday_occurrence(base, wd, allow_today=False)
    if next_week and (d - base).days < 7:
        d = d + timedelta(days=7)
    return d


def _match_day_month(text: str, base: date) -> tuple[date, bool] | None:
    # "12 de noviembre", "12 noviembre", "12 de nov de 2026"
    m = re.search(
        rf"\b(?P<day>\d{{1,2}})\s*(?:de\s+|d')?\s*(?P<month>{_MONTH_WORD})\b(?:\s*(?:de\s+|del\s+)?(?P<year>20\d{{2}})\b)?",
        text,
    )
    if not m:
        return None
    day = int(m.group("day"))
    month = MONTHS[m.group("month")]
    year = int(m.group("year")) if m.group("year") else None
    return _resolve_date(day, month, year, base)


def _match_numeric(text: str, base: date) -> tuple[date, bool] | None:
    # 12/11, 12-11-2026, 12.11
    m = re.search(r"\b(?P<day>\d{1,2})\s*[/\-.]\s*(?P<month>\d{1,2})\s*(?:[/\-.]\s*(?P<year>20\d{2}))?\b", text)
    if not m:
        return None
    day, month = int(m.group("day")), int(m.group("month"))
    if not 1 <= day <= 31 or not 1 <= month <= 12:
        return None
    year = int(m.group("year")) if m.group("year") else None
    return _resolve_date(day, month, year, base)


def _resolve_date(day: int, month: int, year: int | None, base: date) -> tuple[date, bool]:
    assumed = False
    if year is None:
        year = base.year
        assumed = True
    try:
        d = date(year, month, day)
    except ValueError:
        return base, assumed  # invalid -> let caller ask
    if assumed and d < base:
        d = date(year + 1, month, day)
    return d, assumed


def _match_en_in_days(text: str, base: date) -> tuple[date, bool] | None:
    m = re.search(r"\ben\s+(?P<num>\d{1,3}|una|dos|tres|cuatro|cinco|seis|siete)\s+(dias|dia|semanas|semana|meses|mes)\b", text)
    if not m:
        return None
    words = {"una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7}
    raw = m.group("num")
    num = int(raw) if raw.isdigit() else words[raw]
    unit = m.group(2)
    days = {"dia": 1, "dias": 1, "semana": 7, "semanas": 7, "mes": 30, "meses": 30}[unit]
    return base + timedelta(days=num * days), True


def _match_time(text: str) -> tuple[int, int] | None:
    # explicit "a las X[:mm]" (or X:30) first — unambiguous
    m = re.search(r"\ba\s*la?s?\s*(?P<h>\d{1,2})(?::(?P<m>\d{2}))?(?:\s*de\s*la?\s*(?P<part>manana|tarde|noche))?", text)
    if not m:
        # fallback: REQUIRES an explicit time marker (":mm", "y media/cuarto", "9h", "de la tarde"),
        # otherwise bare numbers like "21/11" would be misread as hour "21:00"
        for cand in re.finditer(
            r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2,})|\s*(?:y\s*media|y\s*cuarto))?\s*(?:h\b|horas?)?\s*(?:de\s*la?\s*(?P<part>manana|tarde|noche))?",
            text,
        ):
            token = cand.group(0)
            has_marker = (
                re.search(r":\d{2}", token)
                or re.search(r"\bh\b|horas\b", token)
                or re.search(r"media|cuarto", token)
                or re.search(r"manana|tarde|noche", token)
            )
            if not has_marker:
                continue
            m = cand
            break
        else:
            return None
    hour = int(m.group("h"))
    minute = 0
    extra = m.group("m") or ""
    if extra and extra.isdigit():
        minute = int(extra)
    if not m.group("m"):
        if re.search(r"\bmedia\b", text[m.start(): m.end() + 8]):
            minute = 30
        elif re.search(r"\bcuarto\b", text[m.start(): m.end() + 8]):
            minute = 15
    if 1 <= hour <= 12:
        part = m.group("part")
        if part in ("tarde", "noche") and hour != 12:
            hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    # Ignore implausible "times" that are actually day-of-suffix noise like "tema 3".
    if re.search(r"\b(?:tema|unidad|p\u00e1gina|pagina)\s*$", text[: m.start()]):
        return None
    return hour, minute


def parse_spanish_datetime(text: str, tz_name: str, now: date | None = None) -> ParsedDate | None:
    base = now or today_in_zone(tz_name)
    t = normalize(text)

    day: date | None = None
    assumed_year = False

    rel = _match_relative_words(t, base)
    if rel:
        day = rel

    if day is None:
        enin = _match_en_in_days(t, base)
        if enin:
            day, assumed_year = enin[0], False

    if day is None:
        wd = _match_weekday(t, base)
        if wd:
            day = wd

    if day is None:
        dm = _match_day_month(t, base)
        if dm:
            day, assumed_year = dm

    if day is None:
        num = _match_numeric(t, base)
        if num:
            day, assumed_year = num

    if day is None:
        return None

    time = _match_time(t)
    matched = t
    return ParsedDate(day=day, time=time, assumed_year=assumed_year, matched_text=matched)


def parse_spanish_date(text: str, tz_name: str, now: date | None = None) -> ParsedDate | None:
    """Date-only variant (ignores times: for exams without exact hour)."""
    return parse_spanish_datetime(text, tz_name, now)


def parse_availability_phrase(text: str) -> dict[int, int]:
    """Parse 'tengo dos horas libres hoy y cuatro el sabado' -> {weekday: minutes}.

    Returns {} if nothing recognised. Keys are ISO weekdays (0=Monday) or the strings
    'hoy' / 'manana' (the caller resolves them against its own 'today').
    """
    t = normalize(text)
    word_nums = {
        "un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
        "seis": 6, "siete": 7, "ocho": 8, "media": 0.5,
    }
    result: dict[int, int] = {}

    segments = [s for s in re.split(r",|\by\b(?!\s*(?:media|cuarto))", t) if s.strip()]
    amount_re = re.compile(
        r"(?P<num>\d+(?:[.,]\d)?|una|un|dos|tres|cuatro|cinco|seis|siete|ocho|media)\s*"
        r"(?:(?P<hours>horas?)|(?P<minutes>min(?:utos)?)|(?P<bare_h>\bh)\b)?"
        r"(?:(?:y|and)\s*(?P<extra>media|cuarto))?"
    )

    for segment in segments:
        m = amount_re.search(segment)
        if not m:
            continue
        raw = m.group("num")
        value = word_nums.get(raw)
        if value is None:
            try:
                value = float(raw.replace(",", "."))
            except ValueError:
                continue
        if m.group("minutes"):
            minutes = int(round(value))
        else:
            minutes = int(round(value * 60))  # 'horas' or 'h' suffix
        if m.group("extra") == "media":
            minutes += 30
        elif m.group("extra") == "cuarto":
            minutes += 15
        if minutes <= 0:
            continue

        target: int | tuple[int, int] | None = None
        if re.search(r"\bfin de semana\b", segment):
            target = (5, 6)
        elif re.search(r"\bhoy\b", segment):
            target = "hoy"
        elif re.search(r"\bmanana\b", segment):
            target = "manana"
        else:
            for wd, num in (
                ("lunes", 0), ("martes", 1), ("miercoles", 2), ("jueves", 3),
                ("viernes", 4), ("sabado", 5), ("domingo", 6),
            ):
                if re.search(rf"\b{wd}\b", segment):
                    target = num
                    break
        if target is None:
            continue
        days = target if isinstance(target, tuple) else (target,)
        for d in days:
            result[d] = minutes
    return result


def describe_day(d: date) -> str:
    months = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    weekdays = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]
    return f"{weekdays[d.weekday()]} {d.day:02d} {months[d.month - 1]} {d.year}"


def now_utc() -> datetime:
    """Re-export for convenience."""
    from app.utils.timezones import now_utc as _nu

    return _nu()


def epoch_minutes_ahead(dt: datetime, minutes: int) -> datetime:
    return dt + timedelta(minutes=minutes)
