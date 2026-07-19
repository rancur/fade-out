"""Shared test setup.

Point the app at a throwaway SQLite file in the OS temp dir BEFORE any
``app.*`` module is imported, so importing the app under test never touches the
production ``/data`` volume (and never needs it to be writable).
"""

import os
import tempfile

# The filename is per-process: a shared name collides when two checkouts run
# their suites concurrently (each recreates the table with its own schema).
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(tempfile.gettempdir(), f'fadeout_test_{os.getpid()}.db')}",
)

import httpx
import pytest_asyncio


@pytest_asyncio.fixture
async def prepared_db():
    """Create a fresh set of tables for a test, dropping them afterward.

    Uses the app's own async engine/metadata against the throwaway SQLite file
    configured above, so every test that needs the DB starts from empty tables.
    """
    from app.database import Base, engine
    import app.models  # noqa: F401  (registers models on Base.metadata)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


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
