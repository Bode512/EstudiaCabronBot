"""Global error handler and nav callbacks routed to their features."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    ContextTypes,
)

from app.services.notifications import PermanentNotifierError, TransientNotifierError

logger = logging.getLogger(__name__)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, ApplicationHandlerStop):
        raise err
    if isinstance(err, TransientNotifierError):
        logger.warning("Telegram temporal: %s; reintentando el scheduler mas tarde", err)
        return
    if isinstance(err, PermanentNotifierError):
        logger.error("Telegram definitivo: %s", err)
    else:
        logger.exception("error no manejado", exc_info=err)
    try:
        if isinstance(update, Update) and update.effective_message is not None:
            await update.effective_message.reply_text("Ha fallado algo inesperado; no se ha perdido nada de la base de datos.")
    except Exception:
        pass


def register_error(app: Application) -> None:
    app.add_error_handler(global_error_handler)
