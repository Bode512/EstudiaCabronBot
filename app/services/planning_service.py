"""Planning service: gathers DB inputs into the pure planner, persists approved proposals.

The DETERMINISTIC algorithm lives in study_planner.plan_study; this module only
does IO and formatting. Proposed plans never touch existing sessions; approval
activates them. Re-planning preserves done/missed sessions and regenerates the rest.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings, get_settings
from app.database.models import (
    AssignmentStatus,
    AuditAction,
    ExamStatus,
    PlanStatus,
    SessionStatus,
    StudyPlan,
)
from app.database.repositories import (
    AuditRepository,
    ExamRepository,
    SettingsRepository,
    StudyRepository,
    UserRepository,
)
from app.services.study_planner import (
    AssignmentInput,
    ExamInput,
    PlanProposal,
    SessionProposal,
    plan_study,
)
from app.utils.timezones import from_local_to_utc, local_from_utc, now_utc, today_in_zone

logger = logging.getLogger(__name__)

SLOT_BREAK_MINUTES = 10


class PlanningService:
    def __init__(self, session_factory: async_sessionmaker, settings: Settings | None = None) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    async def user_context(self, user_id: int) -> dict:
        async with self.session_factory() as session:
            user = await UserRepository(session).get_by_id(user_id)
            settings_row = await SettingsRepository(session).get_or_create(user_id)
            tz = (user.timezone if user else None) or self.settings.default_timezone
            weekly = {}
            for k, v in (settings_row.weekly_availability or {}).items():
                try:
                    weekly[int(k)] = int(v)
                except (TypeError, ValueError):
                    continue
            return {
                "tz": tz,
                "daily_default": settings_row.daily_availability_minutes or 120,
                "weekly": weekly,
                "start_hour": settings_row.study_day_start_hour or 16,
                "end_hour": settings_row.study_day_end_hour or 22,
            }

    # ---------------------------------------------------------------- proposal
    async def build_proposal(self, user_id: int) -> PlanProposal:
        ctx = await self.user_context(user_id)
        tz = ctx["tz"]
        today = today_in_zone(tz)
        async with self.session_factory() as session:
            exams = await ExamRepository(session).list_active(user_id)
            exams = [e for e in exams if e.status == ExamStatus.ACTIVE]
            assignments = await StudyRepository(session).open_assignments(user_id)
            done_minutes_by_exam = await StudyRepository(session).done_minutes_per_exam(user_id)
            planned = await StudyRepository(session).planned_sessions_from(user_id, now_utc())

        exam_inputs: list[ExamInput] = []
        for e in exams:
            if e.exam_at:
                event_day = local_from_utc(e.exam_at, tz).date()
            elif e.exam_date:
                event_day = e.exam_date
            else:
                continue
            done = int(done_minutes_by_exam.get(e.id, 0))
            remaining = max(0, int(e.est_prep_minutes or 300) - done)
            exam_inputs.append(
                ExamInput(
                    id=e.id,
                    subject_id=e.subject_id or 0,
                    title=e.title,
                    event_day=event_day,
                    minutes_remaining=remaining,
                    importance=e.importance or 3,
                    difficulty=e.difficulty or 3,
                )
            )

        assign_inputs: list[AssignmentInput] = []
        for a in assignments:
            if a.status not in (AssignmentStatus.PENDING, AssignmentStatus.IN_PROGRESS):
                continue
            if a.due_at:
                due_day = local_from_utc(a.due_at, tz).date()
            elif a.due_date:
                due_day = a.due_date
            else:
                due_day = today + timedelta(days=7)
            assign_inputs.append(
                AssignmentInput(
                    id=a.id,
                    subject_id=a.subject_id or 0,
                    description=a.description,
                    due_day=due_day,
                    est_minutes=int(a.est_minutes or 60),
                    priority=a.priority or 2,
                )
            )

        already_planned: dict = {}
        for s in planned:
            if s.status != SessionStatus.PLANNED:
                continue
            d = local_from_utc(s.start_at, tz).date()
            already_planned[d] = already_planned.get(d, 0) + int(s.planned_minutes or 0)

        return plan_study(
            today,
            exam_inputs,
            assign_inputs,
            ctx["weekly"],
            default_day_minutes=ctx["daily_default"],
            start_hour=ctx["start_hour"],
            end_hour=ctx["end_hour"],
            already_planned_by_day=already_planned,
        )

    async def save_proposal(self, user_id: int, proposal: PlanProposal) -> StudyPlan:
        ctx = await self.user_context(user_id)
        tz = ctx["tz"]
        today = today_in_zone(tz)
        async with self.session_factory() as session:
            repo = StudyRepository(session)
            await repo.supersede_all(user_id)
            all_plans = (await session.execute(sa.select(StudyPlan).where(StudyPlan.user_id == user_id))).scalars().all()
            version = max([p.version or 1 for p in all_plans], default=0) + 1
            plan = await repo.create_plan(
                user_id,
                start_date=today,
                end_date=today + timedelta(days=proposal.horizon_days),
                criteria={
                    "weekly": ctx["weekly"],
                    "daily_default": ctx["daily_default"],
                    "start_hour": ctx["start_hour"],
                    "end_hour": ctx["end_hour"],
                },
                version=version,
            )
            count = await self._create_sessions(session, user_id, plan.id, proposal, tz, ctx["start_hour"])
            await repo.set_plan_status(user_id, plan.id, PlanStatus.PROPOSED)
            AuditRepository(session).add(
                user_id, AuditAction.PLAN_CREATE, "study_plan", plan.id,
                {"version": version, "sessions": count, "conflicts": len(proposal.conflicts)},
            )
            await session.commit()
            return plan

    @staticmethod
    async def _create_sessions(session, user_id: int, plan_id: int, proposal: PlanProposal, tz: str, start_hour: int) -> int:
        by_day: dict[date, list[SessionProposal]] = {}
        for s in proposal.sessions:
            by_day.setdefault(s.day, []).append(s)
        count = 0
        for day in sorted(by_day):
            cursor = start_hour * 60  # minutes from midnight, local
            for s in sorted(by_day[day], key=lambda x: x.slot):
                start = from_local_to_utc(day, cursor // 60, cursor % 60, tz)
                end_minutes = cursor + s.minutes
                end = from_local_to_utc(day, end_minutes // 60, end_minutes % 60, tz)
                repo = StudyRepository(session)
                await repo.create_session(
                    user_id,
                    start,
                    end,
                    subject_id=s.subject_id or None,
                    exam_id=s.ref_id if s.kind == "exam_prep" else None,
                    assignment_id=s.ref_id if s.kind == "assignment" else None,
                    plan_id=plan_id,
                    planned_minutes=s.minutes,
                    objective=s.target_label[:220],
                    status=SessionStatus.PLANNED,
                )
                count += 1
                cursor += s.minutes + SLOT_BREAK_MINUTES
        return count

    # ---------------------------------------------------------------- lifecycle
    async def approve_plan(self, user_id: int, plan_id: int) -> bool:
        async with self.session_factory() as session:
            plan = await StudyRepository(session).get_plan_owned(user_id, plan_id)
            if not plan:
                return False
            await StudyRepository(session).supersede_all(user_id)
            updated = await StudyRepository(session).set_plan_status(user_id, plan_id, PlanStatus.ACTIVE)
            AuditRepository(session).add(user_id, AuditAction.PLAN_APPROVE, "study_plan", plan_id, {})
            await session.commit()
            return updated is not None and updated.status == PlanStatus.ACTIVE

    async def discard_plan(self, user_id: int, plan_id: int) -> bool:
        async with self.session_factory() as session:
            plan = await StudyRepository(session).get_plan_owned(user_id, plan_id)
            if not plan or plan.status != PlanStatus.PROPOSED:
                return False
            await StudyRepository(session).delete_planned_by_plan(user_id, plan_id)
            await session.delete(plan)
            await session.commit()
            return True

    async def current_plan(self, user_id: int) -> StudyPlan | None:
        async with self.session_factory() as session:
            return await StudyRepository(session).active_plan(user_id)

    async def generate_new_proposal(self, user_id: int) -> tuple[StudyPlan, PlanProposal]:
        proposal = await self.build_proposal(user_id)
        plan = await self.save_proposal(user_id, proposal)
        return plan, proposal

    async def replan(self, user_id: int) -> tuple[StudyPlan, PlanProposal]:
        """Recalculate remaining plan: done/missed sessions are preserved automatically
        because build_proposal only counts planned (not done) sessions as blocked time."""
        return await self.generate_new_proposal(user_id)

    # ---------------------------------------------------------------- session actions
    async def mark_session_done(self, user_id: int, session_id: int) -> bool:
        async with self.session_factory() as session:
            row = await StudyRepository(session).update_session_owned(
                user_id, session_id, status=SessionStatus.DONE, completed_at=now_utc()
            )
            await session.commit()
            return row is not None

    async def mark_session_missed(self, user_id: int, session_id: int) -> bool:
        async with self.session_factory() as session:
            row = await StudyRepository(session).update_session_owned(user_id, session_id, status=SessionStatus.MISSED)
            await session.commit()
            return row is not None

    async def mark_all_future_missed(self, user_id: int) -> int:
        """Mark every still-PLANNED session as missed (used by 'Ninguna hecha')."""
        async with self.session_factory() as session:
            repo = StudyRepository(session)
            rows = await repo.planned_sessions_from(user_id, now_utc())
            count = 0
            for s in rows:
                if s.status != SessionStatus.PLANNED:
                    continue
                updated = await repo.update_session_owned(user_id, s.id, status=SessionStatus.MISSED)
                if updated is not None:
                    count += 1
            await session.commit()
            return count

    # ---------------------------------------------------------------- formatting
    @staticmethod
    def format_proposal(proposal: PlanProposal, start_hour: int) -> str:
        lines: list[str] = []
        total = sum(s.minutes for s in proposal.sessions)
        lines.append(f"<b>Plan de estudio propuesto</b> ({total} min de estudio)")
        lines.append(f"Necesidad estimada: {proposal.total_needed} min | disponible: {proposal.total_available} min")
        if proposal.conflicts:
            lines.append("")
            lines.append("<b>Avisos</b>")
            for c in proposal.conflicts[:8]:
                lines.append(f"- {c}")
        lines.append("")
        by_day: dict = {}
        for s in proposal.sessions:
            by_day.setdefault(s.day, []).append(s)
        weekdays = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]
        for day in sorted(by_day):
            lines.append(f"<b>{weekdays[day.weekday()]} {day.strftime('%d/%m')}</b>")
            cursor = start_hour * 60
            for s in sorted(by_day[day], key=lambda x: x.slot):
                hh, mm = divmod(cursor, 60)
                lines.append(f"  {hh:02d}:{mm:02d} - {s.minutes} min - {s.target_label[:80]}")
                cursor += s.minutes + SLOT_BREAK_MINUTES
        if not proposal.sessions:
            lines.append("(Sin sesiones: no hay examenes ni tareas pendientes en el horizonte.)")
        return "\n".join(lines)

    @staticmethod
    def start_hour_of(plan: StudyPlan) -> int:
        return int((plan.criteria or {}).get("start_hour", 16))
