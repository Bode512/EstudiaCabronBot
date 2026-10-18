"""Deterministic Spanish natural-language intent parser (no AI required).

Handles the core sentences:
  "Tengo un examen de Historia el jueves"
  "El examen de Matematicas es el 12 de noviembre"
  "Añade un trabajo de Filosofia para el lunes"
  "He terminado el tema 3 de Biologia"
  "Cambia el examen de Fisica al viernes"
Ambiguous/missing parts return intent='incomplete' plus what WAS parsed, so the
handler can ask only for the missing bits instead of a full questionnaire.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.utils.dates import ParsedDate, parse_spanish_datetime


@dataclass
class NLIntent:
    kind: str                     # 'exam' | 'assignment' | 'complete' | 'reschedule' | 'unknown' | 'incomplete'
    subject: str | None = None
    title: str | None = None
    parsed_date: ParsedDate | None = None
    topic_text: str | None = None


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", text).strip()


_SUBJECT_WORD_CAP = r"[A-Za-zÁÉÍÓÚáéíóúñÑüÜ][\wáéíóúñÑüÜ .]{1,40}?"


def parse_intent(text: str, user_subjects: list[str], tz_name: str, today=None) -> NLIntent:
    t = _norm(text)
    subjects_norm = [(s, _norm(s)) for s in user_subjects]

    def match_subject() -> str | None:
        # exact registered subjects first
        for name, normed in subjects_norm:
            if re.search(rf"\b{re.escape(normed)}\b", t):
                return name
        # generic capture: '<keyword> de <words>'
        m = re.search(
            rf"\b(?:examen|examenes|trabajo|tarea|proyecto|entrega|quiz|test|prueba)s?\s+"
            rf"(?:de\s+(?:la\s+|el\s+|los\s+|las\s+)?|en\s+)?({_SUBJECT_WORD_CAP})\b",
            text,
            re.IGNORECASE,
        )
        if m:
            candidate = m.group(1).strip(" .,;:")
            if candidate and not candidate.lower().startswith(
                ("que viene", "proxim", "esta semana", "manana", "hoy", "pasado")
            ):
                return candidate[0].upper() + candidate[1:]
        return None

    parsed_date = parse_spanish_datetime(text, tz_name, today)

    # ---- reschedule: "cambia/mueve/pasa el examen de X al/al viernes/12 de ..."
    if re.search(r"\b(cambia|cambiar|mueve|mover|pasa|pasar|adelanta|reprograma)\b", t):
        subject = match_subject()
        if not subject:
            m = re.search(rf"\bexamen\s+de\s+({_SUBJECT_WORD_CAP})", text, re.IGNORECASE)
            if m:
                subject = m.group(1).strip(" .,,:")
        return NLIntent(kind="reschedule", subject=subject, parsed_date=parsed_date)

    # ---- completion: "he terminado el tema 3 de Biologia"
    if re.search(r"\bhe\s+terminado\b|\btermine\b|\bhe\s+acabado\b|\bacabe\b|\bhe\s+estudiado\b", t):
        m_topic = re.search(r"\b(?:tema|unidad|capitulo)\s+(\d+)", t)
        subject = match_subject()
        if not subject:
            m = re.search(rf"\bde\s+({_SUBJECT_WORD_CAP})\b", text, re.IGNORECASE)
            if m:
                subject = m.group(1).strip(" .,;:")
        return NLIntent(
            kind="complete",
            subject=subject,
            topic_text=f"tema {m_topic.group(1)}" if m_topic else None,
        )

    # ---- creation: exam
    if re.search(r"\bexame?n\b|\bquizz?\b|\btest\b|\bprueba\b", t):
        subject = match_subject()
        if parsed_date is None:
            return NLIntent(kind="incomplete", subject=subject, title=None)
        return NLIntent(kind="exam", subject=subject, title=(subject and f"Examen de {subject}") or "Examen", parsed_date=parsed_date)

    # ---- creation: assignment
    if re.search(r"\btrabajo\b|\btareas?\b|\bproyecto\b|\bentrega\b|\bdeberes\b|\bpara\s+(entregar)\b", t):
        subject = match_subject()
        if parsed_date is None:
            return NLIntent(kind="incomplete", subject=subject)
        return NLIntent(
            kind="assignment",
            subject=subject,
            title=(subject and f"Trabajo de {subject}") or (text[:80]),
            parsed_date=parsed_date,
        )

    # ---- queries fall back to commands; no intent
    return NLIntent(kind="unknown")
