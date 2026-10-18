"""Services container wired into the Telegram application (bot_data singleton)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings, get_settings
from app.database.engine import build_engine, make_session_factory
from app.services.ai_provider import AIProvider, build_ai_provider
from app.services.documents import DocumentService
from app.services.planning_service import PlanningService
from app.services.reminders import ReminderService
from app.services.users import UserService


@dataclass
class BotContext:
    settings: Settings
    session_factory: async_sessionmaker
    users: UserService
    reminders: ReminderService
    documents: DocumentService
    planning: PlanningService
    ai: AIProvider

    @property
    def owner_ids(self) -> set[int]:
        return set(self.settings.owner_telegram_ids)

    def is_owner(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self.owner_ids


def build_context(settings: Settings | None = None) -> BotContext:
    settings = settings or get_settings()
    engine = build_engine(settings)
    session_factory = make_session_factory(engine)
    users = UserService(session_factory, settings)
    reminders = ReminderService(session_factory, notifier=None, settings=settings)  # notifier injected at startup
    documents = DocumentService(session_factory, settings)
    planning = PlanningService(session_factory, settings)
    ai = build_ai_provider(settings)
    context = BotContext(
        settings=settings,
        session_factory=session_factory,
        users=users,
        reminders=reminders,
        documents=documents,
        planning=planning,
        ai=ai,
    )
    return context


def attach_notifier(context: BotContext, notifier) -> None:
    context.reminders.notifier = notifier
