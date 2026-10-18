"""User settings repository (1:1 with users)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database.models import UserSettings


def _defaults() -> dict:
    s = get_settings()
    return {
        "digest_enabled": True,
        "digest_hour": s.default_digest_hour,
        "quiet_start": None,
        "quiet_end": None,
        "catchup_mode": s.catchup_mode,
        "max_backlog": s.catchup_max_backlog,
        "max_notifications_per_day": s.max_message_rate_per_minute,
        "default_offsets_minutes": {"exam": [10080, 4320, 1440, 60], "assignment": [1440, 60]},
        "weekly_availability": {},
        "daily_availability_minutes": s.default_daily_availability_minutes,
        "study_day_start_hour": 16,
        "study_day_end_hour": 22,
        "_config_timezone": s.default_timezone,
    }


class SettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(self, user_id: int) -> UserSettings:
        row = (await self.session.execute(select(UserSettings).where(UserSettings.user_id == user_id))).scalar_one_or_none()
        if row is not None:
            return row
        d = _defaults()
        row = UserSettings(user_id=user_id, **{k: v for k, v in d.items() if not k.startswith("_")})
        self.session.add(row)
        await self.session.flush()
        return row

    async def update(self, user_id: int, **values) -> UserSettings:
        row = await self.get_or_create(user_id)
        allowed = {
            "digest_enabled", "digest_hour", "quiet_start", "quiet_end", "catchup_mode",
            "max_backlog", "max_notifications_per_day", "default_offsets_minutes",
            "weekly_availability", "daily_availability_minutes",
            "study_day_start_hour", "study_day_end_hour",
        }
        for k, v in values.items():
            if k in allowed and v is not None:
                setattr(row, k, v)
        await self.session.flush()
        return row
