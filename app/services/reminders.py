"""Persistent reminder engine (transactional outbox worker).

The database is the source of truth; this service processes due reminders in batches.
Handlers never send notifications directly - they enqueue rows in `reminders`.

Delivery contract (documented in docs/DESIGN.md):
- SENT is recorded ONLY after the notifier confirms (message_id returned).
- Transient failures reschedule with exponential backoff + jitter.
- Permanent failures (blocked, chat not found) mark failed and stop retrying.
- Catch-up policy sends overdue pending reminders after downtime (summary mode default),
  expiring reminders whose events already passed (overdue-task alerts stay relevant).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.database.models import (
    Assignment,
    AssignmentStatus,
    Exam,
    ExamStatus,
    Reminder,
    ReminderStatus,
    ReminderType,
    SessionStatus,
    Subject,
    utcnow,
)
from app.database.repositories import (
    AssignmentRepository,
    ExamRepository,
    ReminderRepository,
    SettingsRepository,
    StudyRepository,
    UserRepository,
)
from app.services.content import (
    assignment_due_text,
    catchup_summary_text,
    daily_summary_text,
    exam_advance_text,
    overdue_tasks_text,
    reminder_type_label,
)
from app.services.exams import DEFAULT_EXAM_HOURS, DEFAULT_EXAM_MINUTES
from app.services.notifications import PermanentNotifierError, TransientNotifierError
from app.utils.timezones import from_local_to_utc, now_utc, to_user_tz, today_in_zone

logger = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 6 * 3600
QUIET_GRACE_MINUTES = 120


@dataclass
class BatchStats:
    sent: int = 0
    failed: int = 0
    rescheduled: int = 0
    quiet_postponed: int = 0
    expired: int = 0
    claimed_lost: int = 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"BatchStats(sent={self.sent}, failed={self.failed}, rescheduled={self.rescheduled}, quiet={self.quiet_postponed}, expired={self.expired}, lost={self.claimed_lost})"


@dataclass
class ReminderContent:
    text: str
    is_urgent: bool
    event_dt: datetime | None


class ReminderService:
    def __init__(self, session_factory: async_sessionmaker, notifier, settings: Settings | None = None) -> None:
        self.session_factory = session_factory
        self.notifier = notifier  # admission port: any object with async send(chat_id, text) -> int
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------ helpers
    async def get_user_settings(self, user_id: int):
        async with self.session_factory() as session:
            return await SettingsRepository(session).get_or_create(user_id)

    async def user_tz(self, session: AsyncSession, user_id: int) -> str:
        user = await UserRepository(session).get_by_id(user_id)
        return user.timezone if user and user.timezone else self.settings.default_timezone

    async def exam_event_dt(self, session: AsyncSession, exam: Exam, tz: str) -> datetime | None:
        if exam.exam_at:
            return exam.exam_at
        if exam.exam_date is None:
            return None
        return from_local_to_utc(exam.exam_date, DEFAULT_EXAM_HOURS, DEFAULT_EXAM_MINUTES, tz)

    async def assignment_deadline(self, session: AsyncSession, a: Assignment, tz: str) -> datetime | None:
        if a.due_at:
            return a.due_at
        if a.due_date is None:
            return None
        return from_local_to_utc(a.due_date, 23, 59, tz)

    # ------------------------------------------------------------------ scheduling
    async def schedule_for_exam(self, user_id: int, exam: Exam) -> list[Reminder]:
        """(Re)schedule pending reminders for an exam according to user offsets."""
        settings_row = await self.get_user_settings(user_id)
        offsets = list((settings_row.default_offsets_minutes or {}).get("exam", [10080, 4320, 1440, 60]))
        async with self.session_factory() as session:
            wallet = await self.user_tz(session, user_id)
            event_dt = await self.exam_event_dt(session, exam, wallet)
            if event_dt is None:
                return []
            now = now_utc()
            rrepo = ReminderRepository(session)
            await rrepo.cancel_scope(user_id, exam_id=exam.id, reminder_type=ReminderType.EXAM_ADVANCE)
            rows: list[Reminder] = []
            for minutes in offsets:
                send_at = event_dt - timedelta(minutes=minutes)
                if send_at <= now:
                    continue
                rows.append(
                    await rrepo.create(user_id, ReminderType.EXAM_ADVANCE, send_at=send_at, exam_id=exam.id, max_retries=self.settings.reminder_max_retries)
                )
            await session.commit()
        return rows

    async def schedule_for_assignment(self, user_id: int, assignment: Assignment) -> list[Reminder]:
        settings_row = await self.get_user_settings(user_id)
        offsets = list((settings_row.default_offsets_minutes or {}).get("assignment", [1440, 60]))
        async with self.session_factory() as session:
            tz = await self.user_tz(session, user_id)
            event_dt = await self.assignment_deadline(session, assignment, tz)
            if event_dt is None:
                return []
            now = now_utc()
            rrepo = ReminderRepository(session)
            await rrepo.cancel_scope(user_id, assignment_id=assignment.id, reminder_type=ReminderType.ASSIGNMENT_DUE)
            rows: list[Reminder] = []
            for minutes in offsets:
                send_at = event_dt - timedelta(minutes=minutes)
                if send_at <= now:
                    continue
                rows.append(await rrepo.create(user_id, ReminderType.ASSIGNMENT_DUE, send_at=send_at, assignment_id=assignment.id, max_retries=self.settings.reminder_max_retries))
            await session.commit()
        return rows

    async def cancel_for_exam(self, user_id: int, exam_id: int) -> int:
        async with self.session_factory() as session:
            n = await ReminderRepository(session).cancel_scope(user_id, exam_id=exam_id)
            await session.commit()
            return n

    async def cancel_for_assignment(self, user_id: int, assignment_id: int) -> int:
        async with self.session_factory() as session:
            n = await ReminderRepository(session).cancel_scope(user_id, assignment_id=assignment_id)
            await session.commit()
            return n

    async def enqueue_daily_digest(self, user_id: int) -> None:
        """Create tomorrow's daily summary row; idempotent per local day (called hourly)."""
        async with self.session_factory() as session:
            user = await UserRepository(session).get_by_id(user_id)
            settings_row = await SettingsRepository(session).get_or_create(user_id)
            if not user or not settings_row.digest_enabled:
                return
            tz = user.timezone or self.settings.default_timezone
            now = now_utc()
            today_local = today_in_zone(tz)
            local_hour = to_user_tz(now, tz).hour
            if local_hour != settings_row.digest_hour:
                return
            start_local = from_local_to_utc(today_local, 0, 0, tz)
            end_local = from_local_to_utc(today_local + timedelta(days=1), 0, 0, tz)
            dup_rows = (await session.execute(
                sa.select(Reminder.id).where(
                    Reminder.user_id == user_id,
                    Reminder.reminder_type == ReminderType.DAILY_SUMMARY,
                    Reminder.status.in_([ReminderStatus.PENDING, ReminderStatus.PROCESSING, ReminderStatus.SENT]),
                    Reminder.send_at >= start_local,
                    Reminder.send_at < end_local,
                )
            )).all()
            if dup_rows:
                return
            send_at = from_local_to_utc(today_local, settings_row.digest_hour, 0, tz)
            if send_at < now:
                send_at = now
            await ReminderRepository(session).create(user_id, ReminderType.DAILY_SUMMARY, send_at=send_at)
            await session.commit()

    async def run_digest_pass(self) -> int:
        created = 0
        async with self.session_factory() as session:
            users = await UserRepository(session).all_active()
            ids = [u.id for u in users]
        for uid in ids:
            settings_row = await self.get_user_settings(uid)
            if settings_row.digest_enabled:
                before = await self._pending_digest_today(uid)
                await self.enqueue_daily_digest(uid)
                after = await self._pending_digest_today(uid)
                if len(after) > len(before):
                    created += 1
        return created

    async def _pending_digest_today(self, user_id: int) -> list[int]:
        async with self.session_factory() as session:
            user = await UserRepository(session).get_by_id(user_id)
            if not user:
                return []
            tz = user.timezone or self.settings.default_timezone
            today_local = today_in_zone(tz)
            start_local = from_local_to_utc(today_local, 0, 0, tz)
            end_local = from_local_to_utc(today_local + timedelta(days=1), 0, 0, tz)
            rows = (await session.execute(
                sa.select(Reminder.id).where(
                    Reminder.user_id == user_id,
                    Reminder.reminder_type == ReminderType.DAILY_SUMMARY,
                    Reminder.status == ReminderStatus.PENDING,
                    Reminder.send_at >= start_local,
                    Reminder.send_at < end_local,
                )
            )).all()
            return [r[0] for r in rows]

    # ------------------------------------------------------------------ processing
    async def process_due_batch(self, now: datetime | None = None, limit: int = 20) -> BatchStats:
        now = now or now_utc()
        stats = BatchStats()
        async with self.session_factory() as session:
            repo = ReminderRepository(session)
            due_rows = await repo.due(now, limit=limit)
            due_meta = [(r.id, r.user_id) for r in due_rows]
            chat_ids: dict[int, int | None] = {}
            tz_map: dict[int, str] = {}
            for _, uid in due_meta:
                if uid not in chat_ids:
                    u = await UserRepository(session).get_by_id(uid)
                    chat_ids[uid] = u.chat_id if u else None
                    tz_map[uid] = (u.timezone if u else None) or self.settings.default_timezone
        for reminder_id, user_id in due_meta:
            result = await self._process_one(reminder_id, user_id, chat_ids.get(user_id), tz_map.get(user_id, self.settings.default_timezone), now)
            if result == "sent":
                stats.sent += 1
            elif result == "failed":
                stats.failed += 1
            elif result == "rescheduled":
                stats.rescheduled += 1
            elif result == "quiet":
                stats.quiet_postponed += 1
            elif result == "expired":
                stats.expired += 1
            elif result == "lost":
                stats.claimed_lost += 1
        return stats

    async def _process_one(self, reminder_id: int, user_id: int, chat_id: int | None, tz: str, now: datetime) -> str:
        async with self.session_factory() as session:
            repo = ReminderRepository(session)
            reminder = await repo.claim(reminder_id, now)
            if reminder is None:
                return "lost"
            max_retries = reminder.max_retries if reminder.max_retries is not None else self.settings.reminder_max_retries
            retry_count = reminder.retry_count or 0
            await session.commit()

        if chat_id is None or chat_id == 0:
            async with self.session_factory() as session:
                await ReminderRepository(session).mark_failed(reminder_id, "no_chat", "usuario sin chat asociado", attempt=retry_count + 1)
                await session.commit()
            logger.info("recordatorio sin chat reminder_id=%s user=%s", reminder_id, user_id)
            return "failed"

        content = await self.build_content(reminder_id)
        if content is None:
            async with self.session_factory() as session:
                await ReminderRepository(session).expire(reminder_id)
                await session.commit()
            logger.info("recordatorio huérfano expirado reminder_id=%s", reminder_id)
            return "expired"

        # Quiet hours (postpone unless the event is imminent)
        prefs = await self.get_user_settings(user_id)
        effective_at = self.quiet_postpone_at(now, tz, prefs.quiet_start, prefs.quiet_end)
        if effective_at is not None and not content.is_urgent:
            async with self.session_factory() as session:
                await session.execute(
                    sa.update(Reminder)
                    .where(Reminder.id == reminder_id, Reminder.status == ReminderStatus.PROCESSING)
                    .values(status=ReminderStatus.PENDING, send_at=effective_at, claimed_at=None)
                )
                await session.commit()
            return "quiet"
        if effective_at is not None and content.is_urgent:
            logger.info("quiet hours ignoradas por urgencia reminder_id=%s", reminder_id)

        attempt = retry_count + 1
        try:
            message_id = await self.notifier.send(chat_id, content.text)
            async with self.session_factory() as session:
                await ReminderRepository(session).mark_sent(reminder_id, message_id, attempt=attempt, now=now_utc())
                await session.commit()
            return "sent"
        except PermanentNotifierError as exc:
            async with self.session_factory() as session:
                await ReminderRepository(session).mark_failed(reminder_id, exc.kind, "fallo permanente", attempt=attempt)
                await session.commit()
            logger.warning("fallo permanente recordatorio reminder_id=%s kind=%s", reminder_id, exc.kind)
            return "failed"
        except TransientNotifierError as exc:
            if attempt > max_retries:
                async with self.session_factory() as session:
                    await ReminderRepository(session).mark_failed(reminder_id, exc.kind, "max reintentos", attempt=attempt)
                    await session.commit()
                return "failed"
            delay = self._backoff_delay(attempt, exc.retry_after_seconds)
            next_at = now_utc() + timedelta(seconds=delay)
            async with self.session_factory() as session:
                await ReminderRepository(session).reschedule(reminder_id, next_at, exc.kind, "reintento programado", attempt=attempt, now=now_utc())
                await session.commit()
            logger.info("reintento programado reminder_id=%s intento=%s delay=%.0fs", reminder_id, attempt, delay)
            return "rescheduled"

    @staticmethod
    def _backoff_delay(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None and retry_after > 0:
            return min(retry_after, BACKOFF_CAP_SECONDS)
        base = BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1))
        jitter = random.uniform(0.8, 1.2)
        return min(base * jitter, BACKOFF_CAP_SECONDS)

    @staticmethod
    def quiet_postpone_at(now: datetime, tz: str, quiet_start: int | None, quiet_end: int | None) -> datetime | None:
        """If `now` is inside the user's local quiet window, return UTC time when it ends."""
        if quiet_start is None or quiet_end is None or quiet_start == quiet_end:
            return None
        local = to_user_tz(now, tz)
        h = local.hour
        if quiet_start < quiet_end:
            in_quiet = quiet_start <= h < quiet_end
        else:  # window crosses midnight
            in_quiet = h >= quiet_start or h < quiet_end
        if not in_quiet:
            return None
        if h >= quiet_start:  # evening window -> postpone to next day's quiet_end
            target_day = local.date() + timedelta(days=1)
        else:  # early-morning window -> ends today
            target_day = local.date()
        return from_local_to_utc(target_day, quiet_end, 0, tz)

    # ------------------------------------------------------------------ content
    async def build_content(self, reminder_id: int) -> ReminderContent | None:
        async with self.session_factory() as session:
            row = (await session.execute(sa.select(Reminder).where(Reminder.id == reminder_id))).scalar_one_or_none()
            if row is None:
                return None
            user = await UserRepository(session).get_by_id(row.user_id)
            if not user:
                return None
            tz = user.timezone or self.settings.default_timezone
            subject_names = {
                s.id: s.name for s in (await session.execute(sa.select(Subject).where(Subject.user_id == row.user_id))).scalars()
            }

            if row.reminder_type == ReminderType.EXAM_ADVANCE and row.exam_id:
                exam = await ExamRepository(session).get_owned(row.user_id, row.exam_id)
                if not exam or exam.status != ExamStatus.ACTIVE:
                    return None
                event_dt = await self.exam_event_dt(session, exam, tz)
                if event_dt is None or row.send_at is None:
                    return None
                if event_dt < now_utc() - timedelta(seconds=15):
                    return None
                send_aware = row.send_at if row.send_at.tzinfo else row.send_at.replace(tzinfo=timezone.utc)
                offset_minutes = int((event_dt - send_aware).total_seconds() // 60)
                subject_name = subject_names.get(exam.subject_id or 0, "General")
                text = exam_advance_text(exam, subject_name, tz, to_user_tz(event_dt, tz), offset_minutes)
                return ReminderContent(text=text, is_urgent=offset_minutes <= QUIET_GRACE_MINUTES, event_dt=event_dt)

            if row.reminder_type == ReminderType.ASSIGNMENT_DUE and row.assignment_id:
                a = await AssignmentRepository(session).get_owned(row.user_id, row.assignment_id)
                if not a or a.status not in (AssignmentStatus.PENDING, AssignmentStatus.IN_PROGRESS):
                    return None
                event_dt = await self.assignment_deadline(session, a, tz)
                if event_dt is None or row.send_at is None:
                    return None
                send_aware = row.send_at if row.send_at.tzinfo else row.send_at.replace(tzinfo=timezone.utc)
                offset_minutes = int((event_dt - send_aware).total_seconds() // 60)
                subject_name = subject_names.get(a.subject_id or 0, "General")
                text = assignment_due_text(a, subject_name, tz, to_user_tz(event_dt, tz))
                return ReminderContent(text=text, is_urgent=offset_minutes <= QUIET_GRACE_MINUTES, event_dt=event_dt)

            if row.reminder_type == ReminderType.OVERDUE_TASK:
                return await self._build_overdue_content(session, row.user_id, tz)

            if row.reminder_type == ReminderType.DAILY_SUMMARY:
                return await self._build_daily_summary(session, row.user_id, tz, subject_names)

            # custom
            return ReminderContent(text="Recordatorio", is_urgent=False, event_dt=None)

    async def _build_overdue_content(self, session: AsyncSession, user_id: int, tz: str) -> ReminderContent | None:
        opened = await AssignmentRepository(session).list_open(user_id)
        now = utcnow()
        today = today_in_zone(tz)
        overdue: list[Assignment] = []
        for a in opened:
            if a.due_at and a.due_at < now:
                overdue.append(a)
            elif a.due_date and not a.due_at and a.due_date < today:
                overdue.append(a)
        if not overdue:
            return None
        subject_names = {
            s.id: s.name for s in (await session.execute(sa.select(Subject).where(Subject.user_id == user_id))).scalars()
        }
        text = overdue_tasks_text([(a, subject_names.get(a.subject_id or 0, "General")) for a in overdue], tz)
        return ReminderContent(text=text, is_urgent=False, event_dt=None)

    async def _build_daily_summary(self, session: AsyncSession, user_id: int, tz: str, subject_names: dict[int, str]) -> ReminderContent | None:
        now = utcnow()
        today = today_in_zone(tz)
        day_start = from_local_to_utc(today, 0, 0, tz)
        day_end = from_local_to_utc(today + timedelta(days=1), 0, 0, tz)

        exams = await StudyRepository(session).open_exams(user_id)
        exams_today: list[Exam] = []
        exams_soon: list[tuple[Exam, int]] = []
        for e in exams:
            dt = await self.exam_event_dt(session, e, tz)
            if dt is None:
                continue
            if day_start <= dt < day_end:
                exams_today.append(e)
            elif day_end <= dt < day_end + timedelta(days=14):
                days = max(1, (dt - day_end).days + 1)
                exams_soon.append((e, days))
        exams_soon.sort(key=lambda pair: pair[0].exam_at or datetime.max.replace(tzinfo=pair[0].exam_at.tzinfo) if pair[0].exam_at else pair[0].exam_date)

        assignments = await AssignmentRepository(session).list_open(user_id)
        names_for_user = dict(subject_names)
        due_today: list[Assignment] = []
        overdue: list[Assignment] = []
        for a in assignments:
            if a.due_at:
                due_day = to_user_tz(a.due_at, tz).date()
                if due_day == today:
                    due_today.append(a)
                elif a.due_at < now:
                    overdue.append(a)
            elif a.due_date:
                if a.due_date == today:
                    due_today.append(a)
                elif a.due_date < today:
                    overdue.append(a)

        sessions_rows = await StudyRepository(session).sessions_between(user_id, day_start, day_end)
        session_items: list[str] = []
        for s in sessions_rows:
            if s.status != SessionStatus.PLANNED:
                continue
            subj = subject_names.get(s.subject_id or 0, "Estudio")
            local_start = to_user_tz(s.start_at, tz).strftime("%H:%M")
            objective = s.objective or ""
            session_items.append(f"{local_start} {subj} ({s.planned_minutes} min) {objective}".strip())

        text = daily_summary_text(
            tz,
            today,
            exams_today and [(e, names_for_user.get(e.subject_id or 0, "General")) for e in exams_today] or [],
            [(e, names_for_user.get(e.subject_id or 0, "General"), d) for e, d in exams_soon[:3]],
            [(a, names_for_user.get(a.subject_id or 0, "General")) for a in due_today],
            [(a, names_for_user.get(a.subject_id or 0, "General")) for a in overdue],
            session_items,
        )
        return ReminderContent(text=text, is_urgent=False, event_dt=None)

    # ------------------------------------------------------------------ catch-up & recovery
    async def recover_stuck_processing(self) -> int:
        """Reset PROCESSING rows older than the configured threshold to PENDING."""
        cutoff = now_utc() - timedelta(seconds=self.settings.stuck_processing_seconds)
        async with self.session_factory() as session:
            res = await session.execute(
                sa.update(Reminder)
                .where(Reminder.status == ReminderStatus.PROCESSING, Reminder.claimed_at < cutoff)
                .values(status=ReminderStatus.PENDING, claimed_at=None)
            )
            await session.commit()
            n = int(res.rowcount or 0)
        if n:
            logger.warning("recuperados %s recordatorios atascados en PROCESSING", n)
        return n

    async def catch_up_plan(self, now: datetime | None = None) -> dict[int, list[Reminder]]:
        """Classify overdue PENDING reminders: expire obsolete, group deliverable ones."""
        now = now or now_utc()
        groups: dict[int, list[Reminder]] = {}
        to_expire: list[int] = []
        async with self.session_factory() as session:
            rows = await ReminderRepository(session).pending_before(now)
            for r in rows:
                tz = await self.user_tz(session, r.user_id)
                if r.reminder_type == ReminderType.EXAM_ADVANCE and r.exam_id:
                    exam = await ExamRepository(session).get_owned(r.user_id, r.exam_id)
                    event_dt = await self.exam_event_dt(session, exam, tz) if exam else None
                    if exam is None or exam.status != ExamStatus.ACTIVE or event_dt is None or event_dt < now:
                        to_expire.append(r.id)
                        continue
                elif r.reminder_type == ReminderType.ASSIGNMENT_DUE and r.assignment_id:
                    a = await AssignmentRepository(session).get_owned(r.user_id, r.assignment_id)
                    deadline = await self.assignment_deadline(session, a, tz) if a else None
                    if a is None or a.status not in (AssignmentStatus.PENDING, AssignmentStatus.IN_PROGRESS) or (deadline and deadline < now):
                        to_expire.append(r.id)
                        continue
                elif r.reminder_type == ReminderType.DAILY_SUMMARY:
                    # stale daily summaries from downtime: deliver at most the latest
                    to_expire.append(r.id)
                    continue
                elif r.reminder_type == ReminderType.OVERDUE_TASK:
                    pass  # still relevant if tasks remain overdue
                groups.setdefault(r.user_id, []).append(r)
            for rid in to_expire:
                await ReminderRepository(session).expire(rid)
            await session.commit()
        if any(to_expire):
            logger.info("expirados %s recordatorios obsoletos en catch-up", len(to_expire))
        return groups

    async def flush_overdue_queue(self, groups: dict[int, list] | None = None, now: datetime | None = None) -> dict:
        now = now or now_utc()
        report = {"summary": 0, "individual": 0, "failed": 0, "expired": 0}
        if groups is None:
            groups = await self.catch_up_plan(now)
        for user_id, items in groups.items():
            if not items:
                continue
            settings_row = await self.get_user_settings(user_id)
            mode = settings_row.catchup_mode or "summary"
            max_backlog = max(1, int(settings_row.max_backlog or self.settings.catchup_max_backlog))
            ordered = sorted(items, key=lambda r: r.send_at)
            deliverable = ordered[:max_backlog]
            overflow = ordered[max_backlog:]
            if mode == "summary" or len(deliverable) > 3:
                ok = await self._deliver_catchup_summary(user_id, deliverable)
                if ok:
                    report["summary"] += len(deliverable)
                else:
                    report["failed"] += len(deliverable)
            else:
                for item in deliverable:
                    ok = await self._deliver_catchup_individual(user_id, item)
                    report["individual"] += 1 if ok else 0
                    if not ok:
                        report["failed"] += 1
            if overflow:
                async with self.session_factory() as session:
                    for r in overflow:
                        await ReminderRepository(session).expire(r.id)
                    await session.commit()
                report["expired"] += len(overflow)
                logger.warning("catch-up recortado por max_backlog user=%s omitidos=%s", user_id, len(overflow))
        return report

    async def _deliver_catchup_summary(self, user_id: int, items: list[Reminder]) -> bool:
        async with self.session_factory() as session:
            user = await UserRepository(session).get_by_id(user_id)
            if not user or not user.chat_id:
                for r in items:
                    await ReminderRepository(session).mark_failed(r.id, "no_chat", "sin chat", attempt=1)
                await session.commit()
                return False
            tz = user.timezone or self.settings.default_timezone
            subject_names = {s.id: s.name for s in (await session.execute(sa.select(Subject).where(Subject.user_id == user_id))).scalars()}
            rendered: list[tuple[str, str, str]] = []
            for r in sorted(items, key=lambda x: x.send_at):
                title = await self._describe_reminder(session, r, subject_names)
                send_aware_2 = r.send_at if r.send_at.tzinfo else r.send_at.replace(tzinfo=timezone.utc)
                when = to_user_tz(send_aware_2, tz).strftime("%d/%m %H:%M")
                rendered.append((reminder_type_label(r.reminder_type), title, when))
            text = catchup_summary_text(rendered, tz)
            chat_id = user.chat_id
        try:
            message_id = await self.notifier.send(chat_id, text)
        except (TransientNotifierError, PermanentNotifierError) as exc:
            kind = getattr(exc, "kind", "unknown")
            async with self.session_factory() as session:
                for r in items:
                    await ReminderRepository(session).mark_failed(r.id, kind, "catch-up fallo", attempt=1)
                await session.commit()
            return False
        async with self.session_factory() as session:
            for r in items:
                await ReminderRepository(session).mark_summary_sent(r.id, message_id, attempt=1, now=now_utc())
            await session.commit()
        logger.info("catch-up resumen enviado user=%s items=%s", user_id, len(items))
        return True

    async def _deliver_catchup_individual(self, user_id: int, reminder: Reminder) -> bool:
        async with self.session_factory() as session:
            user = await UserRepository(session).get_by_id(user_id)
            if not user or not user.chat_id:
                await ReminderRepository(session).mark_failed(reminder.id, "no_chat", "sin chat", attempt=1)
                await session.commit()
                return False
            content = await self.build_content(reminder.id)
            if content is None:
                await ReminderRepository(session).expire(reminder.id)
                await session.commit()
                return False
            chat_id = user.chat_id
        try:
            message_id = await self.notifier.send(chat_id, content.text)
        except (TransientNotifierError, PermanentNotifierError) as exc:
            kind = getattr(exc, "kind", "unknown")
            async with self.session_factory() as session:
                await ReminderRepository(session).mark_failed(reminder.id, kind, "catch-up fallo", attempt=1)
                await session.commit()
            return False
        async with self.session_factory() as session:
            await ReminderRepository(session).mark_summary_sent(reminder.id, message_id, attempt=1, now=now_utc())
            await session.commit()
        return True

    async def _describe_reminder(self, session: AsyncSession, r: Reminder, subject_names: dict[int, str]) -> str:
        if r.exam_id:
            exam = (await session.execute(sa.select(Exam).where(Exam.id == r.exam_id))).scalar_one_or_none()
            if exam:
                return f"{subject_names.get(exam.subject_id or 0, 'General')}: {exam.title}"
        if r.assignment_id:
            a = (await session.execute(sa.select(Assignment).where(Assignment.id == r.assignment_id))).scalar_one_or_none()
            if a:
                return a.description[:120]
        return "Recordatorio"

    async def full_recovery(self) -> dict:
        """Run on startup BEFORE the scheduler/polling starts."""
        report: dict = {}
        report["stuck_reset"] = await self.recover_stuck_processing()
        groups = await self.catch_up_plan()
        overdue_titles = {uid: [r.uuid for r in rows] for uid, rows in groups.items()}
        report["groups"] = list(overdue_titles)
        flush = await self.flush_overdue_queue(groups)
        report["delivered"] = flush
        return report

    # ------------------------------------------------------------------ housekeeping
    async def purge_old(self, days: int = 120) -> int:
        """Retention: terminal-state reminders older than N days are garbage."""
        cutoff = now_utc() - timedelta(days=days)
        async with self.session_factory() as session:
            res = await session.execute(
                sa.delete(Reminder).where(
                    Reminder.status.in_([ReminderStatus.SENT, ReminderStatus.EXPIRED, ReminderStatus.CANCELLED]),
                    Reminder.sent_at < cutoff,
                )
            )
            await session.commit()
            return int(res.rowcount or 0)

    async def cancel_owned(self, user_id: int, reminder_id: int) -> bool:
        async with self.session_factory() as session:
            ok = await ReminderRepository(session).cancel_owned(user_id, reminder_id)
            await session.commit()
            return ok

    async def counts_for_report(self) -> dict:
        async with self.session_factory() as session:
            now = now_utc()
            pending = await session.execute(sa.select(sa.func.count()).select_from(Reminder).where(Reminder.status == ReminderStatus.PENDING))
            overdue = await session.execute(sa.select(sa.func.count()).select_from(Reminder).where(Reminder.status == ReminderStatus.PENDING, Reminder.send_at <= now))
            failed = await session.execute(sa.select(sa.func.count()).select_from(Reminder).where(Reminder.status == ReminderStatus.FAILED))
            return {
                "pending": int(pending.scalar_one()),
                "overdue": int(overdue.scalar_one()),
                "failed": int(failed.scalar_one()),
            }

    async def list_user_reminders(self, user_id: int, limit: int = 30) -> list[Reminder]:
        async with self.session_factory() as session:
            rows = await ReminderRepository(session).list_by_user(user_id, status=None, limit=limit)
            return rows


