"""Assignments repository — every query is ownership-filtered by user_id."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Assignment, AssignmentStatus


class AssignmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_owned(self, user_id: int, assignment_id: int) -> Assignment | None:
        stmt = select(Assignment).where(Assignment.id == assignment_id, Assignment.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_open(self, user_id: int) -> list[Assignment]:
        stmt = (
            select(Assignment)
            .where(
                Assignment.user_id == user_id,
                Assignment.status.in_([status for status in AssignmentStatus]),
            )
            .order_by(Assignment.due_at, Assignment.due_date)
        )
        rows = list((await self.session.execute(stmt)).scalars())
        return [r for r in rows if r.status in (AssignmentStatus.PENDING, AssignmentStatus.IN_PROGRESS)]

    @staticmethod
    def deadline_sort_key(a: Assignment) -> tuple:
        """Deterministic sort key: precise time first, then date, then undated last."""
        if a.due_at:
            return (0, a.due_at, "")
        if a.due_date:
            return (1, a.due_date, "")
        return (2, None, "")

    async def create(self, user_id: int, **values) -> Assignment:
        assignment = Assignment(user_id=user_id, status=AssignmentStatus.PENDING, **values)
        self.session.add(assignment)
        await self.session.flush()
        return assignment

    async def update_owned(self, user_id: int, assignment_id: int, **values) -> Assignment | None:
        assignment = await self.get_owned(user_id, assignment_id)
        if not assignment:
            return None
        allowed = {"description", "subject_id", "due_at", "due_date", "has_time", "est_minutes", "priority", "status", "notes", "completed_at"}
        for k, v in values.items():
            if k in allowed:
                setattr(assignment, k, v)
        await self.session.flush()
        return assignment

    async def delete_owned(self, user_id: int, assignment_id: int) -> bool:
        assignment = await self.get_owned(user_id, assignment_id)
        if not assignment:
            return False
        await self.session.delete(assignment)
        return True

    async def purge_open(self, user_id: int) -> None:
        await self.session.execute(
            delete(Assignment).where(Assignment.user_id == user_id)
        )
