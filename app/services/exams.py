"""Exams service: CRUD + reminder scheduling + audit + duplicate control."""

from __future__ import annotations

import logging
import unicodedata
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database.models import AuditAction, Exam, ExamStatus, PrepStatus, Reminder, ReminderType
from app.database.repositories import (
    ExamRepository,
    ReminderRepository,
    SettingsRepository,
    UserRepository,
)
from app.database.repositories.subjects import AuditRepository, SubjectRepository
from app.services.errors import OwnershipError, ValidationError
from app.utils.timezones import from_local_to_utc, now_utc

logger = logging.getLogger(__name__)

# Assumption (documented in docs/DESIGN.md): date-only exams are treated as 09:00 local
DEFAULT_EXAM_HOURS, DEFAULT_EXAM_MINUTES = 9, 0


class DuplicateExam(ValidationError):
    pass


def _norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode()
    return " ".join(t.lower().split())


def exam_key(exam_at: datetime | None, exam_date: date | None) -> datetime:
    if exam_at:
        return exam_at if exam_at.tzinfo else exam_at.replace(tzinfo=timezone.utc)
    assert exam_date is not None
    return datetime(exam_date.year, exam_date.month, exam_date.day, DEFAULT_EXAM_HOURS, DEFAULT_EXAM_MINUTES, tzinfo=timezone.utc)


class ExamService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    # ---------- creation ----------
    async def create_exam(
        self,
        user_id: int,
        audit: AuditRepository,
        *,
        title: str,
        subject_name: str | None = None,
        subject_id: int | None = None,
        exam_date: date | None = None,
        exam_at: datetime | None = None,
        has_time: bool = False,
        topics: str | None = None,
        est_prep_minutes: int = 300,
        importance: int = 3,
        difficulty: int = 3,
        notes: str | None = None,
        location: str | None = None,
        schedule_reminders: bool = True,
    ) -> Exam:
        if not title or not title.strip():
            raise ValidationError("El titulo del examen no puede estar vacio.")
        if exam_at is None and exam_date is None:
            raise ValidationError("Falta la fecha del examen.")
        if exam_at is not None and exam_at.tzinfo is None:
            raise ValidationError("La hora del examen debe ser consciente de zona horaria.")

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

        repo = ExamRepository(self.session)
        existing = await repo.list_active(user_id)
        target_key = exam_key(exam_at, exam_date)
        for ex in existing:
            key = exam_key(ex.exam_at, ex.exam_date)
            same_subject = ex.subject_id == subject_id
            if not same_subject:
                continue
            if _norm_title(ex.title) == _norm_title(title):
                raise DuplicateExam(f"Ya tienes un examen activo '{ex.title}' en esa asignatura.")
            if abs((key - target_key).total_seconds()) <= 6 * 3600:
                raise DuplicateExam(f"Ya hay un examen de esa asignatura cerca de esa fecha: '{ex.title}'.")

        exam = await repo.create(
            user_id,
            title=title.strip()[:180],
            subject_id=subject_id,
            exam_at=exam_at,
            exam_date=exam_date if exam_at is None else None,
            has_time=bool(has_time and exam_at is not None),
            topics=topics,
            est_prep_minutes=max(0, int(est_prep_minutes)),
            importance=max(1, min(5, importance)),
            difficulty=max(1, min(5, difficulty)),
            notes=notes,
            location=location,
        )
        audit.add(user_id, AuditAction.CREATE, "exam", exam.id, {"title": exam.title, "subject": subject_name})
        await self.session.flush()

        if schedule_reminders:
            await self._schedule_exam_reminders(user_id, exam, subject_name or "General")
        return exam

    # ---------- scheduling ----------
    async def _schedule_exam_reminders(self, user_id: int, exam: Exam, subject_name: str) -> list[Reminder]:
        settings_row = await SettingsRepository(self.session).get_or_create(user_id)
        offsets = list((settings_row.default_offsets_minutes or {}).get("exam", [10080, 4320, 1440, 60]))
        event_dt = await self.event_dt_for_user(exam)
        if event_dt is None:
            return []
        now = now_utc()
        rrepo = ReminderRepository(self.session)
        scheduled: list[Reminder] = []
        for minutes in offsets:
            send_at = event_dt - timedelta(minutes=minutes)
            if send_at <= now:
                continue
            scheduled.append(
                await rrepo.create(
                    user_id,
                    ReminderType.EXAM_ADVANCE,
                    send_at=send_at,
                    exam_id=exam.id,
                    max_retries=self.settings.reminder_max_retries,
                )
            )
        return scheduled

    async def event_dt_for_user(self, exam: Exam) -> datetime | None:
        if exam.exam_at:
            return exam.exam_at if exam.exam_at.tzinfo else exam.exam_at.replace(tzinfo=timezone.utc)
        if exam.exam_date is None:
            return None
        user = await UserRepository(self.session).get_by_id(exam.user_id)
        tz = user.timezone if user else self.settings.default_timezone
        return from_local_to_utc(exam.exam_date, DEFAULT_EXAM_HOURS, DEFAULT_EXAM_MINUTES, tz)

    async def subject_name(self, exam: Exam) -> str:
        if not exam.subject_id:
            return "General"
        subject = await SubjectRepository(self.session).get_owned(exam.user_id, exam.subject_id)
        return subject.name if subject else "General"

    # ---------- queries ----------
    async def get_owned(self, user_id: int, exam_id: int) -> Exam:
        exam = await ExamRepository(self.session).get_owned(user_id, exam_id)
        if not exam:
            raise OwnershipError("El examen no existe o no es tuyo.")
        return exam

    async def list_active(self, user_id: int) -> list[Exam]:
        exams = await ExamRepository(self.session).list_active(user_id)
        return sorted(exams, key=lambda e: exam_key(e.exam_at, e.exam_date))

    # ---------- mutations ----------
    async def move_exam(self, user_id: int, exam_id: int, new_date: date | None, new_at: datetime | None, has_time: bool = False) -> Exam:
        exam = await self.get_owned(user_id, exam_id)
        old_key = await self.event_dt_for_user(exam)
        await ExamRepository(self.session).update_owned(
            user_id, exam_id,
            exam_at=new_at,
            exam_date=new_date if new_at is None else None,
            has_time=bool(has_time and new_at is not None),
        )
        await self.session.refresh(exam)
        new_key = await self.event_dt_for_user(exam)
        if old_key and new_key and old_key != new_key:
            await ReminderRepository(self.session).cancel_scope(user_id, exam_id=exam.id, reminder_type=ReminderType.EXAM_ADVANCE)
            subject_name = await self.subject_name(exam)
            await self._schedule_exam_reminders(user_id, exam, subject_name)
        await self.session.flush()
        return exam

    async def complete_exam(self, user_id: int, exam_id: int) -> Exam:
        exam = await self.get_owned(user_id, exam_id)
        await ExamRepository(self.session).update_owned(user_id, exam_id, status=ExamStatus.COMPLETED, prep_status=PrepStatus.READY)
        await ReminderRepository(self.session).cancel_scope(user_id, exam_id=exam_id)
        await self.session.flush()
        return exam

    async def cancel_exam(self, user_id: int, exam_id: int) -> Exam:
        exam = await self.get_owned(user_id, exam_id)
        await ExamRepository(self.session).update_owned(user_id, exam_id, status=ExamStatus.CANCELLED)
        await ReminderRepository(self.session).cancel_scope(user_id, exam_id=exam_id)
        await self.session.flush()
        return exam

    async def delete_exam(self, user_id: int, exam_id: int) -> bool:
        await self.get_owned(user_id, exam_id)
        return await ExamRepository(self.session).delete_owned(user_id, exam_id)

    async def update_exam(self, user_id: int, exam_id: int, **values) -> Exam:
        await self.get_owned(user_id, exam_id)
        allowed = {"title", "topics", "importance", "difficulty", "est_prep_minutes", "notes", "location", "prep_status"}
        values = {k: v for k, v in values.items() if k in allowed}
        if values:
            await ExamRepository(self.session).update_owned(user_id, exam_id, **values)
            await self.session.flush()
        return await self.get_owned(user_id, exam_id)
