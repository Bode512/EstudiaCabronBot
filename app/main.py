"""Entry point. Async startup: logging -> recovery -> Telegram app + schedules -> polling."""

from __future__ import annotations

import logging
from datetime import time as dtime
from pathlib import Path

from telegram.ext import Application, ApplicationBuilder

from app.bot.context import attach_notifier, build_context
from app.config import get_settings
from app.logging_config import setup_logging
from app.services.notifications import TelegramNotifier

logger = logging.getLogger(__name__)


def register_all(app: Application, ctx) -> None:
    from app.bot.handlers.anadir import register_anadir
    from app.bot.handlers.error import register_error
    from app.bot.handlers.estado import register_admin
    from app.bot.handlers.files import register_files
    from app.bot.handlers.nl_router import register_nl
    from app.bot.handlers.plan import register_plan
    from app.bot.handlers.recordatorios import register_recordatorios
    from app.bot.handlers.start import register_start_handlers
    from app.bot.handlers.views import register_views

    register_start_handlers(app)
    register_anadir(app)
    register_files(app)
    register_plan(app)
    register_recordatorios(app)
    register_admin(app)
    register_nl(app)
    register_views(app)
    register_error(app)


async def _notify_owner(app: Application, ctx, text: str) -> None:
    for chat_id in ctx.owner_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("enviando informe al owner %s", chat_id)


async def post_init(app: Application) -> None:
    ctx = app.bot_data["ctx"]
    attach_notifier(ctx, TelegramNotifier(app.bot, ctx.settings.max_message_rate_per_minute))
    await app.bot.set_my_commands(
        [
            ("start", "Registro e inicio"),
            ("help", "Ayuda y comandos"),
            ("hoy", "Que tengo hoy"),
            ("semana", "Proximos 7 dias"),
            ("examenes", "Examenes activos"),
            ("tareas", "Tareas pendientes"),
            ("anadir", "Crear examen/tarea guiado"),
            ("archivos", "Como subir PDF/fotos"),
            ("plan", "Plan de estudio propuesto"),
            ("disponibilidad", "Minutos de estudio por dia"),
            ("recordatorios", "Configurar avisos"),
            ("progreso", "Progreso acumulado"),
            ("perfil", "Ver tu perfil y zona horaria"),
            ("borrarme", "Eliminar tu cuenta"),
            ("cancelar", "Cancelar la operacion actual"),
        ]
    )
    from app.scheduler.recovery import ensure_storage_dirs, run_startup_recovery
    from app.scheduler.ticks import backup_tick, digest_tick, reminder_tick

    for w in ensure_storage_dirs(ctx.settings):
        logger.warning("arranque: %s", w)
    summary = await run_startup_recovery(ctx.reminders, ctx)
    logger.info("recovery: %s", summary)
    job_queue = app.job_queue
    if job_queue is None:
        logger.error("job_queue no disponible: no se planifican ticks")
    else:
        job_queue.run_repeating(reminder_tick, interval=30, first=10, name="reminders")
        job_queue.run_repeating(digest_tick, interval=3600, first=120, name="digest")
        job_queue.run_daily(backup_tick, time=dtime(hour=4, minute=30), name="backup")
    lines = [f"{k}: {v}" for k, v in summary.items() if v]
    text = "<b>Sistema encendido y sincronizado</b>\n" + "\n".join(lines)
    atraso = summary.get("overdue_flushed") or 0
    await _notify_owner(app, ctx, text + f"\nAtraso recuperado: {atraso} recordatorios")


def main() -> None:
    from app.config import dump_settings_masked

    settings = get_settings()
    setup_logging(log_file=Path("logs") / "estudia_bot.log")
    logger.info("config: %s", dump_settings_masked(settings))
    if not settings.is_configured:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en .env")
    ctx = build_context(settings)
    app = (
        ApplicationBuilder()
        .token(settings.telegram_token.get_secret_value())
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    app.bot_data["ctx"] = ctx
    register_all(app, ctx)
    app.run_polling(allowed_updates=["message", "callback_query", "edited_message"])


if __name__ == "__main__":
    main()
