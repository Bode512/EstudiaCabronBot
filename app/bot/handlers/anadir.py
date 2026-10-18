"""/anadir: guided creation of exams and assignments with cancellation at every step."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.bot.keyboards import parse_callback, subject_keyboard, type_keyboard
from app.bot.middleware import require_registered
from app.utils.dates import parse_spanish_datetime

logger = logging.getLogger(__name__)

CHOOSE_TYPE, CHOOSE_SUBJECT, ASK_TITLE, ASK_DATE, CONFIRM_DRAFT = range(5)


def _get_user_id(update: Update) -> int | None:
    if update.effective_user is None:
        return None
    return update.effective_user.id


@require_registered
async def cmd_anadir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message is None:
        return ConversationHandler.END
    context.user_data["draft"] = {}
    await update.effective_message.reply_html("<b>Que quieres crear?</b>", reply_markup=type_keyboard())
    return CHOOSE_TYPE


@require_registered
async def on_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    parsed = parse_callback(query.data or "")
    if parsed is None:
        await query.answer("Invalido")
        return ConversationHandler.END
    _s, _id, action, arg = parsed
    if action != "type":
        await query.answer()
        return await _cancel_from_callback(query, context)
    await query.answer()
    kind = arg or "exam"
    context.user_data["draft"] = {"kind": kind}
    subjects = await _list_subjects(update, context)
    await query.message.reply_html(
        "<b>Asignatura?</b>", reply_markup=subject_keyboard([(sid, name) for sid, name in subjects])
    )
    return CHOOSE_SUBJECT


async def _cancel_from_callback(query, context) -> int:
    context.user_data.pop("draft", None)
    await query.message.reply_text("Cancelado.")
    return ConversationHandler.END


async def _list_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> list[tuple[int, str]]:
    import sqlalchemy as sa

    from app.database.models import Subject

    ctx = context.bot_data["ctx"]
    db_user = await ctx.users.get_user_by_telegram(update.effective_user.id)
    if db_user is None:
        return []
    async with ctx.session_factory() as session:
        rows = (await session.execute(sa.select(Subject).where(Subject.user_id == db_user.id).order_by(Subject.name))).scalars().all()
    return [(s.id, s.name) for s in rows]


@require_registered
async def on_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is not None:
        parsed = parse_callback(query.data or "")
        if parsed is None:
            await query.answer("Invalido")
            return ConversationHandler.END
        _s, sid, action, arg = parsed
        await query.answer()
        if action != "subject":
            return await _cancel_from_callback(query, context)
        if arg == "other" or sid == 0:
            await query.message.reply_text("Escribe la asignatura (p. ej. 'Fisica'):")
            return CHOOSE_SUBJECT
        context.user_data["draft"]["subject"] = str(sid)
        await query.message.reply_text("Titulo corto (enter para autogenerar):")
        return ASK_TITLE
    # free text subject
    text = (update.effective_message.text or "").strip() if update.effective_message else ""
    if text and context.user_data.get("draft", {}).get("subject") is None:
        context.user_data["draft"]["subject"] = text[:80]
        await update.effective_message.reply_text("Titulo corto (enter para autogenerar):")
        return ASK_TITLE
    return CHOOSE_SUBJECT


@require_registered
async def on_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip() if update.effective_message else ""
    if text:
        context.user_data["draft"]["title"] = text[:180]
    await update.effective_message.reply_text(
        "Fecha ('jueves', '20/11', '12 de noviembre a las 9'). /cancelar para salir:"
    )
    return ASK_DATE


@require_registered
async def on_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip() if update.effective_message else ""
    if not text:
        return ASK_DATE
    tz = await _user_tz(update, context)
    parsed = parse_spanish_datetime(text, tz)
    if parsed is None:
        await update.effective_message.reply_text("No entendi la fecha. Prueba 'jueves', '14/11', '12 de noviembre a las 9'.")
        return ASK_DATE
    draft = context.user_data.get("draft", {})
    draft["day"] = parsed.day
    draft["time"] = parsed.time
    draft["assumed_year"] = parsed.assumed_year
    context.user_data["draft"] = draft

    kind = draft.get("kind", "exam")
    when = parsed.day.strftime("%d/%m/%Y") + (f" {parsed.time[0]:02d}:{parsed.time[1]:02d}" if parsed.time else "")
    reminder_note = "se crearan avisos 7/3/1 dias y 1 h antes" if kind == "exam" else "avisos 1 dia y 1 h antes"
    text_confirm = (
        f"<b>Confirmar</b>\nTipo: {'Examen' if kind == 'exam' else 'Tarea/Trabajo'}\n"
        f"Asignatura: {draft.get('subject', 'General')}\nTitulo: {draft.get('title') or '(auto)'}\n"
        f"Fecha: {when}{' (anio asumido)' if parsed.assumed_year else ''}\n"
        f"Recordatorios: {reminder_note}\n\nAceptar?"
    )
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Crear", callback_data="ecb:conv:0:draft_ok"),
                InlineKeyboardButton("Cancelar", callback_data="ecb:nav:0:cancel"),
            ]
        ]
    )
    await update.effective_message.reply_html(text_confirm, reply_markup=keyboard)
    return CONFIRM_DRAFT


@require_registered
async def on_draft_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    parsed = parse_callback(query.data or "")
    await query.answer()
    if parsed is None or parsed[2] != "draft_ok":
        context.user_data.pop("draft", None)
        await query.message.reply_text("Cancelado.")
        return ConversationHandler.END
    draft = context.user_data.pop("draft", None)
    if not draft:
        await query.message.reply_text("No hay nada pendiente.")
        return ConversationHandler.END
    ctx = context.bot_data["ctx"]
    db_user = await ctx.users.get_user_by_telegram(update.effective_user.id)
    if db_user is None:
        await query.message.reply_text("No estas registrado. Escribe /start.")
        return ConversationHandler.END
    user_id = db_user.id
    kind = draft.get("kind", "exam")
    try:
        async with ctx.session_factory() as session:
            from app.database.repositories import AuditRepository

            audit = AuditRepository(session)
            if kind == "exam":
                from app.services.exams import ExamService

                service = ExamService(session)
                exam_at = None
                has_time = draft.get("time") is not None
                if has_time:
                    hh, mm = draft["time"]
                    from app.utils.timezones import from_local_to_utc

                    exam_at = from_local_to_utc(draft["day"], hh, mm, await _user_tz(update, context))
                title = draft.get("title") or f"Examen de {draft['subject']}"
                exam = await service.create_exam(
                    user_id,
                    audit,
                    title=title,
                    subject_name=draft.get("subject"),
                    exam_date=None if has_time else draft["day"],
                    exam_at=exam_at,
                    has_time=has_time,
                )
                await session.commit()
                subject_name = await service.subject_name(exam)
                day_str = (draft["day"]).strftime("%d/%m/%Y")
                hh_mm = f" {hh:02d}:{mm:02d}" if has_time else ""
                await query.message.reply_html(
                    f"Examen creado: [{subject_name}] {exam.title} ({day_str}{hh_mm})."
                )
            else:
                from app.services.assignments import AssignmentService

                service = AssignmentService(session)
                due_at = None
                has_time = draft.get("time") is not None
                if has_time:
                    hh, mm = draft["time"]
                    from app.utils.timezones import from_local_to_utc

                    due_at = from_local_to_utc(draft["day"], hh, mm, await _user_tz(update, context))
                description = draft.get("title") or f"Trabajo de {draft.get('subject', 'General')}"
                assignment, created = await service.create_assignment(
                    user_id,
                    audit,
                    description=description,
                    subject_name=draft.get("subject"),
                    due_date=None if has_time else draft["day"],
                    due_at=due_at,
                    has_time=has_time,
                )
                await session.commit()
                if created:
                    day_str = draft["day"].strftime("%d/%m/%Y")
                    hh_mm = f" {hh:02d}:{mm:02d}" if has_time else ""
                    await query.message.reply_text(f"Tarea creada: {assignment.description} ({day_str}{hh_mm}).")
                else:
                    await query.message.reply_text("Ya existia una tarea identica abierta (no duplique).")
    except Exception:
        logger.exception("creando registro")
        await query.message.reply_text("No se pudo crear. Prueba de nuevo.")
    return ConversationHandler.END


async def _user_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    import sqlalchemy as sa

    from app.database.models import User

    user_id = _get_user_id(update)
    ctx = context.bot_data["ctx"]
    async with ctx.session_factory() as session:
        user = (await session.execute(sa.select(User).where(User.telegram_user_id == user_id))).scalar_one_or_none()
        return (user.timezone if user else None) or "Europe/Madrid"


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("draft", None)
    if update.effective_message:
        await update.effective_message.reply_text("Operacion cancelada.")
    return ConversationHandler.END


def register_anadir(app: Application) -> None:
    conv = ConversationHandler(
        entry_points=[CommandHandler("anadir", cmd_anadir)],
        states={
            CHOOSE_TYPE: [CallbackQueryHandler(on_type, pattern=r"^ecb:conv:0:type:")],
            CHOOSE_SUBJECT: [
                CallbackQueryHandler(on_subject, pattern=r"^ecb:conv:\d+:subject"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_subject),
            ],
            ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_title)],
            ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_date)],
            CONFIRM_DRAFT: [CallbackQueryHandler(on_draft_ok, pattern=r"^ecb:conv:0:draft_ok")],
        },
        fallbacks=[CommandHandler("cancelar", cancel_conv), CallbackQueryHandler(_fallback_cancel, pattern=r"^ecb:nav:\d+:cancel$")],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(conv, group=0)


async def _fallback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    context.user_data.pop("draft", None)
    if query and query.message:
        await query.message.reply_text("Cancelado.")
    return ConversationHandler.END
