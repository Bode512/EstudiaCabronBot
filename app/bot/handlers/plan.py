"""/plan (propuesta/aplicar/descartar/replan) y /disponibilidad (config de minutos)."""

from __future__ import annotations

import logging
import re

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

from app.bot.keyboards import parse_callback, plan_keyboard
from app.bot.middleware import require_registered
from app.constants import WEEKDAY_NAMES_INDEX
from app.database.models import PlanStatus, StudyPlan
from app.services.errors import ValidationError
from app.services.planning_service import PlanningService

logger = logging.getLogger(__name__)

ASK_DEFAULT_MINUTES, ASK_PER_DAY = range(2)


async def _get_db_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from sqlalchemy import select

    from app.database.models import User

    uid = update.effective_user.id if update.effective_user else None
    if uid is None:
        return None
    ctx = context.bot_data["ctx"]
    async with ctx.session_factory() as session:
        return (await session.execute(select(User).where(User.telegram_user_id == uid))).scalar_one_or_none()


def _show_proposal_text(plan: StudyPlan, proposal) -> str:
    return PlanningService.format_proposal(proposal, PlanningService.start_hour_of(plan))


@require_registered
async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    user = await _get_db_user(update, context)
    if user is None:
        return
    ctx = context.bot_data["ctx"]
    service = PlanningService(ctx.session_factory)
    try:
        plan, proposal = await service.generate_new_proposal(user.id)
    except Exception:
        logger.exception("plan generation")
        await update.effective_message.reply_text("No he podido generar el plan. Prueba de nuevo.")
        return
    text = _show_proposal_text(plan, proposal) + "\n\n<b>Aplicar este plan?</b>"
    await update.effective_message.reply_html(text, reply_markup=plan_keyboard(plan.id))


@require_registered
async def on_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    parsed = parse_callback(query.data or "")
    if parsed is None:
        await query.answer()
        return
    _s, plan_id, action, _arg = parsed
    user = await _get_db_user(update, context)
    if user is None:
        await query.answer("No estas registrado.")
        return
    service = PlanningService(context.bot_data["ctx"].session_factory)
    try:
        if action == "apply":
            ok = await service.approve_plan(user.id, plan_id)
            await query.answer("Aplicado" if ok else "Caducado")
            if ok:
                await query.message.reply_html(
                    "<b>Plan activo</b>. Los recordatorios de sesion apareceran en el resumen diario; "
                    "marca sesiones hechas desde /plan cuando termines."
                )
            else:
                await query.message.reply_text("El plan ya no esta pendiente.")
            return
        if action == "discard":
            ok = await service.discard_plan(user.id, plan_id)
            await query.answer("Descartado" if ok else "No pendiente")
            if ok:
                await query.message.reply_text("Plan descartado. Puedes pedir otro con /plan.")
            else:
                await query.message.reply_text("El plan ya no esta pendiente.")
            return
    except Exception:
        logger.exception("plan callback")
        await query.answer("Fallo")
        return
    await query.answer()


@require_registered
async def cmd_replan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    user = await _get_db_user(update, context)
    if user is None:
        return
    ctx = context.bot_data["ctx"]
    service = PlanningService(ctx.session_factory)
    plan, proposal = await service.replan(user.id)
    text = _show_proposal_text(plan, proposal) + "\n\n<b>Aplicar este plan?</b>"
    await update.effective_message.reply_html(text, reply_markup=plan_keyboard(plan.id))


@require_registered
async def cmd_sesiones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List planned study sessions of active plan with mark-done buttons."""
    if update.effective_message is None:
        return
    user = await _get_db_user(update, context)
    if user is None:
        return
    ctx = context.bot_data["ctx"]
    svc = PlanningService(ctx.session_factory)
    plan = await svc.current_plan(user.id)
    if plan is None or plan.status != PlanStatus.ACTIVE:
        await update.effective_message.reply_text("No hay plan activo. Genera uno con /plan.")
        return
    import sqlalchemy

    from app.database.models import SessionStatus, StudySession
    from app.utils.timezones import local_from_utc

    async with ctx.session_factory() as session:
        rows = (
            await session.execute(
                sqlalchemy.select(StudySession)
                .where(StudySession.user_id == user.id, StudySession.status == SessionStatus.PLANNED)
                .order_by(StudySession.start_at)
                .limit(20)
            )
        ).scalars().all()
    if not rows:
        await update.effective_message.reply_text("Todas las sesiones del plan estan hechas. Usa /replan para recalcular.")
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    tz = (await svc.user_context(user.id))["tz"]
    kb_rows = []
    for s in rows:
        hh = local_from_utc(s.start_at, tz).strftime("%H:%M")
        kb_rows.append([InlineKeyboardButton(f"{hh} {s.objective[:40]}", callback_data=f"ecb:ses:{s.id}:done")])
    kb_rows.append([InlineKeyboardButton("Descartar todas", callback_data="ecb:ses:0:missall")])
    await update.effective_message.reply_html(
        "<b>Sesiones pendientes:</b> pulsa 'hecha' al terminar",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


@require_registered
async def on_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    parsed = parse_callback(query.data or "")
    if parsed is None:
        await query.answer()
        return
    _s, session_id, action, _arg = parsed
    svc = PlanningService(context.bot_data["ctx"].session_factory)
    user = await _get_db_user(update, context)
    if user is None:
        await query.answer("No estas registrado.")
        return
    user_id = user.id
    try:
        if action == "done":
            ok = await svc.mark_session_done(user_id, session_id)
            await query.answer("Registrada" if ok else "No existe")
            await query.message.reply_text("Bien hecho. Progreso guardado.")
        elif action == "missall":
            count = await svc.mark_all_future_missed(user_id)
            await query.answer("Vale")
            await query.message.reply_text(f"{count} sesiones marcadas como no hechas; usa /replan para recolocarlas.")
        else:
            await query.answer()
    except Exception:
        logger.exception("session callback")
        await query.answer("Fallo")


# ---------------------------------------------------------------- /disponibilidad
@require_registered
async def cmd_disponibilidad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message is None:
        return ConversationHandler.END
    await update.effective_message.reply_html(
        "<b>Disponibilidad de estudio</b>\n"
        "Formatos validos:\n"
        "- '123' -> minutos por defecto para todos los dias\n"
        "- 'lunes 90, martes 60, 123' -> overrides por dia + resto\n"
        "/cancelar para salir."
    )
    return ASK_PER_DAY


@require_registered
async def on_availability_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = await _get_db_user(update, context)
    if user is None or update.effective_message is None:
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    ctx = context.bot_data["ctx"]
    try:
        weekly, default_minutes = parse_availability_text(text)
    except ValidationError as exc:
        await update.effective_message.reply_text(f"No lo he entendido: {exc}. Usa 'lunes 90, martes 60, 123'.")
        return ASK_PER_DAY
    async with ctx.session_factory() as session:
        from app.database.repositories import SettingsRepository

        repo = SettingsRepository(session)
        row = await repo.update(
            user.id,
            weekly_availability={str(k): v for k, v in weekly.items()},
            daily_availability_minutes=default_minutes,
        )
        await session.commit()
        summary = ", ".join(
            f"{WEEKDAY_NAMES_INDEX_INV.get(int(k), k)}: {v}" for k, v in sorted((row.weekly_availability or {}).items(), key=lambda kv: int(kv[0]))
        )
        await update.effective_message.reply_html(
            f"Guardo: por defecto {row.daily_availability_minutes} min; por dia: {summary or '(solo el valor por defecto)'}\n"
            "Ejecuta /plan para aplicar la nueva disponibilidad."
        )
        return ConversationHandler.END


def parse_availability_text(text: str) -> tuple[dict[int, int], int | None]:
    """Return (weekly overrides, default) where default may be None (no change)."""
    weekly: dict[int, int] = {}
    default: int | None = None
    for token in re.split(r"[,;]+|\s+y\s+", (text or "").strip()):
        token = token.strip()
        if not token:
            continue
        m = re.fullmatch(r"(\d{1,3})", token)
        if m:
            default = int(m.group(1))
            continue
        m = re.fullmatch(r"(lunes|martes|miercoles|jueves|viernes|sabado|domingo)\s+(\d{1,3})", token.lower())
        if m:
            day_idx = WEEKDAY_NAMES_INDEX[m.group(1)]
            weekly[day_idx] = int(m.group(2))
            continue
        raise ValidationError(f"no entiendo '{token}'")
    return weekly, default


WEEKDAY_NAMES_INDEX_INV = {0: "lunes", 1: "martes", 2: "miercoles", 3: "jueves", 4: "viernes", 5: "sabado", 6: "domingo"}


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text("Cancelado.")
    return ConversationHandler.END


def register_plan(app: Application) -> None:
    app.add_handler(CommandHandler("plan", cmd_plan), group=0)
    app.add_handler(CommandHandler("replan", cmd_replan), group=0)
    app.add_handler(CommandHandler("sesiones", cmd_sesiones), group=0)
    app.add_handler(CallbackQueryHandler(on_plan_callback, pattern=r"^ecb:plan:\d+:"), group=0)
    app.add_handler(CallbackQueryHandler(on_session_callback, pattern=r"^ecb:ses:\d+:"), group=0)
    conv = ConversationHandler(
        entry_points=[CommandHandler("disponibilidad", cmd_disponibilidad)],
        states={ASK_PER_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_availability_text)]},
        fallbacks=[CommandHandler("cancelar", cancel_conv)],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(conv, group=0)
