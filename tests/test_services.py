"""Service-level flows: registration, exam/task creation + dedupe, ownership, moves."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import sqlalchemy as sa

from app.database.models import Reminder, ReminderStatus
from app.database.repositories import AuditRepository
from app.services.assignments import AssignmentService
from app.services.errors import OwnershipError, ValidationError
from app.services.exams import DuplicateExam, ExamService


async def _audit_session(ctx):
    ses = ctx.session_factory()
    return ses, AuditRepository(ses)


@pytest.mark.asyncio
async def test_register_is_idempotent_and_reactivates(ctx):
    u1, created1 = await ctx.users.register(77, "A", chat_id=77)
    assert created1
    u2, created2 = await ctx.users.register(77, "A", chat_id=77)
    assert not created2 and u2.id == u1.id
    await ctx.users.delete_account(u1.id)
    u3, created3 = await ctx.users.register(77, "A", chat_id=77)
    assert created3


@pytest.mark.asyncio
async def test_exam_creates_reminders_and_dedupe(ctx, user_row):
    async with ctx.session_factory() as ses:
        audit = AuditRepository(ses)
        svc = ExamService(ses)
        today = date.today()
        exam_day = today + timedelta(days=10)
        exam = await svc.create_exam(
            user_row.id, audit, title="Integral", subject_name="Matematicas", exam_date=exam_day
        )
        await ses.commit()
        assert exam.exam_date == exam_day
        offsets = [
            r.send_at
            for r in (
                await ses.execute(
                    sa.select(Reminder).where(
                        Reminder.user_id == user_row.id, Reminder.status == ReminderStatus.PENDING
                    )
                )
            ).scalars()
        ]
        assert len(offsets) == 4
        assert len(set(offsets)) == 4
        with pytest.raises(DuplicateExam):
            await svc.create_exam(
                user_row.id, audit, title="Integral", subject_name="Matematicas", exam_date=exam_day
            )


@pytest.mark.asyncio
async def test_move_exam_recomputes_reminders(ctx, user_row):
    async with ctx.session_factory() as ses:
        audit = AuditRepository(ses)
        svc = ExamService(ses)
        exam = await svc.create_exam(
            user_row.id, audit, title="Quimica", subject_name="Quimica", exam_date=date.today() + timedelta(days=10)
        )
        await ses.commit()
    async with ctx.session_factory() as ses:
        audit = AuditRepository(ses)
        svc = ExamService(ses)
        await svc.move_exam(user_row.id, exam.id, date.today() + timedelta(days=15), None, has_time=False)
        await ses.commit()
    async with ctx.session_factory() as ses:
        pend = (
            await ses.execute(
                sa.select(sa.func.count()).select_from(Reminder).where(
                    Reminder.user_id == user_row.id, Reminder.status == ReminderStatus.PENDING
                )
            )
        ).scalar_one()
        assert pend == 4


@pytest.mark.asyncio
async def test_complete_exam_cancels_reminders(ctx, user_row):
    async with ctx.session_factory() as ses:
        audit = AuditRepository(ses)
        svc = ExamService(ses)
        exam = await svc.create_exam(
            user_row.id, audit, title="Historia", subject_name="Historia", exam_date=date.today() + timedelta(days=10)
        )
        await ses.commit()
    async with ctx.session_factory() as ses:
        audit = AuditRepository(ses)
        svc = ExamService(ses)
        await svc.complete_exam(user_row.id, exam.id)
        await ses.commit()
    async with ctx.session_factory() as ses:
        pend = (
            await ses.execute(
                sa.select(sa.func.count()).select_from(Reminder).where(
                    Reminder.user_id == user_row.id, Reminder.status == ReminderStatus.PENDING
                )
            )
        ).scalar_one()
        assert pend == 0


@pytest.mark.asyncio
async def test_assignment_dedupe_and_ownership(ctx, user_row):
    async with ctx.session_factory() as ses:
        audit = AuditRepository(ses)
        svc = AssignmentService(ses)
        t1, cre1 = await svc.create_assignment(
            user_row.id, audit, description="Problemas 1-10", subject_name="Fisica", due_date=date.today()
        )
        assert cre1
        _t2, cre2 = await svc.create_assignment(
            user_row.id, audit, description="Problemas 1-10", subject_name="Fisica", due_date=date.today()
        )
        assert not cre2
    other_user, _ = await ctx.users.register(99, "B", chat_id=99)
    async with ctx.session_factory() as ses:
        svc = AssignmentService(ses)
        with pytest.raises(OwnershipError):
            await svc.complete_assignment(other_user.id, t1.id)


@pytest.mark.asyncio
async def test_exam_validation_errors(ctx, user_row):
    async with ctx.session_factory() as ses:
        audit = AuditRepository(ses)
        svc = ExamService(ses)
        with pytest.raises(ValidationError):
            await svc.create_exam(user_row.id, audit, title="  ", exam_date=date.today() + timedelta(days=10))
        with pytest.raises(ValidationError):
            await svc.create_exam(user_row.id, audit, title="Sin fecha")


