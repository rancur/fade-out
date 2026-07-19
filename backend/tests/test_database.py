"""Tests for the SQLite concurrency setup (WAL + busy timeout pragmas).

Production hit repeated ``sqlite3.OperationalError: database is locked`` with
several concurrent writers (pipeline task, activity/log writes, API sessions).
Every new connection must come up in WAL mode with a 30s busy timeout so late
writers queue instead of erroring out instantly.
"""

from sqlalchemy import text


class TestSqlitePragmas:
    async def test_new_connections_get_wal_and_busy_timeout(self):
        from app.database import engine

        async with engine.connect() as conn:
            journal_mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
            busy_timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
            synchronous = (await conn.execute(text("PRAGMA synchronous"))).scalar()

        # File-backed test DB -> WAL sticks. (On :memory: sqlite silently keeps
        # journal_mode='memory', which is harmless — but the test DB is a file.)
        assert str(journal_mode).lower() == "wal"
        assert int(busy_timeout) == 30000
        assert int(synchronous) == 1  # NORMAL
