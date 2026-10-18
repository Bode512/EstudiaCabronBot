"""Shared fixtures: temp sqlite DB + services with NullNotifier."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_TELEGRAM_IDS", "1")


@pytest_asyncio.fixture
async def tmp_settings(tmp_path: Path):
    from app.config import reset_settings_cache

    os.environ["DATABASE_URL"] = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    os.environ["STORAGE_DIR"] = str(tmp_path / "storage")
    reset_settings_cache()
    from app.config import get_settings

    return get_settings()


@pytest_asyncio.fixture
async def ctx(tmp_settings):
    from app.bot.context import attach_notifier, build_context
    from app.database.engine import build_sync_engine
    from app.database.models import Base
    from app.services.notifications import NullNotifier

    sync_engine = build_sync_engine(tmp_settings.sync_database_url)
    Base.metadata.create_all(bind=sync_engine)
    sync_engine.dispose()

    ctx = build_context(tmp_settings)
    attach_notifier(ctx, NullNotifier())
    yield ctx


@pytest_asyncio.fixture
async def user_row(ctx):
    user, _created = await ctx.users.register(42, "Bode", chat_id=42)
    return user
