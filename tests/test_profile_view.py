import datetime

import pytest

from cogs import profile
from tests.conftest import FakeApplicationContext

TEST_DISCORDID = 900000000000000003  # obviously-fake id, isolated test DB anyway


class FakeAvatar:
    def __init__(self):
        self.url = "https://example.com/avatar.png"


class FakeMember:
    def __init__(self, id, display_name):
        self.id = id
        self.display_name = display_name
        self.joined_at = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
        self.display_avatar = FakeAvatar()


async def _clear_profile_rows(db):
    await db.perform_one("DELETE FROM profile_stats WHERE discordid = %s;", (TEST_DISCORDID,))
    await db.perform_one("DELETE FROM profiles WHERE discordid = %s;", (TEST_DISCORDID,))


@pytest.mark.asyncio
async def test_view_shows_home_page_when_no_game_requested(migrated_db):
    from utils import db
    await _clear_profile_rows(db)
    try:
        await db.perform_one(
            "INSERT INTO profile_stats (discordid, game, rank_label, wins, losses) VALUES (%s, %s, %s, %s, %s);",
            (TEST_DISCORDID, "league", "Gold 2", 5, 3),
        )
        member = FakeMember(TEST_DISCORDID, "caviar")
        ctx = FakeApplicationContext(author=member)

        await profile.Profile.view.callback(object(), ctx, None, None)

        assert ctx.deferred
        assert len(ctx.followup.send_calls) == 1
        call = ctx.followup.send_calls[0]
        embed = call["embed"]
        assert embed.title == "💬 caviar's Profile"
        assert embed.footer.text == "Page 1/2"  # home + league, no other game has data
        assert isinstance(call["view"], profile.ProfilePaginator)
    finally:
        await _clear_profile_rows(db)


@pytest.mark.asyncio
async def test_view_jumps_directly_to_requested_game(migrated_db):
    from utils import db
    await _clear_profile_rows(db)
    try:
        await db.perform_one(
            "INSERT INTO profile_stats (discordid, game, rank_label, wins, losses) VALUES (%s, %s, %s, %s, %s);",
            (TEST_DISCORDID, "league", "Gold 2", 5, 3),
        )
        member = FakeMember(TEST_DISCORDID, "caviar")
        ctx = FakeApplicationContext(author=member)

        await profile.Profile.view.callback(object(), ctx, None, "league")

        embed = ctx.followup.send_calls[0]["embed"]
        assert embed.title == "💬 caviar - League"
        assert embed.footer.text == "Page 2/2"
    finally:
        await _clear_profile_rows(db)


@pytest.mark.asyncio
async def test_view_targets_another_user_when_given(migrated_db):
    from utils import db
    await _clear_profile_rows(db)
    try:
        requester = FakeMember(1, "requester")
        target = FakeMember(TEST_DISCORDID, "benjamin")
        ctx = FakeApplicationContext(author=requester)

        await profile.Profile.view.callback(object(), ctx, target, None)

        embed = ctx.followup.send_calls[0]["embed"]
        assert "benjamin's Profile" in embed.title
    finally:
        await _clear_profile_rows(db)
