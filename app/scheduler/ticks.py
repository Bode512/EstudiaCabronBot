"""Periodic scheduler jobs. Callbacks receive the PTB JobQueue callback context."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram.ext import ContextTypes

from app.utils.timezones import now_utc

logger = logging.getLogger(__name__)


async def reminder_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every 30 seconds: claim & send due reminders."""
    try:
        ctx = context.application.bot_data["ctx"]
        stats = await ctx.reminders.process_due_batch()
        if stats.sent or stats.failed or stats.expired:
            logger.info("reminders tick: %s", stats)
    except Exception:
        logger.exception("reminder tick failed")


async def digest_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly pass: each user's digest fires once per day at their digest_hour (their tz)."""
    try:
        ctx = context.application.bot_data["ctx"]
        count = await ctx.reminders.run_digest_pass()
        if count:
            logger.info("digest pass: %d resumenes enviados", count)
    except Exception:
        logger.exception("digest tick failed")


async def backup_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.backups import hot_copy, integrity_check, rotate_backups

    ctx = context.application.bot_data["ctx"]
    db_path = ctx.settings.db_path
    backup_dir = db_path.parent / "backups"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"estudia_{stamp}.db"
    try:
        if hot_copy(db_path, target):
            check = integrity_check(target)
            if check != "ok":
                logger.error("backup con integridad sospechosa: %s", check)
            rotate_backups(backup_dir, keep=14)
    except Exception:
        logger.exception("backup tick failed")


def _stamp_now() -> str:
    return now_utc().isoformat()
