"""Documents, topics, study sessions and plans repositories."""

from __future__ import annotations

from datetime import date, datetime
from datetime import timezone as _tz

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Assignment,
    AssignmentStatus,
    Document,
    DocumentStatus,
    Exam,
    ExamStatus,
    PlanStatus,
    SessionStatus,
    StudyPlan,
    StudySession,
    Topic,
)


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_owned(self, user_id: int, document_id: int) -> Document | None:
        stmt = select(Document).where(Document.id == document_id, Document.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_user(self, user_id: int, limit: int = 30) -> list[Document]:
        stmt = select(Document).where(Document.user_id == user_id).order_by(Document.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def create(self, user_id: int, original_name: str, stored_path: str, kind: str, size_bytes: int) -> Document:
        doc = Document(
            user_id=user_id,
            original_name=original_name[:255],
            stored_path=stored_path,
            kind=kind,
            size_bytes=size_bytes,
            status=DocumentStatus.PENDING,
        )
        self.session.add(doc)
        await self.session.flush()
        return doc

    async def update_owned(self, user_id: int, document_id: int, **values) -> Document | None:
        doc = await self.get_owned(user_id, document_id)
        if not doc:
            return None
        allowed = {"status", "error", "meta", "processed_at"}
        for k, v in values.items():
            if k in allowed:
                setattr(doc, k, v)
        await self.session.flush()
        return doc

    async def delete_owned(self, user_id: int, document_id: int) -> Document | None:
        doc = await self.get_owned(user_id, document_id)
        if not doc:
            return None
        await self.session.delete(doc)
        return doc


class TopicRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_by_user(self, user_id: int, subject_id: int | None = None) -> list[Topic]:
        stmt = select(Topic).where(Topic.user_id == user_id).order_by(Topic.subject_id, Topic.order_no, Topic.id)
        if subject_id:
            stmt = stmt.where(Topic.subject_id == subject_id)
        return list((await self.session.execute(stmt)).scalars())

    async def get_owned(self, user_id: int, topic_id: int) -> Topic | None:
        stmt = select(Topic).where(Topic.id == topic_id, Topic.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(self, user_id: int, name: str, *, subject_id: int | None = None, exam_id: int | None = None, order_no: int = 0) -> Topic:
        topic = Topic(user_id=user_id, name=name[:160], subject_id=subject_id, exam_id=exam_id, order_no=order_no)
        self.session.add(topic)
        await self.session.flush()
        return topic

    async def update_owned(self, user_id: int, topic_id: int, **values) -> Topic | None:
        topic = await self.get_owned(user_id, topic_id)
        if not topic:
            return None
        allowed = {"name", "description", "status", "confidence", "order_no", "exam_id"}
        for k, v in values.items():
            if k in allowed:
                setattr(topic, k, v)
        await self.session.flush()
        return topic

    async def delete_owned(self, user_id: int, topic_id: int) -> bool:
        topic = await self.get_owned(user_id, topic_id)
        if not topic:
            return False
        await self.session.delete(topic)
        return True


class StudyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- plans ---
    async def active_plan(self, user_id: int) -> StudyPlan | None:
        stmt = select(StudyPlan).where(StudyPlan.user_id == user_id, StudyPlan.status.in_([PlanStatus.PROPOSED, PlanStatus.ACTIVE])).order_by(StudyPlan.created_at.desc())
        return (await self.session.execute(stmt)).scalars().first()

    async def supersede_all(self, user_id: int) -> None:
        await self.session.execute(
            update(StudyPlan).where(StudyPlan.user_id == user_id, StudyPlan.status.in_([PlanStatus.PROPOSED, PlanStatus.ACTIVE])).values(status=PlanStatus.SUPERSEDED)
        )

    async def create_plan(self, user_id: int, start_date: date, end_date: date, criteria: dict, version: int) -> StudyPlan:
        plan = StudyPlan(user_id=user_id, start_date=start_date, end_date=end_date, criteria=criteria, version=version, status=PlanStatus.PROPOSED)
        self.session.add(plan)
        await self.session.flush()
        return plan

    async def set_plan_status(self, user_id: int, plan_id: int, status: PlanStatus) -> StudyPlan | None:
        stmt = select(StudyPlan).where(StudyPlan.id == plan_id, StudyPlan.user_id == user_id)
        plan = (await self.session.execute(stmt)).scalar_one_or_none()
        if not plan:
            return None
        plan.status = status
        await self.session.flush()
        return plan

    async def get_plan_owned(self, user_id: int, plan_id: int) -> StudyPlan | None:
        stmt = select(StudyPlan).where(StudyPlan.id == plan_id, StudyPlan.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # --- sessions ---
    async def create_session(
        self,
        user_id: int,
        start_at: datetime,
        end_at: datetime,
        *,
        subject_id: int | None = None,
        topic_id: int | None = None,
        exam_id: int | None = None,
        assignment_id: int | None = None,
        plan_id: int | None = None,
        planned_minutes: int,
        objective: str | None = None,
        status: SessionStatus = SessionStatus.PLANNED,
    ) -> StudySession:
        session_row = StudySession(
            user_id=user_id,
            start_at=start_at,
            end_at=end_at,
            subject_id=subject_id,
            topic_id=topic_id,
            exam_id=exam_id,
            assignment_id=assignment_id,
            plan_id=plan_id,
            planned_minutes=planned_minutes,
            objective=objective,
            status=status,
        )
        self.session.add(session_row)
        await self.session.flush()
        return session_row

    async def get_owned_session(self, user_id: int, session_id: int) -> StudySession | None:
        stmt = select(StudySession).where(StudySession.id == session_id, StudySession.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def sessions_between(self, user_id: int, start: datetime, end: datetime) -> list[StudySession]:
        stmt = select(StudySession).where(StudySession.user_id == user_id, StudySession.start_at >= start, StudySession.start_at < end).order_by(StudySession.start_at)
        return list((await self.session.execute(stmt)).scalars())

    async def planned_sessions_from(self, user_id: int, from_dt: datetime) -> list[StudySession]:
        stmt = select(StudySession).where(StudySession.user_id == user_id, StudySession.status == SessionStatus.PLANNED, StudySession.start_at >= from_dt).order_by(StudySession.start_at)
        return list((await self.session.execute(stmt)).scalars())

    async def done_minutes_per_exam(self, user_id: int) -> dict[int, int]:
        stmt = select(StudySession.exam_id, StudySession.planned_minutes).where(StudySession.user_id == user_id, StudySession.status == SessionStatus.DONE, StudySession.exam_id.isnot(None))
        out: dict[int, int] = {}
        for exam_id, minutes in (await self.session.execute(stmt)).all():
            out[int(exam_id)] = out.get(int(exam_id), 0) + int(minutes or 0)
        return out

    async def delete_planned_by_plan(self, user_id: int, plan_id: int) -> None:
        await self.session.execute(
            delete(StudySession).where(StudySession.user_id == user_id, StudySession.plan_id == plan_id, StudySession.status == SessionStatus.PLANNED)
        )

    async def delete_all_planned(self, user_id: int, from_dt: datetime | None = None) -> int:
        stmt = delete(StudySession).where(StudySession.user_id == user_id, StudySession.status == SessionStatus.PLANNED)
        if from_dt:
            stmt = stmt.where(StudySession.start_at >= from_dt)
        res = await self.session.execute(stmt)
        return int(res.rowcount or 0)

    async def update_session_owned(self, user_id: int, session_id: int, **values) -> StudySession | None:
        row = await self.get_owned_session(user_id, session_id)
        if not row:
            return None
        allowed = {"status", "notes", "completed_at", "objective", "start_at", "end_at", "planned_minutes"}
        for k, v in values.items():
            if k in allowed and v is not None:
                setattr(row, k, v)
        await self.session.flush()
        return row

    async def delete_session_owned(self, user_id: int, session_id: int) -> bool:
        row = await self.get_owned_session(user_id, session_id)
        if not row:
            return False
        await self.session.delete(row)
        return True

    # --- denormalized views for dashboard/planner ---
    async def open_exams(self, user_id: int) -> list[Exam]:
        stmt = select(Exam).where(Exam.user_id == user_id, Exam.status == ExamStatus.ACTIVE)
        return list((await self.session.execute(stmt)).scalars())

    async def open_assignments(self, user_id: int) -> list[Assignment]:
        stmt = select(Assignment).where(Assignment.user_id == user_id, Assignment.status.in_([AssignmentStatus.PENDING, AssignmentStatus.IN_PROGRESS]))
        return list((await self.session.execute(stmt)).scalars())

    async def topics_for_subject(self, user_id: int, subject_id: int) -> list[Topic]:
        stmt = select(Topic).where(Topic.user_id == user_id, Topic.subject_id == subject_id).order_by(Topic.order_no, Topic.id)
        return list((await self.session.execute(stmt)).scalars())

    async def count_open(self, user_id: int) -> dict[str, int]:
        exams = await self.open_exams(user_id)
        assignments = await self.open_assignments(user_id)
        return {"exams": len(exams), "assignments": len(assignments)}


def parse_due_key(a: Assignment) -> datetime:
    """UTC deadline for sorts; date-only = 23:59 UTC of that day (approximation)."""
    if a.due_at:
        return a.due_at
    if a.due_date:
        return datetime(a.due_date.year, a.due_date.month, a.due_date.day, 23, 59, tzinfo=_tz.utc)
    return datetime.max.replace(tzinfo=_tz.utc)
