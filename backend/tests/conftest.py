"""Shared test setup.

Point the app at a throwaway SQLite file in the OS temp dir BEFORE any
``app.*`` module is imported, so importing the app under test never touches the
production ``/data`` volume (and never needs it to be writable).

The file is UNIQUE PER PROCESS (PID-suffixed): a fixed path let concurrent
pytest runs (other worktrees, a second terminal, CI) race each other's
drop_all/create_all on the same sqlite file, which surfaced as random
"no such table: activity_events" errors and phantom/missing rows in whichever
test lost the race.
"""

import os
import tempfile

_TEST_DB_PATH = os.path.join(
    tempfile.gettempdir(), f"fadeout_test_{os.getpid()}.db"
)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_DB_PATH}")

import httpx
import pytest
import pytest_asyncio


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_db():
    """Remove this process's DB (and its WAL/SHM journals) after the run."""
    yield
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_TEST_DB_PATH + suffix)
        except OSError:
            pass


@pytest_asyncio.fixture
async def prepared_db():
    """Create a fresh set of tables for a test, dropping them afterward.

    Uses the app's own async engine/metadata against the throwaway SQLite file
    configured above, so every test that needs the DB starts from empty tables.

    The engine's connection pool is disposed on both sides of the test:
    pytest-asyncio gives every test its own event loop, and aiosqlite
    connections pooled under a previous test's (now-closed) loop must not leak
    into this one — each test starts and ends with an empty pool.
    """
    from app.database import Base, engine
    import app.models  # noqa: F401  (registers models on Base.metadata)

    await engine.dispose()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(prepared_db, monkeypatch):
    """An httpx.AsyncClient wired to the real ASGI app via ASGITransport.

    The orchestrator's pipeline-launching methods are stubbed to no-ops so the
    HTTP/DB layer can be exercised without kicking off real background pipelines.
    """
    from app.main import app, orchestrator

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(orchestrator, "start_pipeline", _noop)
    monkeypatch.setattr(orchestrator, "resume_pipeline", _noop)
    monkeypatch.setattr(orchestrator, "retry_step", _noop)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
