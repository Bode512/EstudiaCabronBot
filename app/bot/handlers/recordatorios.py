"""Settings handler (Spanish): /ajustes and /recordatorios (digest, quiet hours, catchup mode)."""

from __future__ import annotations

import logging
import re

import sqlalchemy as sa
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.bot.middleware import require_registered
from app.database.models import UserSettings
from app.database.repositories import SettingsRepository

logger = logging.getLogger(__name__)


@require_registered
async def cmd_ajustes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    row = await _settings_row(update, context)
    if row is None:
        return
    catchup_label = "resumen (lista)" if row.catchup_mode == "summary" else "detalle (todos)"
    quiet = f"{row.quiet_start:02d}:00-{row.quiet_end:02d}:00" if row.quiet_start is not None and row.quiet_end is not None else "no"
    await update.effective_message.reply_html(
        "<b>Ajustes</b>\n"
        f"Resumen diario: {'ON' if row.digest_enabled else 'OFF'} a las {row.digest_hour:02d}:00\n"
        f"Horas de silencio: {quiet}\n"
        f"Recordatorios acumulados tras dias sin conectarte: {catchup_label}\n"
        f"Examenes: aviso 7/3/1 dias y 1 h antes | Tareas: 24 h y 1 h antes\n"
        f"Sesion de estudio: {row.study_day_start_hour:02d}:00-{row.study_day_end_hour:02d}:00\n\n"
        "Cambia todo con /recordatorios."
    )


@require_registered
async def cmd_recordatorios(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_html(
        "<b>/recordatorios</b> (una orden por mensaje):\n"
        "/recordatorios resumen on|off\n"
        "/recordatorios resumen 8 (hora 0-23)\n"
        "/recordatorios silencio 23 7 (no avisa entre esas horas)\n"
        "/recordatorios sin silencio\n"
        "/recordatorios catchup resumen|todo (que mostrar tras 2 dias sin abrir el bot)"
    )


@require_registered
async def recordatorios_args(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    row = await _settings_row(update, context)
    if row is None:
        return
    args = (context.args or [])
    if not args:
        await cmd_recordatorios(update, context)
        return
    sub = args[0].lower()
    try:
        if sub == "resumen" and len(args) >= 2:
            if args[1].lower() in ("on", "si", "activar"):
                await _save(update, context, digest_enabled=True)
            elif args[1].lower() in ("off", "no", "desactivar"):
                await _save(update, context, digest_enabled=False)
            elif re.fullmatch(r"\d{1,2}", args[1]):
                hour = int(args[1])
                if not 0 <= hour <= 23:
                    raise ValueError("La hora debe ser 0-23.")
                await _save(update, context, digest_hour=hour)
            else:
                raise ValueError("Usa: resumen on | resumen off | resumen 8")
        elif sub == "silencio" and len(args) >= 3:
            start, end = int(args[1]), int(args[2])
            if not (0 <= start <= 23 and 0 <= end <= 23):
                raise ValueError("Horas deben ser 0-23.")
            await _save(update, context, quiet_start=start, quiet_end=end)
        elif len(args) >= 2 and args[0].lower() == "sin" and args[1].lower().startswith("silen"):
            await _save(update, context, quiet_start=None, quiet_end=None)
        elif sub == "catchup" and len(args) >= 2:
            mode = "summary" if args[1].lower().startswith(("resum", "sum")) else "full"
            await _save(update, context, catchup_mode=mode)
        else:
            await cmd_recordatorios(update, context)
            return
        row = await _settings_row(update, context, fresh=True)
        if row is not None:
            await update.effective_message.reply_text(f"Hecho. {settings_summary(row)}")
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


def settings_summary(row: UserSettings) -> str:
    catchup_label = "resumen" if row.catchup_mode == "summary" else "todo"
    quiet = (
        f"{row.quiet_start:02d}:00-{row.quiet_end:02d}:00"
        if row.quiet_start is not None and row.quiet_end is not None
        else "sin horas de silencio"
    )
    return (
        f"Resumen diario: {'ON' if row.digest_enabled else 'OFF'} a las {row.digest_hour:02d}:00; "
        f"silencio: {quiet}; catch-up: {catchup_label}."
    )


async def _settings_row(update: Update, context: ContextTypes.DEFAULT_TYPE, fresh: bool = False) -> UserSettings | None:
    from app.database.models import User

    if update.effective_user is None:
        return None
    ctx = context.bot_data["ctx"]
    async with ctx.session_factory() as session:
        user = (
            await session.execute(sa.select(User).where(User.telegram_user_id == update.effective_user.id))
        ).scalar_one_or_none()
        if user is None:
            return None
        repo = SettingsRepository(session)
        row = await repo.get_or_create(user.id)
        await session.commit()
        session.expunge(row)
        return row


async def _save(update: Update, context: ContextTypes.DEFAULT_TYPE, **values) -> None:
    from app.database.models import User

    if update.effective_user is None:
        return
    ctx = context.bot_data["ctx"]
    async with ctx.session_factory() as session:
        user = (await session.execute(sa.select(User).where(User.telegram_user_id == update.effective_user.id))).scalar_one_or_none()
        if user is None:
            return
        repo = SettingsRepository(session)
        await repo.update(user.id, **values)
        await session.commit()


def register_recordatorios(app: Application) -> None:
    app.add_handler(CommandHandler("ajustes", cmd_ajustes), group=0)
    app.add_handler(CommandHandler("recordatorios", recordatorios_args), group=0)
