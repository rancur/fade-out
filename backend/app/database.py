"""SQLAlchemy database setup with async support."""

import logging
import os
from typing import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Convert sqlite:/// to sqlite+aiosqlite:///
_db_url = settings.DATABASE_URL
if _db_url.startswith("sqlite:///"):
    _db_url = _db_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)

# Ensure the data directory exists for SQLite. Do this best-effort: importing a
# module must not hard-crash the process just because the target dir is not
# writable yet (e.g. a read-only/sandboxed host or a not-yet-mounted volume).
# If creation fails here, the engine will surface a clear error on first connect.
if "sqlite" in _db_url:
    db_path = _db_url.split("///")[-1]
    db_dir = os.path.dirname(db_path)
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Could not create database directory %s at import: %s", db_dir, exc
            )

engine = create_async_engine(
    _db_url,
    echo=False,
    future=True,
    # SQLite with several concurrent writers (pipeline task, activity/log
    # writes, API sessions) needs a busy timeout, or contention surfaces as
    # instant "database is locked" errors instead of a short wait.
    connect_args={"timeout": 30} if "sqlite" in _db_url else {},
)

if "sqlite" in _db_url:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        # WAL lets readers proceed while one writer commits; busy_timeout
        # makes late writers queue instead of erroring out.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


async def init_db() -> None:
    """Create all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
