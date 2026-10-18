"""Temporary integration smoke test (services only, no Telegram)."""

import asyncio
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, ".")

_tmp = Path(tempfile.mkdtemp(prefix="estudia_smoke_"))
os.environ["TELEGRAM_BOT_TOKEN"] = "123:abc"
os.environ["OWNER_TELEGRAM_IDS"] = "1"

from app.config import get_settings  # noqa: E402

settings_env = get_settings  # noqa: F841


async def main() -> None:
    import sqlalchemy as sa

    from app.bot.context import attach_notifier, build_context
    from app.config import reset_settings_cache
    from app.database.models import Reminder, ReminderStatus, StudySession, utcnow
    from app.database.repositories import AuditRepository
    from app.scheduler.recovery import run_startup_recovery
    from app.services.assignments import AssignmentService
    from app.services.exams import ExamService
    from app.services.notifications import NullNotifier

    # point settings at a temp dir BEFORE building context
    os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'smoke.db').as_posix()}"
    os.environ["STORAGE_DIR"] = str(_tmp / "storage")
    reset_settings_cache()
    s = get_settings()
    print("db:", s.db_path)

    ctx = build_context(s)
    attach_notifier(ctx, NullNotifier())
    summary = await run_startup_recovery(ctx.reminders, ctx)
    print("recovery:", summary)

    user, created = await ctx.users.register(555, "Bode", chat_id=555)
    print("user:", user.id, created)

    async with ctx.session_factory() as ses:
        audit = AuditRepository(ses)
        exam = await ExamService(ses).create_exam(
            user.id, audit, title="Derivadas", subject_name="Matematicas", exam_date=date(2026, 11, 20)
        )
        await ses.commit()
        n = (
            await ses.execute(
                sa.select(sa.func.count()).select_from(Reminder).where(Reminder.status == ReminderStatus.PENDING)
            )
        ).scalar_one()
        print("exam reminders pending:", n, "(exam id", exam.id, ")")
        task, cre = await AssignmentService(ses).create_assignment(
            user.id, audit, description="Problemas 1-10", subject_name="Matematicas", due_date=date(2026, 10, 25)
        )
        await ses.commit()
        print("task created:", cre)

    plan, proposal = await ctx.planning.generate_new_proposal(user.id)
    print("plan sessions:", len(proposal.sessions), "conflicts:", len(proposal.conflicts))
    ok = await ctx.planning.approve_plan(user.id, plan.id)
    print("plan approved:", ok)
    async with ctx.session_factory() as ses:
        sessions = (await ses.execute(sa.select(StudySession).where(StudySession.user_id == user.id))).scalars().all()
        print("sessions rows:", len(sessions), [s.status for s in sessions][:3])

    # age all pending reminders -> catch-up
    async with ctx.session_factory() as ses:
        await ses.execute(
            sa.update(Reminder)
            .where(Reminder.status == ReminderStatus.PENDING)
            .values(send_at=utcnow() - timedelta(days=1))
        )
        await ses.commit()
    groups = await ctx.reminders.catch_up_plan()
    print("catchup groups:", list(groups.keys()))
    flushed = await ctx.reminders.flush_overdue_queue(groups)
    print("flushed:", flushed)
    n = await ctx.reminders.run_digest_pass()
    print("digest sent:", n)
    print("SMOKE OK")


asyncio.run(main())
