"""Notifications: the single port through which reminders reach Telegram (mock-friendly)."""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class NotifierError(Exception):
    message = "notifier error"


class TransientNotifierError(NotifierError):
    def __init__(self, kind: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds


class PermanentNotifierError(NotifierError):
    def __init__(self, kind: str = "unknown") -> None:
        super().__init__(kind)
        self.kind = kind


def classify_telegram_error(exc: Exception) -> tuple[str, bool, float | None]:
    """Returns (kind, transient, retry_after_seconds)."""
    text = str(exc).lower()
    retry_after = getattr(exc, "retry_after", None)
    if "429" in str(exc) or "flood control" in text or "too many requests" in text:
        seconds = float(retry_after) if retry_after else 30.0
        return "rate_limited", True, seconds
    if "403" in str(exc) or "forbidden" in text:
        return "blocked", False, None
    if "chat not found" in text or "chat_id is empty" in text:
        return "chat_not_found", False, None
    if "timeout" in text or "network" in text or "timed out" in text or "connection" in text:
        return "network", True, None
    return "unknown", True, None


class NullNotifier:
    """Test/fallback notifier: records sends in memory, never touches the network."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_next: int = 0
        self.permanent_fail_next: int = 0

    async def send(self, chat_id: int, text: str) -> int:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TransientNotifierError("network", 0.1)
        if self.permanent_fail_next > 0:
            self.permanent_fail_next -= 1
            raise PermanentNotifierError("blocked")
        self.sent.append((chat_id, text))
        return len(self.sent)


class TelegramNotifier:
    """Wraps python-telegram-bot send_message with typed error handling + soft rate limiting."""

    def __init__(self, bot, max_per_minute: int = 20) -> None:
        self.bot = bot
        self.max_per_minute = max_per_minute
        self._lock = asyncio.Lock()
        self._window_start = 0.0
        self._sent_in_window = 0

    async def _respect_rate_limit(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now - self._window_start >= 60:
                self._window_start = now
                self._sent_in_window = 0
            if self._sent_in_window >= self.max_per_minute:
                wait = 60.0 - (now - self._window_start)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._window_start = time.monotonic()
                self._sent_in_window = 0
            self._sent_in_window += 1

    async def send(self, chat_id: int, text: str) -> int:
        await self._respect_rate_limit()
        try:
            message = await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return message.message_id
        except Exception as exc:
            kind, transient, retry_after = classify_telegram_error(exc)
            if transient:
                raise TransientNotifierError(kind, retry_after) from exc
            raise PermanentNotifierError(kind) from exc
