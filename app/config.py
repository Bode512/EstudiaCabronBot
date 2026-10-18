"""Application configuration, loaded from environment / .env (never hardcode secrets)."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_owner_ids(value: str | list[str] | list[int] | None) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        items: list[str] = [str(v) for v in value]
    else:
        items = re.split(r"[,;\s]+", str(value).strip())
    out: list[int] = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        try:
            out.append(int(item))
        except ValueError as exc:
            raise ValueError(f"OWNER_TELEGRAM_IDS debe contener IDs numericos: {item!r}") from exc
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- required ---
    telegram_token: SecretStr = Field(default="", alias="TELEGRAM_BOT_TOKEN")

    # --- identity / access ---
    owner_telegram_ids_raw: str = Field(default="", alias="OWNER_TELEGRAM_IDS")
    default_timezone: str = Field(default="Europe/Madrid", alias="DEFAULT_TIMEZONE")

    # --- database ---
    database_url: str = Field(default="sqlite:///data/app.db", alias="DATABASE_URL")

    # --- paths ---
    data_dir: Path = PROJECT_ROOT / "data"
    storage_dir: Path = PROJECT_ROOT / "storage"
    backups_dir: Path = PROJECT_ROOT / "backups"

    # --- logging ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- reminder engine defaults ---
    catchup_mode: Literal["catch_up", "summary"] = "summary"
    catchup_max_backlog: int = Field(default=10, ge=1, le=100)
    reminder_max_retries: int = Field(default=5, ge=0, le=20)
    stuck_processing_seconds: int = Field(default=300, ge=30)
    tick_interval_seconds: int = Field(default=30, ge=5, le=3600)
    default_digest_hour: int = Field(default=8, ge=0, le=23)
    default_daily_availability_minutes: int = Field(default=120, ge=0, le=720)
    max_message_rate_per_minute: int = Field(default=20, ge=1, le=60)

    # --- backups ---
    backup_enabled: bool = True
    backup_hour: int = Field(default=3, ge=0, le=23)
    backup_retention: int = Field(default=14, ge=1, le=365)

    # --- AI providers (optional) ---
    ai_provider: str = Field(default="", alias="AI_PROVIDER")
    ai_api_key: SecretStr | None = Field(default=None, alias="AI_API_KEY")
    ai_model: str = Field(default="gpt-4o-mini", alias="AI_MODEL")
    ai_base_url: str | None = Field(default=None, alias="AI_BASE_URL")
    ai_timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("default_timezone")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"Zona horaria IANA invalida: {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _checkpaths(self) -> Settings:
        for d in (self.data_dir, self.storage_dir, self.backups_dir):
            Path(d).mkdir(parents=True, exist_ok=True)
        return self

    @property
    def owner_telegram_ids(self) -> list[int]:
        return _parse_owner_ids(self.owner_telegram_ids_raw)

    @property
    def is_configured(self) -> bool:
        return bool(self.telegram_token.get_secret_value().strip())

    @property
    def async_database_url(self) -> str:
        return _to_async_url(self.database_url)

    @property
    def sync_database_url(self) -> str:
        return _to_sync_url(self.database_url)

    @property
    def db_path(self) -> Path:
        """Return the sqlite file path of DATABASE_URL (None-ish for non-sqlite URLs)."""
        m = re.match(r"sqlite(?:\+[a-z0-9]+)?:///(?P<path>.+)", self.database_url)
        if not m:
            raise ValueError("Solo se soporta sqlite como motor de base de datos.")
        path = Path(m.group("path"))
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        return path

    @property
    def ai_provider_kind(self) -> str:
        p = (self.ai_provider or "none").strip().lower()
        return "openai" if p in ("openai", "openai_compatible") else "none"

    @property
    def ai_enabled(self) -> bool:
        return self.ai_provider_kind == "openai" and bool(
            self.ai_api_key and self.ai_api_key.get_secret_value().strip()
        )

    def config_warnings(self) -> list[str]:
        out: list[str] = []
        if not self.is_configured:
            out.append("TELEGRAM_BOT_TOKEN no definido: el bot no podra iniciar.")
        if self.db_path.parent != (PROJECT_ROOT / self.data_dir):
            # only warn if data dir is non-default outside root
            pass
        if self.ai_provider != "none" and not self.ai_enabled:
            out.append("AI_PROVIDER configurado pero falta AI_API_KEY: la IA quedara desactivada.")
        if not self.owner_telegram_ids:
            out.append("OWNER_TELEGRAM_IDS vacio: no habra comandos de administrador.")
        return out


def _to_async_url(url: str) -> str:
    if url.startswith("sqlite+"):
        return url
    return url.replace("sqlite://", "sqlite+aiosqlite://", 1) if url.startswith("sqlite://") else url


def _to_sync_url(url: str) -> str:
    return re.sub(r"^sqlite\+[a-z0-9]+://", "sqlite://", url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return _load_settings()


def _load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    get_settings.cache_clear()


def dump_settings_masked(s: Settings) -> str:
    """Safe representation for logs/diagnostics (secrets masked)."""
    return json.dumps(
        {
            "telegram_token": "***",
            "owner_ids": s.owner_telegram_ids,
            "database_url": s.sync_database_url,
            "default_timezone": s.default_timezone,
            "backup_enabled": s.backup_enabled,
            "ai_provider": "disabled" if not s.ai_enabled else s.ai_provider,
        },
        ensure_ascii=False,
    )
