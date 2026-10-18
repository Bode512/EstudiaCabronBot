"""Optional AI provider abstraction. The bot is 100% functional with no provider.

The provider can only:
- return structured suggestions that callers validate with pydantic schemas; or
- produce explanatory text.

It can NEVER touch the database, files, tokens or Telegram API. All outputs are
advisory inputs to the same service-layer validation paths used without AI.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class AIResponse:
    ok: bool
    data: dict | None = None
    error: str | None = None


class AIProvider(ABC):
    name = "base"

    @abstractmethod
    async def complete_json(self, system: str, user_prompt: str) -> dict | None:
        """Return parsed JSON dict or None (never raises)."""

    async def explain(self, prompt: str) -> str | None:
        return None


class NullAIProvider(AIProvider):
    name = "none"

    async def complete_json(self, system: str, user_prompt: str) -> dict | None:
        return None

    async def explain(self, prompt: str) -> str | None:
        return None


class OpenAICompatibleProvider(AIProvider):
    """Works with OpenAI and any OpenAI-compatible endpoint (base_url)."""

    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str | None = None, timeout: int = 30) -> None:
        import httpx  # python-telegram-bot already depends on httpx; no new heavy deps

        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.timeout = timeout
        self._httpx = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout,
        )

    async def complete_json(self, system: str, user_prompt: str) -> dict | None:
        try:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }
            response = await self._httpx.post(f"{self.base_url}/chat/completions", json=payload)
            if response.status_code == 429:
                logger.warning("AI provider rate-limited (429)")
                return None
            if response.status_code >= 400:
                logger.warning("AI provider HTTP %s", response.status_code)
                return None
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return _extract_json_object(content)
        except Exception as exc:
            logger.warning("AI provider fallo: %s", type(exc).__name__)
            return None

    async def explain(self, prompt: str) -> str | None:
        try:
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "max_tokens": 500,
            }
            response = await self._httpx.post(f"{self.base_url}/chat/completions", json=payload)
            if response.status_code >= 400:
                return None
            data = response.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content")
        except Exception:
            logger.warning("AI explain fallo", exc_info=True)
            return None

    async def aclose(self) -> None:
        await self._httpx.aclose()


def _extract_json_object(content: str) -> dict | None:
    """Best-effort JSON object extraction from model output."""
    if not content:
        return None
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`\n ")
        if content.lower().startswith("json"):
            content = content[4:]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(content[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def build_ai_provider(settings: Settings | None = None) -> AIProvider:
    settings = settings or get_settings()
    if not settings.ai_enabled:
        return NullAIProvider()
    assert settings.ai_api_key is not None
    try:
        return OpenAICompatibleProvider(
            api_key=settings.ai_api_key.get_secret_value(),
            model=settings.ai_model,
            base_url=settings.ai_base_url,
            timeout=settings.ai_timeout_seconds,
        )
    except Exception:
        logger.exception("no se pudo inicializar el proveedor de IA; se desactiva")
        return NullAIProvider()


# ---------------------------------------------------------------- structured helpers
def validate_intent_payload(payload: dict | None) -> dict | None:
    """Validate AI NL-intent output; supports 'exam' | 'assignment' | 'reschedule' | None."""
    if not isinstance(payload, dict):
        return None
    intent = payload.get("intent")
    if intent not in ("exam", "assignment", "reschedule", "unknown", None):
        return None
    result = {"intent": intent or "unknown"}
    for key in ("subject", "title", "date_text", "description"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()[:200]
    return result  # handlers still run the deterministic date parser on date_text
