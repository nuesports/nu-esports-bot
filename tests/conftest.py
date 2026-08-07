import asyncio
import shutil
import sys
from pathlib import Path

import pytest_asyncio

# psycopg's async mode can't run on Windows' default ProactorEventLoop -- only
# matters for local dev on Windows, Linux (and so CI) has no such split.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

def _stage(example_name: str, real_name: str) -> None:
    """Stages the config and secrets yamls if they exist.
    If not, copies the examples to be used instead"""
    example = Path(example_name)
    real = Path(real_name)
    if not real.exists():
        shutil.copy(example, real)

_stage("config.example.yaml", "config.yaml")
_stage("secrets.example.yaml", "secrets.yaml")


@pytest_asyncio.fixture(scope="session")
async def migrated_db():
    """Applies real migrations and opens the connection pool, once per test session,
    against whatever Postgres secrets.yaml points at -- the CI service container, or
    a local `docker compose -f compose.yaml -f compose.dev.yaml up db` when running
    these tests by hand. Only requested by tests that actually need a real database,
    so the rest of the suite stays fast and dependency-free.

    Async (not asyncio.run()-wrapped) so it shares the session-scoped event loop
    configured in pyproject.toml -- the pool it opens has to stay valid for every
    test that uses it, and an async connection pool can't cross event loops."""
    from utils import db, migrate
    await migrate.run_migrations()
    await db.open_pool()