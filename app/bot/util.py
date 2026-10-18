"""Shared handler helpers: user resolution, session creation, safe wrappers."""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def current_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch the DB user by telegram id (already validated by middleware)."""
    from app.database.models import User

    ctx = context.bot_data["ctx"]
    if update.effective_user is None:
        return None
    async with ctx.session_factory() as session:
        result = await session.execute(
            __import__("sqlalchemy").select(User).where(User.telegram_user_id == update.effective_user.id)
        )
        user = result.scalar_one_or_none()
        return user


def safe(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Catch unexpected errors so a handler crash never kills the process/reply loop."""

    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await fn(update, context, *args, **kwargs)
        except Exception:
            logger.exception("handler error in %s", fn.__name__)
            if update.effective_chat:
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="Se produjo un error interno. Prueba de nuevo o usa /cancelar.",
                    )
                except Exception:
                    logger.exception("no se pudo avisar del error al usuario")
            return None

    return wrapper
