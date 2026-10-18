"""Views: /hoy, /semana, /examenes, /tareas, /progreso + exam/task callbacks + move-date flow."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import sqlalchemy as sa
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.bot.keyboards import cancel_button, exam_menu_keyboard, parse_callback, task_menu_keyboard
from app.bot.middleware import require_registered
from app.constants import LIST_PAGE_SIZE
from app.database.models import (
    Assignment,
    AssignmentStatus,
    Exam,
    ExamStatus,
    SessionStatus,
    StudySession,
    Subject,
    Topic,
    TopicStatus,
    User,
)
from app.services.assignments import AssignmentService
from app.services.errors import OwnershipError
from app.services.exams import ExamService
from app.utils.dates import parse_spanish_datetime
from app.utils.text import format_minutes
from app.utils.timezones import from_local_to_utc, local_from_utc, now_utc, today_in_zone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers
async def _get_db_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> User | None:
    if update.effective_user is None:
        return None
    ctx = context.bot_data["ctx"]
    return await ctx.users.get_user_by_telegram(update.effective_user.id)


def _exam_day_utc(e: Exam, tz: str) -> datetime | None:
    if e.exam_at:
        return e.exam_at
    if e.exam_date:
        return from_local_to_utc(e.exam_date, 9, 0, tz)
    return None


def _assignment_deadline_utc(a: Assignment, tz: str) -> datetime | None:
    if a.due_at:
        return a.due_at
    if a.due_date:
        return from_local_to_utc(a.due_date, 23, 59, tz)
    return None


def relative_short(day: date | None, today: date) -> str:
    if day is None:
        return "sin fecha"
    delta = (day - today).days
    if delta == 0:
        return "hoy"
    if delta == 1:
        return "manana"
    if delta < 0:
        return f"hace {-delta}d"
    return f"{day.strftime('%d/%m')} (+{delta}d)"


# ---------------------------------------------------------------- /hoy
@require_registered
async def cmd_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _get_db_user(update, context)
    if user is None or update.effective_message is None:
        return
    ctx = context.bot_data["ctx"]
    tz = user.timezone
    today = today_in_zone(tz)
    day_start = from_local_to_utc(today, 0, 0, tz)
    day_end = from_local_to_utc(today, 23, 59, tz)
    now = now_utc()
    async with ctx.session_factory() as session:
        subjects = {s.id: s.name for s in (await session.execute(sa.select(Subject).where(Subject.user_id == user.id))).scalars()}
        exams = (await session.execute(sa.select(Exam).where(Exam.user_id == user.id, Exam.status == ExamStatus.ACTIVE))).scalars().all()
        assignments = (await session.execute(sa.select(Assignment).where(
            Assignment.user_id == user.id,
            Assignment.status.in_([AssignmentStatus.PENDING, AssignmentStatus.IN_PROGRESS]),
        ))).scalars().all()
        sessions_rows = (await session.execute(sa.select(StudySession).where(
            StudySession.user_id == user.id,
            StudySession.start_at >= day_start,
            StudySession.start_at < day_end,
        ))).scalars().all()

    lines = [f"<b>Hoy — {today.strftime('%d/%m/%Y')}</b>", ""]
    today_exams = sorted([e for e in exams if (d := _exam_day_utc(e, tz)) and day_start <= d <= day_end], key=lambda e: _exam_day_utc(e, tz))
    if today_exams:
        lines.append("<b>Examenes</b>")
        for e in today_exams:
            when = ""
            utc_day = _exam_day_utc(e, tz)
            if e.exam_at:
                when = " a las " + local_from_utc(utc_day, tz).strftime("%H:%M")
            lines.append(f"- [{subjects.get(e.subject_id or 0, 'General')}] {e.title}{when}")
    due_today, overdue = [], []
    for a in sorted(assignments, key=lambda a: _assignment_deadline_utc(a, tz) or now):
        d = _assignment_deadline_utc(a, tz)
        if d and d <= day_end:
            if d < now:
                overdue.append(a)
            else:
                due_today.append(a)
    if due_today:
        lines.append("")
        lines.append("<b>Entregas de hoy</b>")
        lines.extend(f"- [{subjects.get(a.subject_id or 0, 'General')}] {a.description}" for a in due_today)
    if overdue:
        lines.append("")
        lines.append("<b>Vencidas</b>")
        lines.extend(f"- [{subjects.get(a.subject_id or 0, 'General')}] {a.description}" for a in overdue)
    planned_today = sorted(
        [s for s in sessions_rows if s.status == SessionStatus.PLANNED],
        key=lambda s: s.start_at,
    )
    if planned_today:
        lines.append("")
        lines.append("<b>Estudio hoy</b>")
        for s in planned_today:
            subj = subjects.get(s.subject_id or 0, "Estudio")
            lines.append(f"- {local_from_utc(s.start_at, tz).strftime('%H:%M')} {subj} ({format_minutes(s.planned_minutes)})")
    if len(lines) == 2:
        lines.append("Nada pendiente para hoy.")
    await update.effective_message.reply_html("\n".join(lines), reply_markup=_main_nav_keyboard())


def _main_nav_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Semana", callback_data="ecb:nav:0:semana"),
                InlineKeyboardButton("Examenes", callback_data="ecb:nav:0:examenes"),
            ],
            cancel_button(),
        ]
    )


# ---------------------------------------------------------------- /semana
@require_registered
async def cmd_semana(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _get_db_user(update, context)
    if user is None or update.effective_message is None:
        return
    ctx = context.bot_data["ctx"]
    tz = user.timezone
    today = today_in_zone(tz)
    week_end = from_local_to_utc(today + timedelta(days=7), 23, 59, tz)
    now = now_utc()
    async with ctx.session_factory() as session:
        subjects = {s.id: s.name for s in (await session.execute(sa.select(Subject).where(Subject.user_id == user.id))).scalars()}
        exams = (await session.execute(sa.select(Exam).where(Exam.user_id == user.id, Exam.status == ExamStatus.ACTIVE))).scalars().all()
        assignments = (await session.execute(sa.select(Assignment).where(
            Assignment.user_id == user.id,
            Assignment.status.in_([AssignmentStatus.PENDING, AssignmentStatus.IN_PROGRESS]),
        ))).scalars().all()

    lines = ["<b>Proximos 7 dias</b>", ""]
    week_exams = sorted([e for e in exams if (d := _exam_day_utc(e, tz)) and now <= d <= week_end], key=lambda e: _exam_day_utc(e, tz))
    if week_exams:
        lines.append("<b>Examenes</b>")
        for e in week_exams:
            local = local_from_utc(_exam_day_utc(e, tz), tz)
            rel = relative_short(local.date(), today)
            time_part = local.strftime(" %H:%M") if e.has_time else ""
            lines.append(f"- {rel}{time_part}: [{subjects.get(e.subject_id or 0, 'General')}] {e.title}")
    week_tasks = sorted([a for a in assignments if (d := _assignment_deadline_utc(a, tz)) and d <= week_end], key=lambda a: _assignment_deadline_utc(a, tz) or now)
    overdue = [a for a in week_tasks if (_assignment_deadline_utc(a, tz) or now) < now]
    upcoming = [a for a in week_tasks if a not in overdue]
    if upcoming:
        lines.append("")
        lines.append("<b>Tareas/entregas</b>")
        for a in upcoming:
            d = _assignment_deadline_utc(a, tz)
            local = local_from_utc(d, tz) if d else None
            rel = relative_short(local.date() if local else None, today)
            lines.append(f"- {rel}: [{subjects.get(a.subject_id or 0, 'General')}] {a.description}")
    if overdue:
        lines.append("")
        lines.append("<b>Vencidas sin completar</b>")
        for a in overdue[:8]:
            lines.append(f"- [{subjects.get(a.subject_id or 0, 'General')}] {a.description}")
    if not week_exams and not week_tasks:
        lines.append("Nada en la proxima semana. Buen momento para /plan o adelantar temas.")
    await update.effective_message.reply_html("\n".join(lines))


# ---------------------------------------------------------------- /examenes
@require_registered
async def cmd_examenes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _get_db_user(update, context)
    if user is None or update.effective_message is None:
        return
    ctx = context.bot_data["ctx"]
    tz = user.timezone
    async with ctx.session_factory() as session:
        subjects = {s.id: s.name for s in (await session.execute(sa.select(Subject).where(Subject.user_id == user.id))).scalars()}
        exams = (await session.execute(sa.select(Exam).where(Exam.user_id == user.id, Exam.status == ExamStatus.ACTIVE))).scalars().all()
    exams = sorted(exams, key=lambda e: _exam_day_utc(e, tz) or datetime.max.replace(tzinfo=now_utc().tzinfo))
    lines = ["<b>Tus examenes (activos)</b>", ""]
    for idx, e in enumerate(exams, 1):
        day = local_from_utc(_exam_day_utc(e, tz), tz).date() if _exam_day_utc(e, tz) else None
        when = day.strftime("%d/%m") if day else "sin fecha"
        lines.append(f"{idx}. [{subjects.get(e.subject_id or 0, 'General')}] {e.title} - {when}")
    if not exams:
        lines.append("No tienes examenes activos. Crea uno con /anadir.")
    rows = [
        [InlineKeyboardButton(f"{subjects.get(e.subject_id or 0, 'General')}→{e.title[:28]}", callback_data=f"ecb:exam:{e.id}:menu")]
        for e in exams[:3 * LIST_PAGE_SIZE]
    ]
    rows.append(cancel_button())
    await update.effective_message.reply_html("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


# ---------------------------------------------------------------- /tareas
@require_registered
async def cmd_tareas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _get_db_user(update, context)
    if user is None or update.effective_message is None:
        return
    ctx = context.bot_data["ctx"]
    tz = user.timezone
    async with ctx.session_factory() as session:
        subjects = {s.id: s.name for s in (await session.execute(sa.select(Subject).where(Subject.user_id == user.id))).scalars()}
        tasks = (await session.execute(sa.select(Assignment).where(
            Assignment.user_id == user.id,
            Assignment.status.in_([AssignmentStatus.PENDING, AssignmentStatus.IN_PROGRESS]),
        ))).scalars().all()
    tasks = sorted(tasks, key=lambda a: _assignment_deadline_utc(a, tz) or datetime.max.replace(tzinfo=now_utc().tzinfo))
    lines = ["<b>Tus tareas pendientes</b>", ""]
    for idx, a in enumerate(tasks, 1):
        d = _assignment_deadline_utc(a, tz)
        local = local_from_utc(d, tz) if d else None
        when = local.strftime("%d/%m") if local else "sin fecha"
        lines.append(f"{idx}. [{subjects.get(a.subject_id or 0, 'General')}] {a.description} ({when})")
    if not tasks:
        lines.append("Nada pendiente. Buen trabajo.")
    rows = [
        [InlineKeyboardButton(f"{subjects.get(a.subject_id or 0, 'General')}→{a.description[:28]}", callback_data=f"ecb:task:{a.id}:menu")]
        for a in tasks[:3 * LIST_PAGE_SIZE]
    ]
    rows.append(cancel_button())
    await update.effective_message.reply_html("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


# ---------------------------------------------------------------- exam callbacks
@require_registered
async def on_exam_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    parsed = parse_callback(query.data or "")
    if parsed is None:
        await query.answer("Accion invalida.")
        return
    _scope, obj_id, action, _arg = parsed
    user = await _get_db_user(update, context)
    if user is None:
        await query.answer("No estas registrado.")
        return
    ctx = context.bot_data["ctx"]
    try:
        async with ctx.session_factory() as session:
            service = ExamService(session)
            if action == "menu":
                exam = await service.get_owned(user.id, obj_id)
                subject_name = await service.subject_name(exam)
                await query.answer()
                await query.message.reply_html(
                    f"<b>Examen</b>\n[{subject_name}] {exam.title}",
                    reply_markup=exam_menu_keyboard(exam.id),
                )
                return
            if action == "done":
                await service.complete_exam(user.id, obj_id)
                await session.commit()
                await query.answer("Hecho")
                await query.message.reply_text("Examen marcado como completado.")
                return
            if action == "cancelit":
                await service.cancel_exam(user.id, obj_id)
                await session.commit()
                await query.answer("Cancelado")
                await query.message.reply_text("Examen cancelado. Puedes crear uno nuevo con /anadir.")
                return
            if action == "delete":
                await service.delete_exam(user.id, obj_id)
                await session.commit()
                await query.answer("Borrado")
                await query.message.reply_text("Examen borrado.")
                return
            if action == "move":
                await service.get_owned(user.id, obj_id)
                await query.answer("Dime la nueva fecha")
                context.user_data["move_target"] = ("exam", obj_id)
                await query.message.reply_text(
                    "Escribe la nueva fecha ('viernes', '20/11', '12 de noviembre'). /cancelar para salir."
                )
                return
            await query.answer("Accion desconocida.")
    except OwnershipError:
        await query.answer("No autorizado")
    except Exception:
        logger.exception("callback exam fallo")
        await query.answer("Accion fallida")


# ---------------------------------------------------------------- task callbacks
@require_registered
async def on_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    parsed = parse_callback(query.data or "")
    if parsed is None:
        await query.answer("Accion invalida.")
        return
    _scope, obj_id, action, _arg = parsed
    user = await _get_db_user(update, context)
    if user is None:
        await query.answer("No estas registrado.")
        return
    ctx = context.bot_data["ctx"]
    try:
        async with ctx.session_factory() as session:
            service = AssignmentService(session)
            if action == "menu":
                a = await service.get_owned(user.id, obj_id)
                await query.answer()
                await query.message.reply_html(f"<b>Tarea</b>\n{a.description}", reply_markup=task_menu_keyboard(a.id))
                return
            if action == "done":
                await service.complete_assignment(user.id, obj_id)
                await session.commit()
                await query.answer("Hecho")
                await query.message.reply_text("Tarea completada. Buen trabajo.")
                return
            if action == "delete":
                await service.delete_assignment(user.id, obj_id)
                await session.commit()
                await query.answer("Borrado")
                await query.message.reply_text("Tarea borrada.")
                return
            if action == "move":
                await service.get_owned(user.id, obj_id)
                await query.answer("Dime el nuevo plazo")
                context.user_data["move_target"] = ("task", obj_id)
                await query.message.reply_text("Escribe el nuevo plazo ('lunes', '12/11', '12 de noviembre'). /cancelar para salir.")
                return
            await query.answer("Accion desconocida.")
    except OwnershipError:
        await query.answer("No autorizado")
    except Exception:
        logger.exception("callback task fallo")
        await query.answer("Accion fallida")


# ---------------------------------------------------------------- move-date text flow
@require_registered
async def on_move_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if "move_target" not in context.user_data or update.effective_message is None:
        return
    user = await _get_db_user(update, context)
    if user is None:
        return
    text = update.effective_message.text or ""
    parsed = parse_spanish_datetime(text, user.timezone)
    if parsed is None:
        await update.effective_message.reply_text("No entendi la fecha. Prueba 'viernes', '20/11' o '12 de noviembre'.")
        return
    kind, obj_id = context.user_data.pop("move_target")
    exam_at: datetime | None = None
    new_date: date | None = parsed.day
    if parsed.time:
        local = datetime(parsed.day.year, parsed.day.month, parsed.day.day, parsed.time[0], parsed.time[1], tzinfo=_zone(user.timezone))
        from app.utils.timezones import to_utc

        exam_at = to_utc(local)
        new_date = None
    ctx = context.bot_data["ctx"]
    try:
        async with ctx.session_factory() as session:
            if kind == "exam":
                service = ExamService(session)
                exam = await service.move_exam(user.id, obj_id, new_date, exam_at, has_time=parsed.time is not None)
                await session.commit()
                subject_name = await service.subject_name(exam)
                when = new_date.strftime("%d/%m/%Y") if new_date else exam_at.strftime("%d/%m/%Y %H:%M")
                extra = " (anio asumido; confirma si no es 2026)" if parsed.assumed_year else ""
                await update.effective_message.reply_html(f"Examen movido a {when}.{extra}\n[{subject_name}] {exam.title}\nRecordatorios regenerados.")
            else:
                service = AssignmentService(session)
                task = await service.move_deadline(user.id, obj_id, new_date, exam_at, has_time=parsed.time is not None)
                await session.commit()
                when = new_date.strftime("%d/%m/%Y") if new_date else exam_at.strftime("%d/%m/%Y %H:%M")
                await update.effective_message.reply_html(f"Plazo nuevo: {when}. {task.description}")
    except OwnershipError:
        await update.effective_message.reply_text("No autorizado.")


def _zone(tz_name: str):
    from zoneinfo import ZoneInfo

    return ZoneInfo(tz_name)


# ---------------------------------------------------------------- /progreso
@require_registered
async def cmd_progreso(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _get_db_user(update, context)
    if user is None or update.effective_message is None:
        return
    ctx = context.bot_data["ctx"]
    async with ctx.session_factory() as session:
        done_assign = (await session.execute(sa.select(sa.func.count()).select_from(Assignment).where(Assignment.user_id == user.id, Assignment.status == AssignmentStatus.COMPLETED))).scalar_one()
        done_minutes = (await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(StudySession.planned_minutes), 0)).select_from(StudySession)
            .where(StudySession.user_id == user.id, StudySession.status == SessionStatus.DONE)
        )).scalar_one()
        missed = (await session.execute(sa.select(sa.func.count()).select_from(StudySession).where(StudySession.user_id == user.id, StudySession.status == SessionStatus.MISSED))).scalar_one()
        topics_done = (await session.execute(sa.select(sa.func.count()).select_from(Topic).where(Topic.user_id == user.id, Topic.status == TopicStatus.MASTERED))).scalar_one()
    await update.effective_message.reply_html(
        "<b>Progreso acumulado</b>\n"
        f"Tareas completadas: {done_assign}\n"
        f"Estudio completado: {format_minutes(int(done_minutes))}\n"
        f"Sesiones perdidas: {missed}\n"
        f"Temas dominados: {topics_done}\n\n"
        "Las sesiones del plan se marcan desde /plan -> 'Sesion hecha' cuando terminas."
    )


# ---------------------------------------------------------------- nav callbacks
@require_registered
async def on_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    parsed = parse_callback(query.data or "")
    if parsed is None:
        await query.answer()
        return
    _s, _id, action, _arg = parsed
    fake_update = Update(update_id=1, message=query.message)
    handlers = {
        "hoy": ("app.bot.handlers.views", "cmd_hoy"),
        "semana": ("app.bot.handlers.views", "cmd_semana"),
        "examenes": ("app.bot.handlers.views", "cmd_examenes"),
        "tareas": ("app.bot.handlers.views", "cmd_tareas"),
    }
    if action == "cancel":
        await query.answer()
        context.user_data.pop("move_target", None)
        context.user_data.pop("draft", None)
        await query.message.reply_text("Cancelado.")
        return
    if action in handlers:
        await query.answer()
        import importlib

        module_name, func_name = handlers[action]
        func = getattr(importlib.import_module(module_name), func_name)
        await func(fake_update, context)
        return
    if action in ("anadir", "plan", "config"):
        await query.answer()
        import importlib

        mapping = {
            "anadir": ("app.bot.handlers.anadir", "cmd_anadir"),
            "plan": ("app.bot.handlers.plan", "cmd_plan"),
            "config": ("app.bot.handlers.recordatorios", "cmd_ajustes"),
        }
        module_name, func_name = mapping[action]
        func = getattr(importlib.import_module(module_name), func_name)
        await func(fake_update, context)
        return
    await query.answer()


# ---------------------------------------------------------------- registration
def register_views(app: Application) -> None:
    app.add_handler(CommandHandler("hoy", cmd_hoy), group=0)
    app.add_handler(CommandHandler("semana", cmd_semana), group=0)
    app.add_handler(CommandHandler("examenes", cmd_examenes), group=0)
    app.add_handler(CommandHandler("tareas", cmd_tareas), group=0)
    app.add_handler(CommandHandler("progreso", cmd_progreso), group=0)
    app.add_handler(CallbackQueryHandler(on_exam_callback, pattern=r"^ecb:exam:\d+:"), group=0)
    app.add_handler(CallbackQueryHandler(on_task_callback, pattern=r"^ecb:task:\d+:"), group=0)
    app.add_handler(CallbackQueryHandler(on_nav_callback, pattern=r"^ecb:nav:\d+:"), group=0)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(r"^tz \S+$"), on_move_date),
        group=9,  # runs last; no-ops when no move pending
    )
