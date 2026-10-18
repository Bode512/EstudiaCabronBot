"""Subjects + audit repository."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AuditAction, AuditEvent, Subject, utcnow


def norm(name: str) -> str:
    return " ".join(
        (name or "")
        .lower()
        .replace("á", "a").replace("é", "e").replace("í", "i")
        .replace("ó", "o").replace("ú", "u").replace("ü", "u")
        .split()
    )


class SubjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_by_user(self, user_id: int) -> list[Subject]:
        stmt = select(Subject).where(Subject.user_id == user_id).order_by(Subject.name)
        return list((await self.session.execute(stmt)).scalars())

    async def get_owned(self, user_id: int, subject_id: int) -> Subject | None:
        stmt = select(Subject).where(Subject.id == subject_id, Subject.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_by_name(self, user_id: int, name: str) -> Subject | None:
        stmt = select(Subject).where(Subject.user_id == user_id, Subject.name_norm == norm(name))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_create(self, user_id: int, name: str) -> Subject:
        existing = await self.find_by_name(user_id, name)
        if existing:
            return existing
        display = " ".join(name.split())[:80]
        subject = Subject(user_id=user_id, name=display, name_norm=norm(name)[:80])
        self.session.add(subject)
        await self.session.flush()
        return subject

    async def delete_owned(self, user_id: int, subject_id: int) -> bool:
        subject = await self.get_owned(user_id, subject_id)
        if not subject:
            return False
        await self.session.delete(subject)
        return True


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, user_id: int, action: AuditAction, entity: str, entity_id: int | None = None, detail: dict | None = None) -> None:
        self.session.add(
            AuditEvent(user_id=user_id, action=action, entity=entity, entity_id=entity_id, detail=detail or {}, created_at=utcnow())
        )

    async def list_recent(self, user_id: int, limit: int = 20):
        stmt = select(AuditEvent).where(AuditEvent.user_id == user_id).order_by(AuditEvent.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def purge(self, user_id: int) -> None:
        await self.session.execute(delete(AuditEvent).where(AuditEvent.user_id == user_id))
