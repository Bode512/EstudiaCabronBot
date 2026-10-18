"""Text helpers for Telegram messages (HTML mode) and formatting."""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from app.utils.timezones import days_until, format_local_datetime, format_local_day


def e(text: str) -> str:
    """Escape a string for HTML parse mode."""
    return html.escape(str(text), quote=False)


def due_emoji(days: int) -> str:
    if days < 0:
        return "URGENTE"
    if days <= 1:
        return "HOY/MANANA"
    if days <= 3:
        return "CERCANO"
    return "OK"


def priority_label(p: int) -> str:
    return {1: "alta", 2: "media", 3: "baja"}.get(p, str(p))


def status_label(s: str) -> str:
    return {
        "pending": "pendiente",
        "in_progress": "en curso",
        "completed": "completada",
        "cancelled": "cancelada",
        "planned": "planificada",
        "done": "hecha",
        "missed": "perdida",
        "skipped": "saltada",
        "not_started": "sin empezar",
        "ready": "listo",
        "learning": "aprendiendo",
        "reviewed": "repasado",
        "mastered": "dominado",
        "processing": "procesando",
        "sent": "enviado",
        "failed": "fallido",
        "expired": "expirado",
    }.get(s, s)


def format_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours, rem = divmod(minutes, 60)
    if rem == 0:
        return f"{hours} h"
    return f"{hours} h {rem:02d} min"


def relative_label(day: date, tz_name: str) -> str:
    days = days_until(day, tz_name)
    if days == 0:
        return "hoy"
    if days == 1:
        return "manana"
    if days == 2:
        return "pasado manana"
    if days > 2:
        return f"dentro de {days} dias"
    if days == -1:
        return "ayer"
    return f"hace {-days} dias"


def pretty_datetime(dt: datetime, tz_name: str) -> str:
    return e(format_local_datetime(dt, tz_name))


def pretty_day_label(dt: datetime, tz_name: str) -> str:
    return e(format_local_day(dt, tz_name))


def truncate(text: str, limit: int = 60) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def chunk_text(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    while text:
        out.append(text[:limit])
        text = text[limit:]
    return out


def days_delta(d: date, other: date) -> int:
    return (d - other).days


def tomorrow(day: date) -> date:
    return day + timedelta(days=1)
