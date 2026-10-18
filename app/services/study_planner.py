"""Deterministic study planner.

`plan_study()` is a PURE function (no DB, no AI, fully testable). The service layer
gathers inputs, converts to/from UTC datetimes, and persists proposals ONLY after the
user approves. AI never decides dates or writes the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

MAX_SESSION_MINUTES = 60
BREAK_MINUTES = 10
DEFAULT_HORIZON_DAYS = 14


@dataclass(frozen=True)
class ExamInput:
    id: int
    subject_id: int
    title: str
    event_day: date            # local (user tz) exam day
    minutes_remaining: int     # preparation still needed (after credit for done sessions)
    importance: int = 3        # 1..5
    difficulty: int = 3        # 1..5


@dataclass(frozen=True)
class AssignmentInput:
    id: int
    subject_id: int
    description: str
    due_day: date              # local deadline day (None-capable via due_day=None is disallowed here)
    est_minutes: int
    priority: int = 2          # 1 alta, 2 media, 3 baja


@dataclass
class SessionProposal:
    day: date
    subject_id: int
    target_label: str          # "Examen: titulo" | "Tarea: descripcion"
    minutes: int
    kind: str                  # 'exam_prep' | 'assignment'
    ref_id: int
    priority: int              # 1..5 (1 = most urgent)
    slot: int                  # 0-based same-day ordering


@dataclass
class PlanProposal:
    sessions: list[SessionProposal] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    total_needed: int = 0
    total_available: int = 0
    horizon_days: int = DEFAULT_HORIZON_DAYS

    @property
    def ok(self) -> bool:
        return not self.conflicts


def _daily_weights(days_left: int) -> dict[int, int]:
    """Transparent review weighting: more intense in the 3 days before the exam."""
    base = {d: 1 for d in range(days_left)}
    for d in (0, 1):
        if d in base:
            base[d] = 3
        if d in (2, 3):
            base[d] = 2
    return base


def plan_study(
    today: date,
    exams: list[ExamInput],
    assignments: list[AssignmentInput],
    availability: dict[int, int],
    default_day_minutes: int = 120,
    start_hour: int = 16,
    end_hour: int = 22,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    already_planned_by_day: dict[date, int] | None = None,
) -> PlanProposal:
    already_planned_by_day = already_planned_by_day or {}
    horizon_end = today + timedelta(days=horizon_days)

    exams = sorted([e for e in exams if e.event_day >= today and e.minutes_remaining > 0], key=lambda e: (e.event_day, -e.importance, -e.difficulty))
    assignments = sorted([a for a in assignments if a.due_day is not None and a.est_minutes > 0], key=lambda a: (a.due_day, a.priority))
    exams = [e for e in exams if e.event_day <= horizon_end]
    assignments = [a for a in assignments if a.due_day <= horizon_end]

    total_needed = sum(e.minutes_remaining for e in exams) + sum(a.est_minutes for a in assignments)
    day_capacity: dict[date, int] = {}
    for offset in range(horizon_days + 1):
        d = today + timedelta(days=offset)
        cap = availability.get(d.weekday(), default_day_minutes)
        cap -= already_planned_by_day.get(d, 0)
        # clamp to the study window hours
        cap = min(cap, max(0, (end_hour - start_hour) * 60 - BREAK_MINUTES * 4))
        day_capacity[d] = max(0, cap)
    total_available = sum(day_capacity.values())

    proposal = PlanProposal(total_needed=total_needed, total_available=total_available, horizon_days=horizon_days)

    # Remaining needs as mutable copies
    exam_need: dict[int, int] = {e.id: e.minutes_remaining for e in exams}
    assign_need: dict[int, int] = {a.id: a.est_minutes for a in assignments}
    exams_by_id = {e.id: e for e in exams}

    remaining_by_day_capacity = dict(day_capacity)

    # Pass 1: finals review in the last 2-3 days before each exam (highest priority).
    for e in sorted(exams, key=lambda x: x.event_day, reverse=True):
        for back in (1, 2):
            d = e.event_day - timedelta(days=back)
            if d < today or d not in remaining_by_day_capacity or exam_need[e.id] <= 0:
                continue
            give = min(remaining_by_day_capacity[d], exam_need[e.id])
            if give <= 0:
                continue
            _add(proposal, exams_by_id, assign_need, exam_need, remaining_by_day_capacity, e, d, give)

    # Pass 2: rest of exam prep, day by day, urgency-first.
    for offset in range(horizon_days + 1):
        d = today + timedelta(days=offset)
        cap = remaining_by_day_capacity.get(d, 0)
        while cap > 0:
            candidates = [e for e in exams if exam_need[e.id] > 0]
            if not candidates:
                break
            candidates.sort(key=_exam_priority_key(d, event_days(exams_by_id)))
            pick = candidates[0]
            give = min(cap, exam_need[pick.id], MAX_SESSION_MINUTES) if cap > MAX_SESSION_MINUTES else min(cap, exam_need[pick.id])
            give = min(give, exam_need[pick.id])
            if give <= 0:
                break
            _add(proposal, exams_by_id, assign_need, exam_need, remaining_by_day_capacity, pick, d, give)
            cap = remaining_by_day_capacity.get(d, 0)

    # Pass 3: assignments, in due order, into leftover capacity within the horizon.
    for offset in range(horizon_days + 1):
        d = today + timedelta(days=offset)
        cap = remaining_by_day_capacity.get(d, 0)
        while cap > 0:
            candidates = [a for a in assignments if assign_need[a.id] > 0]
            if not candidates:
                break
            pick = candidates[0]
            give = min(cap, assign_need[pick.id], MAX_SESSION_MINUTES)
            _add(proposal, exams_by_id, assign_need, exam_need, remaining_by_day_capacity, pick, d, give)
            cap = remaining_by_day_capacity.get(d, 0)

    # Conflicts
    for e in exams:
        left = exam_need[e.id]
        if left > 0:
            proposal.conflicts.append(
                f"Examen '{e.title}': faltan {left} min de preparacion y no hay espacio hasta el {e.event_day.strftime('%d/%m')}."
            )
    for a in assignments:
        left = assign_need[a.id]
        if left > 0 and a.due_day <= horizon_end:
            proposal.conflicts.append(
                f"Tarea '{a.description[:40]}': faltan {left} min antes del {a.due_day.strftime('%d/%m')}."
            )
    if total_needed > total_available:
        proposal.conflicts.insert(
            0,
            f"Deficit global: necesitas aprox. {total_needed} min pero dispones de {total_available} min en los proximos {horizon_days} dias.",
        )
    # de-dup
    proposal.conflicts = list(dict.fromkeys(proposal.conflicts))
    _assign_priorities(proposal, today)
    return proposal


def event_days(exams_by_id: dict[int, ExamInput]) -> dict[int, date]:
    return {eid: e.event_day for eid, e in exams_by_id.items()}


def _exam_priority_key(day: date, event_days_map: dict[int, date]):
    def key(e: ExamInput):
        days_until = (event_days_map.get(e.id, day) - day).days
        return (max(0, days_until), -e.importance, -e.difficulty)

    return key


def _add(
    proposal: PlanProposal,
    exams_by_id: dict[int, ExamInput],
    assign_need: dict[int, int],
    exam_need: dict[int, int],
    capacity: dict[date, int],
    target,
    day: date,
    minutes: int,
) -> None:
    if minutes <= 0:
        return
    if hasattr(target, "event_day"):  # exam
        minutes = min(minutes, exam_need[target.id])
        exam_need[target.id] -= minutes
        subject = target.subject_id
        label = f"Examen: {target.title}"
        kind, ref = "exam_prep", target.id
    else:  # assignment
        minutes = min(minutes, assign_need[target.id])
        assign_need[target.id] -= minutes
        subject = target.subject_id
        label = f"Tarea: {target.description}"
        kind, ref = "assignment", target.id
    capacity[day] = max(0, capacity.get(day, 0) - minutes)
    if minutes <= 0:
        return
    # split into session-sized chunks <= MAX_SESSION_MINUTES
    left = minutes
    while left > 0:
        chunk = min(MAX_SESSION_MINUTES, left)
        slot = sum(1 for s in proposal.sessions if s.day == day)
        proposal.sessions.append(
            SessionProposal(day=day, subject_id=subject, target_label=label, minutes=chunk, kind=kind, ref_id=ref, priority=3, slot=slot)
        )
        left -= chunk


def _assign_priorities(proposal: PlanProposal, today: date) -> None:
    for s in proposal.sessions:
        if s.kind == "assignment":
            s.priority = 1
        else:
            s.priority = 2
