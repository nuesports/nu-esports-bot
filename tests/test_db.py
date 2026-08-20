import psycopg
import pytest
import pytest_asyncio

from utils import db

TEST_DISCORDID = 900000000000000001  # obviously-fake id, isolated test DB anyway


def test_get_db_conninfo_reads_secrets_yaml(monkeypatch):
    monkeypatch.setattr(db.config, "secrets", {
        "database": {
            "host": "db",
            "port": 5432,
            "dbname": "nu-esports-bot",
            "user": "bot",
            "password": "hunter2",
        }
    })
    assert db.get_db_conninfo() == "host=db port=5432 dbname=nu-esports-bot user=bot password=hunter2"


@pytest_asyncio.fixture
async def clean_test_row(migrated_db):
    """Deletes the throwaway test row before and after each test that uses it, so
    tests don't see leftovers from a previous run or leave any behind."""
    await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
    yield
    await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))


@pytest.mark.asyncio
async def test_perform_one_and_fetch_one_round_trip(clean_test_row):
    await db.perform_one(
        "INSERT INTO users (discordid, points) VALUES (%s, %s);",
        (TEST_DISCORDID, 42),
    )
    row = await db.fetch_one("SELECT points FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
    assert row == (42,)


@pytest.mark.asyncio
async def test_fetch_one_returns_none_when_no_row(clean_test_row):
    row = await db.fetch_one("SELECT points FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
    assert row is None


@pytest.mark.asyncio
async def test_fetch_all_returns_every_matching_row(clean_test_row):
    await db.perform_one("INSERT INTO users (discordid, points) VALUES (%s, %s);", (TEST_DISCORDID, 1))
    rows = await db.fetch_all("SELECT discordid, points FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
    assert rows == [(TEST_DISCORDID, 1)]


@pytest.mark.asyncio
async def test_fetch_all_returns_empty_list_when_no_rows(clean_test_row):
    rows = await db.fetch_all("SELECT * FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
    assert rows == []


@pytest.mark.asyncio
async def test_perform_many_inserts_every_row(clean_test_row):
    second_id = TEST_DISCORDID + 1
    try:
        await db.perform_many(
            "INSERT INTO users (discordid, points) VALUES (%s, %s);",
            [(TEST_DISCORDID, 5), (second_id, 10)],
        )
        rows = await db.fetch_all(
            "SELECT discordid, points FROM users WHERE discordid IN (%s, %s) ORDER BY discordid;",
            (TEST_DISCORDID, second_id),
        )
        assert rows == [(TEST_DISCORDID, 5), (second_id, 10)]
    finally:
        await db.perform_one("DELETE FROM users WHERE discordid = %s;", (second_id,))


@pytest.mark.asyncio
async def test_perform_one_rolls_back_on_error(clean_test_row):
    """cursor()'s try/except rolls back on any exception -- a bad statement
    shouldn't leave a partial write behind."""
    with pytest.raises(psycopg.DataError):
        await db.perform_one(
            "INSERT INTO users (discordid, points) VALUES (%s, %s);",
            (TEST_DISCORDID, "not a number"),
        )
    row = await db.fetch_one("SELECT * FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
    assert row is None


@pytest.mark.asyncio
async def test_perform_one_reports_how_many_rows_it_touched(clean_test_row):
    """Both overdraft guards read this return value to tell a rejected conditional
    UPDATE from an applied one, so 0-vs-1 here is load-bearing, not incidental."""
    await db.perform_one(
        "INSERT INTO users (discordid, points) VALUES (%s, %s);",
        (TEST_DISCORDID, 100),
    )

    applied = await db.perform_one(
        "UPDATE users SET points = points - %s WHERE discordid = %s AND points >= %s;",
        (60, TEST_DISCORDID, 60),
    )
    assert applied == 1

    rejected = await db.perform_one(
        "UPDATE users SET points = points - %s WHERE discordid = %s AND points >= %s;",
        (60, TEST_DISCORDID, 60),
    )
    assert rejected == 0

    row = await db.fetch_one("SELECT points FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
    assert row == (40,)  # the rejected statement left the balance alone
