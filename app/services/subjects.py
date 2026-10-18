"""Subjects service (thin wrapper to keep ownership checks in one place)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Subject
from app.database.repositories import SubjectRepository
from app.services.errors import OwnershipError


class SubjectService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure(self, user_id: int, name: str) -> Subject:
        repo = SubjectRepository(self.session)
        clean = " ".join((name or "").split())
        if not clean:
            raise ValueError("Nombre de asignatura vacio.")
        if len(clean) > 80:
            clean = clean[:80]
        return await repo.get_or_create(user_id, clean)

    async def list(self, user_id: int) -> list[Subject]:
        return await SubjectRepository(self.session).list_by_user(user_id)

    async def get_owned(self, user_id: int, subject_id: int) -> Subject:
        subject = await SubjectRepository(self.session).get_owned(user_id, subject_id)
        if not subject:
            raise OwnershipError("La asignatura no existe o no es tuya.")
        return subject

    async def rename(self, user_id: int, subject_id: int, new_name: str) -> Subject | None:
        from app.database.repositories import norm

        repo = SubjectRepository(self.session)
        subject = await repo.get_owned(user_id, subject_id)
        if not subject:
            return None
        subject.name = " ".join(new_name.split())[:80]
        subject.name_norm = norm(new_name)[:80]
        return subject
