"""Handler-level integration test: drives the /anadir conversation with minimal stubs."""

from __future__ import annotations

import asyncio
from datetime import date

import sqlalchemy as sa

from app.database.models import Exam, Reminder


class ChatStub:
    id = 987
    type = "private"


class UserStub:
    def __init__(self, uid: int, first_name: str = "T"):
        self.id = uid
        self.first_name = first_name
        self.username = None
        self.full_name = first_name


class MsgStub:
    def __init__(self, text: str, user: UserStub, chat=None):
        self.text = text
        self.chat = chat or ChatStub()
        self.from_user = user
        self.entities = []
        self.replies: list[tuple[str, str, object]] = []

    async def reply_text(self, text, **kw):
        self.replies.append(("text", text, kw.get("reply_markup")))
        return self

    async def reply_html(self, text, **kw):
        self.replies.append(("html", text, kw.get("reply_markup")))
        return self

    async def edit_text(self, text, **kw):
        self.replies.append(("edit", text, kw.get("reply_markup")))
        return self


class QueryStub:
    def __init__(self, message: MsgStub, data: str):
        self.message = message
        self.data = data
        self.answered: str | None = None
        self.replies = message.replies

    async def answer(self, text=None, **kw):
        self.answered = text or ""

    @property
    def from_user(self):
        return self.message.from_user


class UpdateStub:
    def __init__(self, message: MsgStub | None = None, query: QueryStub | None = None, user: UserStub | None = None):
        self.message = message
        self.callback_query = query
        if message is not None:
            self.from_user = message.from_user
        elif query is not None:
            self.from_user = (query.message or message or ChatStub()) and query.message.from_user
        if user is not None:
            self.from_user = user

    @property
    def effective_message(self):
        return self.message or (self.callback_query.message if self.callback_query else None)

    @property
    def effective_user(self):
        return self.from_user

    @property
    def effective_chat(self):
        return (self.effective_message.chat) if self.effective_message else ChatStub()


def make_ctx(ctx):
    class CtxStub:
        pass

    c = CtxStub()
    c.bot_data = {"ctx": ctx}
    c.user_data = {}
    return c


def test_full_anadir_flow_exam_21_11(ctx, user_row):
    from app.bot.handlers import anadir as A

    uid = user_row.telegram_user_id
    user = UserStub(uid)

    async def run():
        c = make_ctx(ctx)
        msg = MsgStub("/anadir", user)
        update = UpdateStub(message=msg, user=user)
        state = await A.cmd_anadir(update, c)
        assert state == A.CHOOSE_TYPE, msg.replies

        qmsg = MsgStub("noop", user)
        q = QueryStub(qmsg, "ecb:conv:0:type:exam")
        state = await A.on_type(UpdateStub(query=q, user=user), c)
        assert state == A.CHOOSE_SUBJECT, qmsg.replies

        # "Otra asignatura..." -> free text subject
        msg_s = MsgStub("Fisica", user)
        state = await A.on_subject(UpdateStub(message=msg_s, user=user), c)
        assert state == A.ASK_TITLE, msg_s.replies

        msg_t = MsgStub("Cinematica", user)
        state = await A.on_title(UpdateStub(message=msg_t, user=user), c)
        assert state == A.ASK_DATE

        msg_d = MsgStub("21/11", user)
        state = await A.on_date(UpdateStub(message=msg_d, user=user), c)
        assert state == A.CONFIRM_DRAFT, msg_d.replies

        qmsg2 = MsgStub("noop", user)
        q2 = QueryStub(qmsg2, "ecb:conv:0:draft_ok")
        state = await A.on_draft_ok(UpdateStub(query=q2, user=user), c)
        assert state == 6 or state == -1 or state is None or state == -2, state  # end states
        joined = "\n".join(str(r[1]) for r in q2.replies)
        assert "Examen creado" in joined, joined

        async with ctx.session_factory() as ses:
            exams = (await ses.execute(sa.select(Exam).where(Exam.user_id == user_row.id))).scalars().all()
            assert exams, "exam not found"
            assert any("Cinematica" in e.title for e in exams)
            assert all(e.user_id == user_row.id for e in exams)
            reminder_rows = (
                await ses.execute(sa.select(Reminder).where(Reminder.user_id == user_row.id))
            ).scalars().all()
            assert len(reminder_rows) == 4

    asyncio.run(run())


def test_nl_router_exam_21_11(ctx, user_row):
    from app.bot.handlers import nl_router as NL

    uid = user_row.telegram_user_id
    user = UserStub(uid)

    async def run():
        c = make_ctx(ctx)
        c.user_data = {}
        msg = MsgStub("Examen de Biologia el 21/11", user)
        upd = UpdateStub(message=msg, user=user)
        await NL.on_free_text(upd, c)
        async with ctx.session_factory() as ses:
            exams = (await ses.execute(sa.select(Exam).where(Exam.user_id == user_row.id))).scalars().all()
            match = [e for e in exams if "Biologia" in e.title]
            assert match, exams
            assert match[0].exam_date == date(2026, 11, 21)

        out = "\n".join(str(r[1]) for r in msg.replies)
        assert "Creado examen" in out, out

    asyncio.run(run())


