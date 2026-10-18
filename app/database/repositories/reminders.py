"""Reminders repository: transactional outbox operations.

State machine:
  pending -> processing -> sent
                    |-> failed (terminal, after max retries)
  processing -> pending   (async reset by recovery)
  pending -> expired | cancelled
Claims are atomic conditional updates so concurrent workers cannot double-process.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    DeliveryStatus,
    NotificationDelivery,
    Reminder,
    ReminderStatus,
    ReminderType,
    utcnow,
)


class ReminderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: int,
        reminder_type: ReminderType,
        send_at: datetime,
        *,
        exam_id: int | None = None,
        assignment_id: int | None = None,
        max_retries: int = 5,
        status: ReminderStatus = ReminderStatus.PENDING,
    ) -> Reminder:
        reminder = Reminder(
            user_id=user_id,
            reminder_type=reminder_type,
            send_at=send_at,
            exam_id=exam_id,
            assignment_id=assignment_id,
            max_retries=max_retries,
            status=status,
        )
        self.session.add(reminder)
        await self.session.flush()
        return reminder

    async def get_owned(self, user_id: int, reminder_id: int) -> Reminder | None:
        stmt = select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def due(self, now: datetime, limit: int = 50) -> list[Reminder]:
        stmt = (
            select(Reminder)
            .where(Reminder.status == ReminderStatus.PENDING, Reminder.send_at <= now)
            .order_by(Reminder.send_at)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get(self, reminder_id: int) -> Reminder | None:
        return (await self.session.execute(select(Reminder).where(Reminder.id == reminder_id))).scalar_one_or_none()

    async def list_by_user(self, user_id: int, statuses: list[ReminderStatus] | None = None, limit: int = 50) -> list[Reminder]:
        stmt = select(Reminder).where(Reminder.user_id == user_id).order_by(Reminder.send_at.desc()).limit(limit)
        if statuses:
            stmt = stmt.where(Reminder.status.in_(statuses))
        return list((await self.session.execute(stmt)).scalars())

    async def pending_before(self, now: datetime) -> list[Reminder]:
        stmt = select(Reminder).where(Reminder.status == ReminderStatus.PENDING, Reminder.send_at <= now).order_by(Reminder.user_id, Reminder.send_at)
        return list((await self.session.execute(stmt)).scalars())

    async def pending_future(self, now: datetime) -> list[Reminder]:
        stmt = select(Reminder).where(Reminder.status == ReminderStatus.PENDING, Reminder.send_at > now)
        return list((await self.session.execute(stmt)).scalars())

    async def stuck_processing(self, before: datetime) -> list[Reminder]:
        stmt = select(Reminder).where(Reminder.status == ReminderStatus.PROCESSING, Reminder.claimed_at < before)
        return list((await self.session.execute(stmt)).scalars())

    async def claim(self, reminder_id: int, now: datetime) -> Reminder | None:
        """Atomic conditional claim; returns the row if ONLY this worker won it."""
        res = await self.session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id, Reminder.status == ReminderStatus.PENDING)
            .values(status=ReminderStatus.PROCESSING, claimed_at=now)
        )
        if (res.rowcount or 0) != 1:
            return None
        return await self.get(reminder_id)

    async def mark_sent(self, reminder_id: int, message_id: int | None, attempt: int, now: datetime) -> Reminder:
        await self.session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id, Reminder.status != ReminderStatus.SENT)
            .values(status=ReminderStatus.SENT, sent_at=now, claimed_at=None, deliver_mode="individual")
        )
        await self.add_delivery(reminder_id, attempt, DeliveryStatus.DELIVERED, telegram_message_id=message_id)
        return (await self.get(reminder_id))  # type: ignore[return-value]

    async def mark_summary_sent(self, reminder_id: int, message_id: int | None, attempt: int, now: datetime) -> None:
        await self.session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(status=ReminderStatus.SENT, sent_at=now, claimed_at=None, deliver_mode="summary")
        )
        await self.add_delivery(reminder_id, attempt, DeliveryStatus.DELIVERED, telegram_message_id=message_id)

    async def reschedule(self, reminder_id: int, next_send_at: datetime, error_kind: str, error_detail: str, attempt: int, now: datetime) -> None:
        """Transient failure: keep pending, push send time out with backoff (caller computes)."""
        await self.session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id, Reminder.status == ReminderStatus.PROCESSING)
            .values(status=ReminderStatus.PENDING, send_at=next_send_at, retry_count=attempt, claimed_at=None, last_error=f"{error_kind}: {error_detail}"[:200])
        )
        await self.add_delivery(reminder_id, attempt, DeliveryStatus.FAILED, error_kind=error_kind, error_detail=error_detail[:200])

    async def mark_failed(self, reminder_id: int, error_kind: str, error_detail: str, attempt: int) -> None:
        await self.session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id, Reminder.status == ReminderStatus.PROCESSING)
            .values(status=ReminderStatus.FAILED, claimed_at=None, last_error=f"{error_kind}: {error_detail}"[:200], retry_count=attempt)
        )
        await self.add_delivery(reminder_id, attempt, DeliveryStatus.FAILED, error_kind=error_kind, error_detail=error_detail[:200])

    async def reset_processing(self, reminder_id: int) -> None:
        await self.session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id, Reminder.status == ReminderStatus.PROCESSING)
            .values(status=ReminderStatus.PENDING, claimed_at=None)
        )

    async def expire(self, reminder_id: int) -> None:
        await self.session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id, Reminder.status == ReminderStatus.PENDING)
            .values(status=ReminderStatus.EXPIRED, claimed_at=None)
        )

    async def cancel_owned(self, user_id: int, reminder_id: int) -> bool:
        res = await self.session.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id, Reminder.user_id == user_id, Reminder.status.in_([ReminderStatus.PENDING]))
            .values(status=ReminderStatus.CANCELLED)
        )
        return (res.rowcount or 0) == 1

    async def cancel_scope(self, user_id: int, exam_id: int | None = None, assignment_id: int | None = None, reminder_type: ReminderType | None = None) -> int:
        stmt = update(Reminder).where(Reminder.user_id == user_id, Reminder.status == ReminderStatus.PENDING)
        if exam_id:
            stmt = stmt.where(Reminder.exam_id == exam_id)
        if assignment_id:
            stmt = stmt.where(Reminder.assignment_id == assignment_id)
        if reminder_type:
            stmt = stmt.where(Reminder.reminder_type == reminder_type)
        res = await self.session.execute(stmt.values(status=ReminderStatus.CANCELLED))
        return int(res.rowcount or 0)

    async def add_delivery(
        self,
        reminder_id: int,
        attempt: int,
        status: DeliveryStatus,
        telegram_message_id: int | None = None,
        error_kind: str | None = None,
        error_detail: str | None = None,
    ) -> NotificationDelivery:
        row = NotificationDelivery(
            reminder_id=reminder_id,
            attempt=attempt,
            status=status,
            telegram_message_id=telegram_message_id,
            error_kind=error_kind,
            error_detail=(error_detail or "")[:200],
            tried_at=utcnow(),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def failed_needing_attention(self) -> int:
        stmt = select(Reminder.id).where(Reminder.status == ReminderStatus.FAILED)
        return len(list((await self.session.execute(stmt)).scalars()))

    async def delete_scope(self, user_id: int, exam_id: int | None = None, assignment_id: int | None = None) -> None:
        stmt = delete(Reminder).where(Reminder.user_id == user_id)
        if exam_id:
            stmt = stmt.where(Reminder.exam_id == exam_id)
        if assignment_id:
            stmt = stmt.where(Reminder.assignment_id == assignment_id)
        await self.session.execute(stmt)
