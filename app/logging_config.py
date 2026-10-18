"""Structured logging setup. Never logs tokens or message contents."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_SECRET_KEYS = ("token", "api_key", "authorization", "password", "secret")


class SecretFilter(logging.Filter):
    """Defensive: scrub accidental secret-looking values from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        lowered = msg.lower()
        for key in _SECRET_KEYS:
            if key in lowered and "=" in msg:
                record.msg = "[REDACTED: line contained a secret-looking value]"
                record.args = ()
                return True
        return True


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=DATE_FORMAT, handlers=handlers)

    # python-telegram-bot is very chatty at DEBUG
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    secret_filter = SecretFilter()
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(secret_filter)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
