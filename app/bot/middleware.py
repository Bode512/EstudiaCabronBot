"""Access middleware: every academic operation requires a registered active user."""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def require_registered(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Rejects updates from users not in the registry, with guidance to /start."""

    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user is None or update.effective_chat is None:
            return None
        ctx = context.bot_data["ctx"]
        user = await ctx.users.is_registered(update.effective_user.id)
        if not user:
            chat_type = update.effective_chat.type
            if chat_type and chat_type != "private":
                return None  # stay silent in groups to avoid spam
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="No estas registrado. Escribe /start para crear tu cuenta.",
            )
            return None
        return await fn(update, context, *args, **kwargs)

    return wrapper


def owner_only(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user is None:
            return None
        ctx = context.bot_data["ctx"]
        if not ctx.is_owner(update.effective_user.id):
            return None
        return await fn(update, context, *args, **kwargs)

    return wrapper
