"""start.py: /start (registration), /help, /privacidad, /perfil, /cancelar, /borrarme."""

from __future__ import annotations

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.bot.keyboards import delete_account_keyboard, main_menu_keyboard, yes_no_keyboard
from app.bot.util import safe

ASK_NAME = 0


@safe
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if update.effective_user is None or update.effective_chat is None or update.effective_message is None:
        return ConversationHandler.END
    ctx = context.bot_data["ctx"]
    telegram_user = update.effective_user
    existing = await ctx.users.get_user_by_telegram(telegram_user.id)
    if existing:
        await update.effective_message.reply_html(
            "Ya estas registrado. Escribe /hoy para ver tu dia o /help para todo el menu."
        )
        return ConversationHandler.END
    await update.effective_message.reply_html(
        "Hola! Soy <b>EstudiaCabronBot</b>, tu asistente academico para 2.o de Bachillerato.\n\n"
        "Como quieres que te llame? (Por ejemplo: Bode)"
    )
    context.user_data["chat_id"] = update.effective_chat.id
    return ASK_NAME


@safe
async def on_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user is None or update.effective_message is None:
        return ConversationHandler.END
    name = (update.effective_message.text or "").strip()
    if not name or len(name) > 60:
        await update.effective_message.reply_text("Nombre demasiado largo o vacio. Dime otro (o /cancelar).")
        return ASK_NAME
    ctx = context.bot_data["ctx"]
    telegram_user = update.effective_user
    user, created = await ctx.users.register(
        telegram_user.id,
        name,
        username=telegram_user.username,
        chat_id=context.user_data.get("chat_id"),
    )
    context.user_data.pop("chat_id", None)
    if created:
        await update.effective_message.reply_html(
            f"Registrado como <b>{user.name}</b>. Zona horaria: {user.timezone}.\n\n"
            "Prueba: <code>Tengo un examen de Historia el jueves</code> o /anadir para crear registros paso a paso.",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await update.effective_message.reply_html("Tu cuenta vuelve a estar activa.")
    return ConversationHandler.END


@safe
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    if not await _ensure(update, context):
        return
    await update.effective_message.reply_html(
        "<b>Comandos</b>\n"
        "/hoy - resumen de tu dia\n"
        "/semana - lo que viene en 7 dias\n"
        "/examenes - gestionar examenes\n"
        "/tareas - gestionar tareas\n"
        "/anadir - crear examen o tarea con pasos\n"
        "/archivos - importar documentos (PDF, DOCX, TXT o fotos)\n"
        "/plan - generar plan de estudio semanal\n"
        "/disponibilidad - tus horas libres por dia\n"
        "/recordatorios - configurar avisos\n"
        "/progreso - lo que ya has hecho\n"
        "/perfil - tu ficha y zona horaria\n"
        "/privacidad - datos y borrado de cuenta\n"
        "/cancelar - cancela cualquier operacion en marcha\n\n"
        "Tambien puedes escribir con naturalidad:\n"
        "<i>\"Tengo un examen de Historia el jueves\"</i>\n"
        "<i>\"Añade un trabajo de Filosofia para el lunes\"</i>\n"
        "<i>\"He terminado el tema 3 de Biologia\"</i>\n"
        "<i>\"Cambia el examen de Fisica al viernes\"</i>"
    )


@safe
async def cmd_privacidad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure(update, context):
        return
    await update.effective_message.reply_html(
        "<b>Privacidad</b>\n"
        "- Guardo: nombre, ID de Telegram, zona horaria y tus datos academicos (examenes, tareas, sesiones, documentos que subas).\n"
        "- No giro a pedir: direccion, telefono, fecha de nacimiento ni documentos de identidad.\n"
        "- La IA esta desactivada por defecto; si la activas, solo se envian extractos necesarias para procesar tu peticion.\n"
        "- Copias de seguridad locales pueden contener tus datos hasta que caduquen por la politica de retencion.\n\n"
        "El boton siguiente borra TODA tu cuenta y datos academicos (irreversible; los backups antiguos caducan solos).",
        reply_markup=yes_no_keyboard("acct", 0, "delete_prompt"),
    )


@safe
async def delete_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Second confirmation for full account deletion (destructive)."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    await query.message.reply_text(
        "Confirma FINAL: se borraran todos tus examenes, tareas, sesiones, archivos y recordatorios.",
        reply_markup=delete_account_keyboard(),
    )


@safe
async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    user = await current_db_user(update, context)
    if user is None:
        await query.message.reply_text("No estas registrado.")
        return
    deleted = await self_destruct(update, context, user.id)
    if deleted:
        await query.message.reply_text("Cuenta y datos borrados. Adios.")
    else:
        await query.message.reply_text("No se pudo completar el borrado; se ha avisado por el log del sistema.")


@safe
async def cmd_borrarme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure(update, context):
        return
    await cmd_privacidad(update, context)


@safe
async def cmd_perfil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure(update, context):
        return
    user = await current_db_user(update, context)
    if user is None:
        return
    await update.effective_message.reply_html(
        f"<b>Perfil</b>\nNombre: {user.name}\nUsuario Telegram: @{user.username or '(sin nombre de usuario)'}\n"
        f"Zona horaria: {user.timezone}\nNivel: 2.o de Bachillerato\n"
        "Cambia tu zona horaria escribiendo: <code>pais tz [Region/Ciudad]</code> (ej. <code>tz Europe/Madrid</code>)"
    )


@safe
async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.effective_message is not None and not await _is_conversation(update, context):
        await update.effective_message.reply_text("Cancelado.")
    return ConversationHandler.END


async def _is_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return False


async def _ensure(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Ensure registered + touch chat id; reply guidance if missing."""
    if update.effective_user is None or update.effective_message is None:
        return False
    ctx = context.bot_data["ctx"]
    if not await ctx.users.is_registered(update.effective_user.id):
        await update.effective_message.reply_text("No estas registrado. Escribe /start.")
        return False
    return True


async def current_db_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import sqlalchemy as sa

    from app.database.models import User

    ctx = context.bot_data["ctx"]
    if update.effective_user is None:
        return None
    async with ctx.session_factory() as session:
        result = await session.execute(sa.select(User).where(User.telegram_user_id == update.effective_user.id))
        return result.scalar_one_or_none()


async def self_destruct(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    ctx = context.bot_data["ctx"]
    return await ctx.users.delete_account(user_id)


@safe
async def on_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """'tz Europe/Lisbon' -> change timezone (consequential assumptions confirmed)."""
    text = (update.effective_message.text or "").strip()
    parts = text.split()
    if len(parts) != 2 or parts[0].lower() != "tz":
        return
    tz = parts[1]
    try:
        from app.utils.timezones import get_zone

        get_zone(tz)
    except Exception:
        await update.effective_message.reply_text("Zona horaria invalida.")
        return
    user = await _ensure_user(update, context)
    if user is None:
        return
    ctx = context.bot_data["ctx"]
    await ctx.users.set_timezone(user.id, tz)
    await update.effective_message.reply_text(f"Zona horaria actualizada a {tz}.")


async def _ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await current_db_user(update, context)
    if user is None:
        if update.effective_message:
            await update.effective_message.reply_text("No estas registrado. Escribe /start.")
    return user


def register_start_handlers(app: Application) -> None:
    conn = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_name)]},
        fallbacks=[CommandHandler("cancelar", cmd_cancelar)],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(conn, group=0)
    app.add_handler(CommandHandler("help", cmd_help), group=0)
    app.add_handler(CommandHandler("privacidad", cmd_privacidad), group=0)
    app.add_handler(CommandHandler("borrarme", cmd_borrarme), group=0)
    app.add_handler(CommandHandler("perfil", cmd_perfil), group=0)
    app.add_handler(CommandHandler("cancelar", cmd_cancelar), group=0)
    app.add_handler(MessageHandler(filters.Regex(r"^tz \S+") & ~filters.COMMAND, on_tz), group=1)
