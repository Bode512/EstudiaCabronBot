"""Outbox engine: claim, retry backoff, quiet hours postponement, catch-up summary, digest."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import sqlalchemy as sa

from app.database.models import (
    Reminder,
    ReminderStatus,
    utcnow,
)
from app.database.repositories import AuditRepository
from app.services.exams import ExamService


def _mk_session(ctx):
    return ctx.session_factory()


@pytest.mark.asyncio
async def test_catchup_expires_stale_semantics_and_summary_mode(ctx, user_row):
    # exam + its reminders, age everything a day
    async with ctx.session_factory() as ses:
        await ExamService(ses).create_exam(
            user_row.id, AuditRepository(ses), title="Geo", subject_name="Geografia",
            exam_date=date.today() + timedelta(days=10),
        )
        await ses.commit()
    async with ctx.session_factory() as ses:
        await ses.execute(
            sa.update(Reminder).where(Reminder.status == ReminderStatus.PENDING).values(send_at=utcnow() - timedelta(days=1))
        )
        await ses.commit()
    groups = await ctx.reminders.catch_up_plan()
    assert user_row.id in groups
    flushed = await ctx.reminders.flush_overdue_queue(groups)
    # summary mode default: exam reminders not expired should be delivered as one summary
    assert flushed["summary"] >= 3
    async with ctx.session_factory() as ses:
        left = (
            await ses.execute(
                sa.select(sa.func.count()).select_from(Reminder).where(
                    Reminder.user_id == user_row.id, Reminder.status == ReminderStatus.PENDING
                )
            )
        ).scalar_one()
        assert left == 0


@pytest.mark.asyncio
async def test_claim_atomic_single_delivery(ctx, user_row):
    async with ctx.session_factory() as ses:
        await ExamService(ses).create_exam(
            user_row.id, AuditRepository(ses), title="Unica", subject_name="Lengua",
            exam_date=date.today() + timedelta(days=10),
        )
        await ses.commit()
        rid = (
            await ses.execute(sa.select(Reminder.id).limit(1))
        ).scalar_one()
        # age it so it becomes due NOW
        await ses.execute(sa.update(Reminder).where(Reminder.id == rid).values(send_at=utcnow() - timedelta(minutes=5)))
        # exam lookups: fake due but exam is active so not expired
        await ses.commit()
    stats = await ctx.reminders.process_due_batch()
    assert stats.sent == 1
    # second run should deliver nothing (already claimed & sent)
    stats2 = await ctx.reminders.process_due_batch()
    assert stats2.sent == 0


@pytest.mark.asyncio
async def test_recover_stuck_processing(ctx, user_row):
    async with ctx.session_factory() as ses:
        await ExamService(ses).create_exam(
            user_row.id, AuditRepository(ses), title="Stuck", subject_name="Fisica",
            exam_date=date.today() + timedelta(days=10),
        )
        rid = (await ses.execute(sa.select(Reminder.id).limit(1))).scalar_one()
        await ses.execute(
            sa.update(Reminder).where(Reminder.id == rid).values(
                send_at=utcnow() - timedelta(minutes=30),
                status=ReminderStatus.PROCESSING,
                claimed_at=utcnow() - timedelta(minutes=15),
            )
        )
        await ses.commit()
    recovered = await ctx.reminders.recover_stuck_processing()
    assert recovered == 1


@pytest.mark.asyncio
async def test_process_batch_max_retries_then_fail(ctx, user_row):

    async with ctx.session_factory() as ses:
        audit = AuditRepository(ses)
        await ExamService(ses).create_exam(
            user_row.id, audit, title="Fail", subject_name="Mates",
            exam_date=date.today() + timedelta(days=10),
        )
        rid = (await ses.execute(sa.select(Reminder.id).limit(1))).scalar_one()
        await ses.execute(sa.update(Reminder).where(Reminder.id == rid).values(send_at=utcnow() - timedelta(minutes=5)))
        await ses.commit()
        for _ in range(0, 20):
            ctx.reminders.notifier = _Failing()
            await ctx.reminders.process_due_batch()
            async with ctx.session_factory() as s2:
                r = (await s2.execute(sa.select(Reminder).where(Reminder.id == rid))).scalar_one()
                if r.status == ReminderStatus.FAILED:
                    break
                await s2.execute(
                    sa.update(Reminder).where(Reminder.id == rid).values(send_at=utcnow() - timedelta(minutes=1))
                )
                await s2.commit()
        assert r.status == ReminderStatus.FAILED
        assert r.retry_count >= ctx.settings.reminder_max_retries


class _Failing:
    async def send(self, chat_id: int, text: str) -> int:
        from app.services.notifications import TransientNotifierError

        raise TransientNotifierError("telegram down")

    async def close(self):
        return

