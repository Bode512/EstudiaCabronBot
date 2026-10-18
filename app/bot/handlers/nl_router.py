"""Free-text natural language router (fallback after all other handlers)."""

from __future__ import annotations

import logging
from datetime import date, datetime

import sqlalchemy as sa
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from app.bot.middleware import require_registered
from app.database.models import Subject, TopicStatus, User
from app.database.repositories import AuditRepository, TopicRepository
from app.services.assignments import AssignmentService
from app.services.errors import ValidationError
from app.services.exams import DuplicateExam, ExamService
from app.services.nl_parser import parse_intent
from app.utils.timezones import local_from_utc

logger = logging.getLogger(__name__)


async def _user_and_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = context.bot_data["ctx"]
    if update.effective_user is None:
        return None, [], "Europe/Madrid"
    async with ctx.session_factory() as session:
        user = (await session.execute(sa.select(User).where(User.telegram_user_id == update.effective_user.id))).scalar_one_or_none()
        if user is None:
            return None, [], "Europe/Madrid"
        subjects = (await session.execute(sa.select(Subject).where(Subject.user_id == user.id))).scalars().all()
        session.expunge(user)
        return user, [s.name for s in subjects], user.timezone or "Europe/Madrid"


def _resolve_event_dt(parsed, tz: str) -> tuple[date | None, datetime | None, bool]:
    if parsed is None:
        return None, None, False
    if parsed.time:
        from zoneinfo import ZoneInfo

        dt = datetime(parsed.day.year, parsed.day.month, parsed.day.day, parsed.time[0], parsed.time[1], tzinfo=ZoneInfo(tz))
        from app.utils.timezones import to_utc

        return None, to_utc(dt), True
    return parsed.day, None, False


@require_registered
async def on_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None or update.effective_user is None:
        return
    if context.user_data.get("draft") or context.user_data.get("move_target"):
        return  # pending conversational states take priority
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    user, subjects, tz = await _user_and_subjects(update, context)
    if user is None:
        return
    intent = parse_intent(text, subjects, tz)
    ctx = context.bot_data["ctx"]
    if intent.kind == "unknown":
        return
    if intent.kind == "incomplete":
        need = "falta la fecha" if intent.subject else "indica asignatura y fecha"
        await update.effective_message.reply_text(
            f"Entiendo la idea, pero {need}. Ejemplo: 'Examen de Historia el jueves'."
        )
        return
    try:
        async with ctx.session_factory() as session:
            audit = AuditRepository(session)
            if intent.kind == "exam":
                svc = ExamService(session)
                day, exam_at, has_time = _resolve_event_dt(intent.parsed_date, tz)
                exam = await svc.create_exam(
                    user.id, audit,
                    title=intent.title or "Examen",
                    subject_name=intent.subject,
                    exam_date=day,
                    exam_at=exam_at,
                    has_time=has_time,
                )
                await session.commit()
                if exam.exam_at:
                    dt = local_from_utc(exam.exam_at, tz)
                    fecha = f"{dt.strftime('%d/%m/%Y a las %H:%M')}"
                else:
                    fecha = exam.exam_date.strftime("%d/%m/%Y") if exam.exam_date else "?"
                await update.effective_message.reply_text(f"Creado examen: {exam.title} - {fecha}")
                return
            if intent.kind == "assignment":
                svc = AssignmentService(session)
                day, due_at, has_time = _resolve_event_dt(intent.parsed_date, tz)
                if day is None and due_at is None:
                    await update.effective_message.reply_text("Falta la fecha. Ej: 'Entrega de Fisica el lunes'.")
                    return
                task, created = await svc.create_assignment(
                    user.id, audit,
                    description=intent.title or "Trabajo",
                    subject_name=intent.subject,
                    due_date=day,
                    due_at=due_at,
                    has_time=has_time,
                )
                await session.commit()
                if not created:
                    await update.effective_message.reply_text("Esa tarea ya estaba anotada, no la repito.")
                    return
                await update.effective_message.reply_text(f"Tarea anotada: {task.description}")
                return
            if intent.kind == "reschedule":
                final_exam = await _do_reschedule(user.id, session, intent, tz)
                await session.commit()
                if final_exam.exam_at:
                    dt = local_from_utc(final_exam.exam_at, tz)
                    fecha = dt.strftime("%d/%m/%Y a las %H:%M")
                else:
                    fecha = final_exam.exam_date.strftime("%d/%m/%Y")
                await update.effective_message.reply_text(
                    f"Examen de {intent.subject} movido a {fecha}. Recordatorios recalculados."
                )
                return
            if intent.kind == "complete":
                done = await _do_complete(user.id, session, intent)
                await session.commit()
                if done:
                    await update.effective_message.reply_text("Apuntado: dominado. Buen ritmo.")
                else:
                    await update.effective_message.reply_text(
                        "No encuentro ese tema registrado. Marca materiales con /anadir o revisa con /progreso."
                    )
                return
    except DuplicateExam as exc:
        await update.effective_message.reply_text(str(exc))
    except ValidationError as exc:
        await update.effective_message.reply_text(str(exc))
    except Exception:
        logger.exception("nl router fallo")
        await update.effective_message.reply_text("No he podido procesar eso. Usa /anadir paso a paso.")


async def _do_reschedule(user_id: int, session, intent, tz: str):
    if intent.subject is None:
        raise ValidationError("Di que examen: 'Cambia el examen de Fisica al viernes'.")
    if intent.parsed_date is None:
        raise ValidationError("Falta la nueva fecha en la frase.")
    svc = ExamService(session)
    exams = await svc.list_active(user_id)
    matches = [e for e in exams if intent.subject.lower() in (await svc.subject_name(e)).lower()]
    if not matches:
        raise ValidationError(f"No encuentro un examen activo de {intent.subject}.")
    if len(matches) > 1:
        raise ValidationError(
            f"Hay {len(matches)} examenes de {intent.subject}; mueve el concreto desde /examenes."
        )
    day, exam_at, has_time = _resolve_event_dt(intent.parsed_date, tz)
    return await svc.move_exam(user_id, matches[0].id, day, exam_at, has_time=has_time)


async def _do_complete(user_id: int, session, intent) -> bool:
    if intent.subject is None or intent.topic_text is None:
        return False
    subj = (await session.execute(sa.select(Subject).where(Subject.user_id == user_id))).scalars().all()
    target = next((s for s in subj if s.name.lower() == (intent.subject or "").lower()), None)
    if target is None:
        return False
    repo = TopicRepository(session)
    topics = await repo.list_by_user(user_id, subject_id=target.id)
    num = intent.topic_text
    match = next((t for t in topics if t.name.lower().startswith(num.lower())), None)
    if match is None:
        return False
    await repo.update_owned(user_id, match.id, status=TopicStatus.MASTERED)
    return True


def register_nl(app: Application) -> None:
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_free_text), group=4)
