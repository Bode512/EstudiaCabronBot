"""Pure message-content builders (Spanish). No DB access, fully testable."""

from __future__ import annotations

from datetime import date, datetime

from app.database.models import Assignment, Exam, ReminderType
from app.utils.text import relative_label


def _subject_name(subject_id: int | None, names: dict[int, str]) -> str:
    return names.get(subject_id or 0, "General")


def exam_advance_text(exam: Exam, subject_name: str, tz_name: str, event_local_dt: datetime, offset_minutes: int) -> str:
    when = event_local_dt.strftime("%d/%m/%Y") + (f" {event_local_dt.strftime('%H:%M')}" if exam.has_time else "")
    days = (event_local_dt.date() - datetime.now(event_local_dt.tzinfo).date()).days  # type: ignore[union-attr]
    pending_label = f"{offset_minutes // 60} h antes" if offset_minutes < 1440 and offset_minutes % 1440 else f"{offset_minutes // 1440} dias antes" if offset_minutes >= 1440 else "hoy"
    lines = [
        f"<b>Examen: {subject_name}</b>",
        f"{exam.title}",
        f"Fecha: {when} ({relative_label(event_local_dt.date(), tz_name)})",
    ]
    if exam.topics:
        lines.append(f"Temas: {exam.topics}")
    if exam.location:
        lines.append(f"Lugar: {exam.location}")
    if offset_minutes < 120 and days >= 0:
        lines.append("Ya casi esta. Suerte!")
    elif offset_minutes >= 1440 and days >= 0:
        lines.append(pending_label)
    return "\n".join(lines)


def assignment_due_text(assignment: Assignment, subject_name: str, tz_name: str, event_local_dt: datetime) -> str:
    when = event_local_dt.strftime("%d/%m/%Y") + (f" {event_local_dt.strftime('%H:%M')}" if assignment.has_time else "")
    lines = [
        f"<b>Vence: {subject_name}</b>",
        f"{assignment.description}",
        f"Plazo: {when} ({relative_label(event_local_dt.date(), tz_name)})",
    ]
    if assignment.notes:
        lines.append(f"Notas: {assignment.notes}")
    return "\n".join(lines)


def overdue_tasks_text(overdue: list[tuple[Assignment, str]], tz_name: str) -> str:
    lines = ["<b>Tareas vencidas</b>"]
    for assignment, subject_name in overdue:
        due = assignment.due_at or (assignment.due_date and datetime.combine(assignment.due_date, datetime.min.time()))
        when = due.strftime("%d/%m") if due else "sin fecha"
        lines.append(f"- [{subject_name}] {assignment.description} (vencio {when})")
    lines.append("Usa /tareas para marcarlas completadas o mover el plazo.")
    return "\n".join(lines)


def daily_summary_text(
    tz_name: str,
    today: date,
    exams_today: list[tuple[Exam, str]],
    exams_soon: list[tuple[Exam, str, int]],
    due_today: list[tuple[Assignment, str]],
    overdue: list[tuple[Assignment, str]],
    sessions_today: list[str],
) -> str:
    lines = [f"<b>Tu dia academico — {today.strftime('%d/%m/%Y')}</b>"]
    if exams_today:
        lines.append("")
        lines.append("<b>Examenes de hoy</b>")
        for exam, name in exams_today:
            lines.append(f"- {name}: {exam.title}")
    if due_today:
        lines.append("")
        lines.append("<b>Entregas de hoy</b>")
        for assignment, name in due_today:
            lines.append(f"- {name}: {assignment.description}")
    if sessions_today:
        lines.append("")
        lines.append("<b>Sesiones de estudio hoy</b>")
        lines.extend(f"- {s}" for s in sessions_today)
    if exams_soon:
        lines.append("")
        lines.append("<b>Proximos examenes</b>")
        for exam, name, days in exams_soon:
            when = "manana" if days == 1 else f"en {days} dias"
            lines.append(f"- {name}: {exam.title} ({when})")
    if overdue:
        lines.append("")
        lines.append("<b>Vencidas</b>")
        for assignment, name in overdue:
            lines.append(f"- {name}: {assignment.description}")
    if not (exams_today or due_today or sessions_today or exams_soon or overdue):
        lines.append("")
        lines.append("Nada pendiente. Buen dia para adelantar estudio o revisar /plan.")
    return "\n".join(lines)


def catchup_summary_text(items: list[tuple[str, str, str]], tz_name: str) -> str:
    """items: (kind_label, title, when_str)"""
    lines = ["<b>Mensajes acumulados mientras el bot estaba apagado</b>", ""]
    for kind, title, when in items:
        lines.append(f"- [{kind}] {title} — {when}")
    lines.append("")
    lines.append("Todo esto ocurrio durante la caida. Revisa /hoy y /semana para ver lo vigente.")
    return "\n".join(lines)


def reminder_type_label(rt: ReminderType) -> str:
    return {
        ReminderType.EXAM_ADVANCE: "Examen",
        ReminderType.ASSIGNMENT_DUE: "Entrega",
        ReminderType.DAILY_SUMMARY: "Resumen diario",
        ReminderType.OVERDUE_TASK: "Tarea vencida",
        ReminderType.CUSTOM: "Recordatorio",
    }[rt]
