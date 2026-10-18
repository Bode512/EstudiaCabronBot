"""SQLAlchemy engines and session factories (async for the bot, sync for Alembic/scripts)."""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings

_SQLITE_PRAGMAS = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
)


def _set_pragmas_sync(con: "sqlite3.Connection") -> None:
    cur = con.cursor()
    try:
        for stmt in _SQLITE_PRAGMAS:
            cur.execute(stmt)
    finally:
        cur.close()


async def _set_pragmas_async(con) -> None:
    for stmt in _SQLITE_PRAGMAS:
        await con.execute(stmt)


def _apply_sqlite_pragmas(dbapi_connection, connection_record=None) -> None:
    if isinstance(dbapi_connection, sqlite3.Connection) or type(dbapi_connection).__module__.startswith("sqlite3"):
        _set_pragmas_sync(dbapi_connection)
        return
    # aiosqlite: async API, driven through run_async so the sync listener can apply pragmas
    run_async = getattr(dbapi_connection, "run_async", None)
    if run_async is None:
        raise TypeError(f"Unsupported DBAPI connection type: {type(dbapi_connection)!r}")
    run_async(_set_pragmas_async)


@lru_cache(maxsize=4)
def _build_async_engine_cached(url: str) -> AsyncEngine:
    engine = create_async_engine(url, echo=False)
    event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    return _build_async_engine_cached(settings.async_database_url)


@lru_cache(maxsize=4)
def _build_sync_engine_cached(url: str) -> Engine:
    from sqlalchemy import create_engine

    engine = create_engine(url, echo=False)
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


def build_sync_engine(url: str | None = None) -> Engine:
    if url is None:
        url = get_settings().sync_database_url
    return _build_sync_engine_cached(url)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    from sqlalchemy.ext.asyncio import AsyncSession

    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
