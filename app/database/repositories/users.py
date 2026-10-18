"""User repository. All academic rows are user-scoped; identity is telegram_user_id."""

from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AccountStatus, AuditAction, AuditEvent, User, UserSettings
from app.utils.timezones import now_utc


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        stmt = select(User).where(User.telegram_user_id == telegram_user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> User | None:
        return (await self.session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()

    async def count(self) -> int:
        return int((await self.session.execute(select(func.count(User.id)))).scalar_one())

    async def create(
        self,
        telegram_user_id: int,
        name: str,
        username: str | None = None,
        chat_id: int | None = None,
        timezone: str | None = None,
    ) -> User:
        user = User(
            telegram_user_id=telegram_user_id,
            name=name.strip()[:120],
            username=(username or "").lstrip("@")[:64] or None,
            chat_id=chat_id,
            timezone=timezone or "Europe/Madrid",
            status=AccountStatus.ACTIVE,
        )
        user.settings = UserSettings(
            default_offsets_minutes={"exam": [10080, 4320, 1440, 60], "assignment": [1440, 60]},
            weekly_availability={},
        )
        self.session.add(user)
        await self.session.flush()
        self.session.add(
            AuditEvent(user_id=user.id, action=AuditAction.REGISTER, entity="user", entity_id=user.id, detail={"name": user.name})
        )
        return user

    async def touch(self, user_id: int, chat_id: int | None = None) -> None:
        values: dict = {"last_interaction_at": now_utc()}
        if chat_id:
            values["chat_id"] = chat_id
        await self.session.execute(update(User).where(User.id == user_id).values(**values))

    async def update(self, user_id: int, **values) -> User | None:
        allowed = {"name", "username", "timezone", "language", "status", "chat_id", "level"}
        values = {k: v for k, v in values.items() if k in allowed and v is not None}
        if values:
            await self.session.execute(update(User).where(User.id == user_id).values(**values))
        return await self.get_by_id(user_id)

    async def set_status(self, user_id: int, status: AccountStatus) -> None:
        await self.session.execute(update(User).where(User.id == user_id).values(status=status))

    async def hard_delete(self, user_id: int) -> int:
        """Cascades: settings, subjects, exams, assignments, topics, sessions, plans, documents, reminders, audit."""
        res = await self.session.execute(delete(User).where(User.id == user_id))
        return int(res.rowcount or 0)

    async def all_active(self) -> list[User]:
        stmt = select(User).where(User.status == AccountStatus.ACTIVE)
        return list((await self.session.execute(stmt)).scalars())
