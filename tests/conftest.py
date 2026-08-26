import asyncio
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

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


def select_interaction():
    """Stands in for the interaction a Select records when someone picks an option.

    Tests force a selection by setting `select._selected_values` and `select._interaction`
    by hand. py-cord 2.7 changed Select.values to read `self._interaction.data` before
    handing back those values, where 2.6 only checked that _interaction was set -- so a
    bare object() sentinel now raises AttributeError. Only needs `.data` to be non-None:
    for a string select, values returns _selected_values as soon as that check passes."""
    return SimpleNamespace(data={})


class FakeMessage:
    """Stands in for whatever ctx.followup.send()/channel.send() returns -- just
    enough surface for callers that edit or delete the message afterwards."""
    def __init__(self):
        self.edit_calls = []
        self.replies = []
        self.deleted = False

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)

    async def reply(self, content=None, **kwargs):
        self.replies.append(content)
        return FakeMessage()

    async def delete(self):
        self.deleted = True


class FakeFollowup:
    def __init__(self):
        self.send_calls = []

    async def send(self, content=None, **kwargs):
        self.send_calls.append({"content": content, **kwargs})
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


class FakeInteractionResponse:
    """Stands in for interaction.response. Every branch a button/modal callback can
    take ends in exactly one of these calls, so recording them is how tests tell
    which branch ran."""
    def __init__(self):
        self.messages = []
        self.edits = []
        self.modals = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def defer(self, *args, **kwargs):
        self.deferred = True


class FakeInteraction:
    """Stands in for discord.Interaction -- the component-callback counterpart to
    FakeApplicationContext, which only covers slash commands. Views and modals reach
    for response/followup/user/client, so those are what this carries."""
    def __init__(self, user, client=None):
        self.user = user
        self.client = client
        self.response = FakeInteractionResponse()
        self.followup = FakeFollowup()
        self.original_response_deleted = False
        self.original_response_edits = []

    async def edit_original_response(self, **kwargs):
        """How a handler updates its message once it has deferred -- response.edit_message
        is no longer available to it at that point, so these land in their own list."""
        self.original_response_edits.append(kwargs)
        return FakeMessage()

    async def delete_original_response(self):
        self.original_response_deleted = True


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