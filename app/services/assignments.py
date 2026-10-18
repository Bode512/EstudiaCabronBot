"""Assignments service: CRUD + overdue logic (user-timezone aware) + reminders + audit."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database.models import Assignment, AssignmentStatus, AuditAction, Reminder, ReminderType
from app.database.repositories import (
    AssignmentRepository,
    ReminderRepository,
    SettingsRepository,
    UserRepository,
)
from app.database.repositories.subjects import AuditRepository, SubjectRepository
from app.services.errors import OwnershipError, ValidationError
from app.utils.timezones import from_local_to_utc, now_utc, to_user_tz, today_in_zone

logger = logging.getLogger(__name__)


class AssignmentService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    async def user_tz(self, user_id: int) -> str:
        user = await UserRepository(self.session).get_by_id(user_id)
        if user and user.timezone:
            return user.timezone
        return self.settings.default_timezone

    async def create_assignment(
        self,
        user_id: int,
        audit: AuditRepository,
        *,
        description: str,
        subject_name: str | None = None,
        subject_id: int | None = None,
        due_date: date | None = None,
        due_at: datetime | None = None,
        has_time: bool = False,
        est_minutes: int = 60,
        priority: int = 2,
        notes: str | None = None,
        schedule_reminders: bool = True,
    ) -> tuple[Assignment, bool]:
        if not description or not description.strip():
            raise ValidationError("La descripcion de la tarea no puede estar vacia.")
        if due_at is None and due_date is None:
            raise ValidationError("Falta la fecha de entrega.")
        if due_at is not None and due_at.tzinfo is None:
            raise ValidationError("La hora de entrega debe ser consciente de zona horaria.")
        priority = max(1, min(3, priority))

        subject_repo = SubjectRepository(self.session)
        if subject_id is None and subject_name:
            subject = await subject_repo.get_or_create(user_id, subject_name)
            subject_id = subject.id
            subject_name = subject.name
        elif subject_id is not None:
            subject = await subject_repo.get_owned(user_id, subject_id)
            if subject is None:
                raise OwnershipError("La asignatura no existe o no es tuya.")
            subject_name = subject.name

        repo = AssignmentRepository(self.session)
        opened = await repo.list_open(user_id)
        for t in opened:
            if t.subject_id == subject_id and t.description.strip().lower()[:40] == description.strip().lower()[:40]:
                existing = await repo.get_owned(user_id, t.id)
                return existing, False  # idempotent duplicate

        assignment = await repo.create(
            user_id,
            description=description.strip()[:220],
            subject_id=subject_id,
            due_at=due_at,
            due_date=due_date if due_at is None else None,
            has_time=bool(has_time and due_at is not None),
            est_minutes=max(0, int(est_minutes)),
            priority=priority,
            notes=notes,
        )
        audit.add(user_id, AuditAction.CREATE, "assignment", assignment.id, {"description": assignment.description[:80]})
        await self.session.flush()

        if schedule_reminders:
            await self._schedule_assignment_reminders(user_id, assignment)
        return assignment, True

    async def _schedule_assignment_reminders(self, user_id: int, assignment: Assignment) -> list[Reminder]:
        settings_row = await SettingsRepository(self.session).get_or_create(user_id)
        offsets = list((settings_row.default_offsets_minutes or {}).get("assignment", [1440, 60]))
        event_dt = await self.deadline_utc(assignment, user_id)
        if event_dt is None:
            return []
        now = now_utc()
        rrepo = ReminderRepository(self.session)
        rows: list[Reminder] = []
        for minutes in offsets:
            send_at = event_dt - timedelta(minutes=minutes)
            if send_at <= now:
                continue
            rows.append(
                await rrepo.create(
                    user_id,
                    ReminderType.ASSIGNMENT_DUE,
                    send_at=send_at,
                    assignment_id=assignment.id,
                    max_retries=self.settings.reminder_max_retries,
                )
            )
        return rows

    async def deadline_utc(self, assignment: Assignment, user_id: int) -> datetime | None:
        if assignment.due_at:
            return assignment.due_at if assignment.due_at.tzinfo else assignment.due_at.replace(tzinfo=timezone.utc)
        if assignment.due_date is None:
            return None
        tz = await self.user_tz(user_id)
        return from_local_to_utc(assignment.due_date, 23, 59, tz)

    async def due_date_of(self, assignment: Assignment, user_id: int) -> date | None:
        """The deadline as seen in the user's timezone ('entrega el 12 de noviembre')."""
        if assignment.due_date and not assignment.due_at:
            return assignment.due_date
        dt = await self.deadline_utc(assignment, user_id)
        if dt is None:
            return None
        return to_user_tz(dt, await self.user_tz(user_id)).date()

    async def is_overdue(self, assignment: Assignment, user_id: int, now: datetime | None = None) -> bool:
        now = now or now_utc()
        if assignment.due_at:
            due = assignment.due_at if assignment.due_at.tzinfo else assignment.due_at.replace(tzinfo=timezone.utc)
            return due < now
        if assignment.due_date:
            tz = await self.user_tz(user_id)
            today = today_in_zone(tz)
            return assignment.due_date < today
        return False

    async def get_owned(self, user_id: int, assignment_id: int) -> Assignment:
        a = await AssignmentRepository(self.session).get_owned(user_id, assignment_id)
        if not a:
            raise OwnershipError("La tarea no existe o no es tuya.")
        return a

    async def list_open(self, user_id: int) -> list[Assignment]:
        rows = await AssignmentRepository(self.session).list_open(user_id)
        return sorted(rows, key=AssignmentRepository.deadline_sort_key)

    async def list_overdue(self, user_id: int, now: datetime | None = None) -> list[Assignment]:
        opened = await self.list_open(user_id)
        return [a for a in opened if await self.is_overdue(a, user_id, now)]

    async def due_today(self, user_id: int) -> list[Assignment]:
        opened = await self.list_open(user_id)
        out: list[Assignment] = []
        for a in opened:
            day = await self.due_date_of(a, user_id)
            if day == today_in_zone(await self.user_tz(user_id)):
                out.append(a)
        return out

    async def move_deadline(self, user_id: int, assignment_id: int, new_date: date | None, new_at: datetime | None, has_time: bool = False) -> Assignment:
        assignment = await self.get_owned(user_id, assignment_id)
        await AssignmentRepository(self.session).update_owned(
            user_id, assignment_id,
            due_at=new_at,
            due_date=new_date if new_at is None else None,
            has_time=bool(has_time and new_at is not None),
        )
        await self.session.refresh(assignment)
        await ReminderRepository(self.session).cancel_scope(user_id, assignment_id=assignment.id, reminder_type=ReminderType.ASSIGNMENT_DUE)
        await self._schedule_assignment_reminders(user_id, assignment)
        await self.session.flush()
        return assignment

    async def complete_assignment(self, user_id: int, assignment_id: int) -> Assignment:
        assignment = await self.get_owned(user_id, assignment_id)
        await AssignmentRepository(self.session).update_owned(
            user_id, assignment_id, status=AssignmentStatus.COMPLETED, completed_at=now_utc()
        )
        await ReminderRepository(self.session).cancel_scope(user_id, assignment_id=assignment_id)
        await self.session.flush()
        self._last_completed = assignment.id
        return assignment

    async def cancel_assignment(self, user_id: int, assignment_id: int) -> Assignment:
        await self.get_owned(user_id, assignment_id)
        await AssignmentRepository(self.session).update_owned(user_id, assignment_id, status=AssignmentStatus.CANCELLED)
        await ReminderRepository(self.session).cancel_scope(user_id, assignment_id=assignment_id)
        await self.session.flush()

    async def delete_assignment(self, user_id: int, assignment_id: int) -> bool:
        await self.get_owned(user_id, assignment_id)
        return await AssignmentRepository(self.session).delete_owned(user_id, assignment_id)

    async def update_assignment(self, user_id: int, assignment_id: int, **values) -> Assignment:
        await self.get_owned(user_id, assignment_id)
        allowed = {"description", "priority", "est_minutes", "notes", "status"}
        values = {k: v for k, v in values.items() if k in allowed}
        if values:
            await AssignmentRepository(self.session).update_owned(user_id, assignment_id, **values)
            await self.session.flush()
        return await self.get_owned(user_id, assignment_id)
