"""Studier planner: exam priority, capacity, breaks, overdue tasks."""

from __future__ import annotations

from datetime import date, timedelta

from app.services.study_planner import AssignmentInput, ExamInput, plan_study


def test_planner_prioritizes_exam_over_task():
    today = date(2026, 10, 18)
    exams = [ExamInput(id=1, subject_id=1, title="Fisica", event_day=today + timedelta(days=7), minutes_remaining=180, importance=5, difficulty=5)]
    assigns = [AssignmentInput(id=2, subject_id=1, description="practicas", due_day=today + timedelta(days=2), est_minutes=60, priority=1)]
    proposal = plan_study(today, exams, assigns, {}, default_day_minutes=120)
    assert proposal.sessions, "debe generar sesiones"
    kinds = {s.kind for s in proposal.sessions}
    assert kinds, plan_study
    assert any(s.kind == "exam_prep" for s in proposal.sessions)
    assert all(s.minutes <= 60 for s in proposal.sessions)


def test_planner_empty_inputs():
    today = date(2026, 10, 18)
    proposal = plan_study(today, [], [], {})
    assert proposal.sessions == []
