import pytest

from cogs import points
from tests.conftest import FakeApplicationContext

TEST_DISCORDID = 900000000000000002  # obviously-fake id, isolated test DB anyway


class FakeUser:
    def __init__(self, id, display_name):
        self.id = id
        self.display_name = display_name


@pytest.mark.asyncio
async def test_balance_reads_own_points(migrated_db):
    from utils import db
    await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
    try:
        await db.perform_one(
            "INSERT INTO users (discordid, points) VALUES (%s, %s);",
            (TEST_DISCORDID, 42),
        )
        user = FakeUser(TEST_DISCORDID, "caviar")
        ctx = FakeApplicationContext(author=user)

        await points.Points.balance.callback(object(), ctx, None)

        assert ctx.deferred
        assert len(ctx.followup.send_calls) == 1
        embed = ctx.followup.send_calls[0]["embed"]
        assert embed.title == "caviar's points"
        assert embed.description == "42 points"
    finally:
        await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))


@pytest.mark.asyncio
async def test_balance_defaults_to_zero_when_no_row(migrated_db):
    from utils import db
    await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))

    user = FakeUser(TEST_DISCORDID, "caviar")
    ctx = FakeApplicationContext(author=user)

    await points.Points.balance.callback(object(), ctx, None)

    embed = ctx.followup.send_calls[0]["embed"]
    assert embed.description == "0 points"


@pytest.mark.asyncio
async def test_balance_targets_other_user_when_given(migrated_db):
    from utils import db
    other_id = TEST_DISCORDID + 1
    await db.perform_one("DELETE FROM users WHERE discordid = %s;", (other_id,))
    try:
        await db.perform_one(
            "INSERT INTO users (discordid, points) VALUES (%s, %s);",
            (other_id, 7),
        )
        requester = FakeUser(TEST_DISCORDID, "caviar")
        target = FakeUser(other_id, "benjamin")
        ctx = FakeApplicationContext(author=requester)

        await points.Points.balance.callback(object(), ctx, target)

        embed = ctx.followup.send_calls[0]["embed"]
        assert embed.title == "benjamin's points"
        assert embed.description == "7 points"
    finally:
        await db.perform_one("DELETE FROM users WHERE discordid = %s;", (other_id,))
