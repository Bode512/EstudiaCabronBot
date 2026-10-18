"""User services: registration, profile, account deletion, access control helpers."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.database.models import AccountStatus, User
from app.database.repositories import AuditRepository, SettingsRepository, UserRepository
from app.utils.timezones import now_utc

logger = logging.getLogger(__name__)


class UserService:
    def __init__(self, session_factory: async_sessionmaker, settings: Settings | None = None) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    async def resolve_user(self, session: AsyncSession, telegram_user_id: int) -> User | None:
        repo = UserRepository(session)
        user = await repo.get_by_telegram_id(telegram_user_id)
        if user and user.status == AccountStatus.ACTIVE:
            await repo.touch(user.id)
        return user

    async def get_user_by_telegram(self, telegram_user_id: int) -> User | None:
        async with self.session_factory() as session:
            user = await UserRepository(session).get_by_telegram_id(telegram_user_id)
            if user and user.status == AccountStatus.ACTIVE:
                return user
            return None

    async def is_registered(self, telegram_user_id: int) -> bool:
        async with self.session_factory() as session:
            user = await UserRepository(session).get_by_telegram_id(telegram_user_id)
            return bool(user and user.status == AccountStatus.ACTIVE)

    async def register(
        self,
        telegram_user_id: int,
        name: str,
        username: str | None = None,
        chat_id: int | None = None,
        timezone: str | None = None,
    ) -> tuple[User, bool]:
        """Returns (user, created). Idempotent for existing active users."""
        tz = timezone or self.settings.default_timezone
        async with self.session_factory() as session:
            repo = UserRepository(session)
            existing = await repo.get_by_telegram_id(telegram_user_id)
            if existing is not None:
                if existing.status == AccountStatus.ACTIVE:
                    await repo.touch(existing.id, chat_id=chat_id)
                    return existing, False
                # reactivate previously deleted user
                await repo.set_status(existing.id, AccountStatus.ACTIVE)
                updated = await repo.update(
                    existing.id, name=name.strip()[:120], chat_id=chat_id, timezone=tz
                )
                await session.commit()
                assert updated is not None
                return updated, True
            if not name or not name.strip():
                raise ValueError("El nombre no puede estar vacio.")
            user = await repo.create(
                telegram_user_id,
                name=name,
                username=username,
                chat_id=chat_id,
                timezone=tz,
            )
            await SettingsRepository(session).get_or_create(user.id)
            await session.commit()
            logger.info("nuevo usuario registrado id=%s tz=%s", user.id, user.timezone)
            return user, True

    async def update_profile(self, user_id: int, **values) -> None:
        async with self.session_factory() as session:
            await UserRepository(session).update(user_id, **values)
            await session.commit()

    async def get_user(self, user_id: int) -> User | None:
        async with self.session_factory() as session:
            return await UserRepository(session).get_by_id(user_id)

    async def set_timezone(self, user_id: int, tz: str) -> None:
        from app.utils.timezones import get_zone

        get_zone(tz)  # validate
        await self.update_profile(user_id, timezone=tz)

    async def update_settings(self, user_id: int, **values) -> None:
        async with self.session_factory() as session:
            await SettingsRepository(session).update(user_id, **values)
            await session.commit()

    async def get_settings(self, user_id: int):
        async with self.session_factory() as session:
            return await SettingsRepository(session).get_or_create(user_id)

    async def delete_account(self, user_id: int) -> bool:
        """Hard delete with cascade; retains nothing (backups expire per policy)."""
        async with self.session_factory() as session:
            repo = UserRepository(session)
            user = await repo.get_by_id(user_id)
            if not user:
                return False
            deleted = await repo.hard_delete(user_id)
            await session.commit()
        logger.warning("cuenta eliminada user_id=%s filas=%s", user_id, deleted)
        return deleted == 1

    async def detail_for_report(self, user_id: int) -> dict:
        async with self.session_factory() as session:
            user = await UserRepository(session).get_by_id(user_id)
            audit = await AuditRepository(session).list_recent(user_id, limit=1)
        if not user:
            return {}
        return {
            "id": user.id,
            "name": user.name,
            "timezone": user.timezone,
            "registered": user.created_at.isoformat() if user.created_at else None,
            "last_audit": audit[0].created_at.isoformat() if audit else None,
            "now_utc": now_utc().isoformat(),
        }
