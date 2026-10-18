"/estado: owner-only diagnostics (counts + scheduler/queue health)."

from __future__ import annotations

import logging

import sqlalchemy as sa
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.bot.middleware import owner_only
from app.config import get_settings
from app.database.models import (
    Assignment,
    Document,
    Exam,
    Reminder,
    StudySession,
    User,
)

logger = logging.getLogger(__name__)


def _counts(label: str, values: dict[str, int]) -> str:
    inner = ", ".join(f"{k}={v}" for k, v in values.items())
    return f"<b>{label}</b>: {inner}"


def _state_dict(raw: dict) -> dict[str, int]:
    return {str(getattr(k, "value", k)): int(v) for k, v in raw.items()}


@owner_only
async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    ctx = context.bot_data["ctx"]
    settings = get_settings()
    reply = update.effective_message.reply_html
    async with ctx.session_factory() as session:
        users_total = (await session.execute(sa.select(sa.func.count()).select_from(User))).scalar_one()
        users_active = (
            await session.execute(sa.select(sa.func.count()).select_from(User).where(User.deleted_at.is_(None)))
        ).scalar_one()
        exam_status = _state_dict(
            dict((await session.execute(sa.select(Exam.status, sa.func.count()).group_by(Exam.status))).all())
        )
        assign_status = _state_dict(
            dict((await session.execute(sa.select(Assignment.status, sa.func.count()).group_by(Assignment.status))).all())
        )
        session_status = _state_dict(
            dict((await session.execute(sa.select(StudySession.status, sa.func.count()).group_by(StudySession.status))).all())
        )
        doc_status = _state_dict(
            dict((await session.execute(sa.select(Document.status, sa.func.count()).group_by(Document.status))).all())
        )
        reminders_state = _state_dict(
            dict((await session.execute(sa.select(Reminder.status, sa.func.count()).group_by(Reminder.status))).all())
        )
    overdue_queue = reminders_state.get("pending", 0) + reminders_state.get("processing", 0)
    text = "<b>Estado del bot</b>\n"
    text += _counts("Usuarios", {"total": users_total, "activos": users_active}) + "\n"
    text += _counts("Examenes", exam_status) + "\n"
    text += _counts("Tareas", assign_status) + "\n"
    text += _counts("Sesiones estudio", session_status) + "\n"
    text += _counts("Documentos", doc_status) + "\n"
    text += _counts("Recordatorios (outbox)", reminders_state) + "\n"
    text += f"Atraso por enviar ahora: {overdue_queue}\n"
    text += f"AI: {settings.ai_provider_kind}\n"
    text += f"Zona por defecto: {settings.default_timezone} | Catch-up: {settings.catchup_mode} (max {settings.catchup_max_backlog})"
    try:
        import sqlite3

        con = sqlite3.connect(settings.db_path)
        con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        ok = "OK"
    except Exception as exc:
        ok = f"FALLO: {exc}"
    text += f"\nSQLite: {ok}"
    await reply(text)


def register_admin(app: Application) -> None:
    app.add_handler(CommandHandler("estado", cmd_estado), group=0)
