"""config_history against a real database -- needs the db host, like test_db.py.

Every row these tests write is by TEST_MEMBER, and only those rows get cleaned up,
so running them against a dev database keeps its real history intact."""

import pytest
import pytest_asyncio

from utils import config_history, db


class FakeMember:
    id = 900000000000000001
    name = "config-history-test"


TEST_MEMBER = FakeMember()
# a path no real setting has, so path filters only ever see these tests' rows
TEST_PATH = "tests.config_history"


async def delete_test_rows() -> None:
    await db.perform_one(
        "DELETE FROM config_revisions WHERE user_id = %s;", (TEST_MEMBER.id,)
    )


@pytest_asyncio.fixture
async def test_rows(migrated_db):
    await delete_test_rows()
    yield
    await delete_test_rows()


async def record(action, after_text, before_text=None, path=TEST_PATH):
    return await config_history.record(
        action, after_text, before_text, path, TEST_MEMBER
    )


@pytest.mark.asyncio
async def test_a_revision_round_trips(test_rows):
    revision_id = await record("edit", "after", "before")

    revision = await config_history.get(revision_id)

    assert (revision.action, revision.path, revision.user_name) == (
        "edit",
        TEST_PATH,
        TEST_MEMBER.name,
    )
    assert (revision.before_text, revision.after_text) == ("before", "after")
    assert revision.undoable


@pytest.mark.asyncio
async def test_latest_text_is_the_newest_file(test_rows):
    await record("startup", "first", path=None)
    await record("reload", "second", "first", path=None)

    assert await config_history.latest_text() == "second"
    assert config_history.latest_text_before_startup() == "second"


@pytest.mark.asyncio
async def test_recent_filters_by_path_newest_first(test_rows):
    await record("edit", "a", "")
    await record("edit", "b", "a", path="tests.somewhere_else")
    await record("edit", "c", "b")

    mine = await config_history.recent(10, TEST_PATH)

    assert [r.after_text for r in mine] == ["c", "a"]
    assert [r.after_text for r in await config_history.recent(2)] == ["c", "b"]


@pytest.mark.asyncio
async def test_an_undone_revision_stops_being_undoable(test_rows):
    first = await record("edit", "b", "a")
    undo = await record("undo", "a", "b")

    await config_history.mark_undone(first, undo)

    revision = await config_history.get(first)
    assert revision.undone_by == undo
    assert not revision.undoable
