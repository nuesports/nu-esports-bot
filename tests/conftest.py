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


class FakeMessage:
    """Stands in for whatever ctx.followup.send()/channel.send() returns -- just
    enough surface for callers that edit or delete the message afterwards."""
    def __init__(self):
        self.edit_calls = []
        self.deleted = False

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)

    async def delete(self):
        self.deleted = True


class FakeFollowup:
    def __init__(self):
        self.send_calls = []

    async def send(self, *args, **kwargs):
        self.send_calls.append(kwargs)
        return FakeMessage()


class FakeApplicationContext:
    """Stands in for discord.ApplicationContext -- covers the handful of attributes
    and methods command handlers actually touch (defer/respond/followup, author/user,
    channel), shared across test files since several commands need the same shape."""
    def __init__(self, author, channel=None):
        self.author = author
        self.user = author
        self.channel = channel
        self.deferred = False
        self.respond_calls = []
        self.followup = FakeFollowup()

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def respond(self, *args, **kwargs):
        self.respond_calls.append(kwargs)


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