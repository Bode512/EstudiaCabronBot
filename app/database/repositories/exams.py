"""Exams repository — every query is ownership-filtered by user_id."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Exam, ExamStatus, PrepStatus


class ExamRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_owned(self, user_id: int, exam_id: int) -> Exam | None:
        stmt = select(Exam).where(Exam.id == exam_id, Exam.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_active(self, user_id: int) -> list[Exam]:
        stmt = select(Exam).where(Exam.user_id == user_id, Exam.status == ExamStatus.ACTIVE).order_by(Exam.exam_at)  # type: ignore[arg-type]
        return list((await self.session.execute(stmt)).scalars())

    async def list_upcoming(self, user_id: int, until: datetime, from_dt: datetime | None = None) -> list[Exam]:
        stmt = select(Exam).where(
            Exam.user_id == user_id,
            Exam.status == ExamStatus.ACTIVE,
            (Exam.exam_at <= until) | (Exam.exam_date <= until.date()),  # type: ignore[union-attr]
        ).order_by(Exam.exam_at)  # type: ignore[arg-type]
        rows = list((await self.session.execute(stmt)).scalars())
        if from_dt:
            rows = [
                r
                for r in rows
                if (r.exam_at and r.exam_at >= from_dt) or (r.exam_date and datetime(r.exam_date.year, r.exam_date.month, r.exam_date.day) >= from_dt)
            ]
        return rows

    async def list_on_day(self, user_id: int, day: date) -> list[Exam]:
        stmt = select(Exam).where(
            Exam.user_id == user_id,
            Exam.status == ExamStatus.ACTIVE,
            Exam.exam_date == day,
        )
        return list((await self.session.execute(stmt)).scalars())

    async def create(self, user_id: int, **values) -> Exam:
        exam = Exam(user_id=user_id, status=ExamStatus.ACTIVE, prep_status=PrepStatus.NOT_STARTED, **values)
        self.session.add(exam)
        await self.session.flush()
        return exam

    async def update_owned(self, user_id: int, exam_id: int, **values) -> Exam | None:
        exam = await self.get_owned(user_id, exam_id)
        if not exam:
            return None
        allowed = {"title", "subject_id", "exam_at", "exam_date", "has_time", "location", "topics", "est_prep_minutes", "importance", "difficulty", "prep_status", "status", "notes"}
        for k, v in values.items():
            if k in allowed:
                setattr(exam, k, v)
        await self.session.flush()
        return exam

    async def delete_owned(self, user_id: int, exam_id: int) -> bool:
        exam = await self.get_owned(user_id, exam_id)
        if not exam:
            return False
        await self.session.delete(exam)
        return True

    async def list_active_for_sort(self, user_id: int) -> list[Exam]:
        """Active exams with a sortable key (exam_at if precise, else exam_date)."""
        exams = await self.list_active(user_id)
        return exams


def exam_key(exam: Exam) -> datetime:
    if exam.exam_at:
        return exam.exam_at
    assert exam.exam_date is not None
    return datetime(
        exam.exam_date.year, exam.exam_date.month, exam.exam_date.day,
        9, 0, tzinfo=exam.exam_at.tzinfo if exam.exam_at else None,
    )
