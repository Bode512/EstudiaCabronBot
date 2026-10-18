"""Startup recovery: backups dir exists, migrations current, outbox repaired."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_storage_dirs(settings) -> list[str]:
    """Create working dirs; returns human warnings (never raises)."""
    warnings: list[str] = []
    for d in (settings.db_path.parent, settings.storage_dir, settings.db_path.parent / "backups", Path("logs")):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            warnings.append(f"No pude crear el directorio {d}: {exc}")
    return warnings


async def run_startup_recovery(reminders_service, ctx) -> dict:
    """Runs alembic migrations, hinges the outbox to a sane state, purges stale rows.

    Returns a summary dict for the startup Telegram report (best-effort, never raises).
    """
    summary: dict = {}
    from alembic import command as alembic_command
    from alembic.config import Config

    # 1) alembic upgrade head (env.py resolves the URL from app settings)
    try:
        cfg = Config("alembic.ini")
        alembic_command.upgrade(cfg, "head")
        summary["migrations"] = "al dia"
    except Exception as exc:
        logger.exception("migraciones fallidas")
        summary["migrations"] = f"FALLO: {exc}"
        return summary

    # 2) outbox recovery: stuck processing -> retryable; overdue -> grouped catch-up
    try:
        stuck = await reminders_service.recover_stuck_processing()
        summary["stuck_recovered"] = stuck
        flushed = await reminders_service.flush_overdue_queue()
        summary["overdue_flushed"] = flushed
    except Exception as exc:
        logger.exception("outbox recovery failed")
        summary["outbox"] = f"FAILED: {exc}"
        return summary

    # 3) purge ancient history (120d retention policy)
    try:
        summary["purged"] = await reminders_service.purge_old()
    except Exception as exc:
        logger.exception("purge failed")
        summary["purge"] = f"FAILED: {exc}"
    return summary
