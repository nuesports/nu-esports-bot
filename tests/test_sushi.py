import pytest

from cogs import sushi
from tests.conftest import FakeApplicationContext, FakeMessage


class FakeUser:
    def __init__(self, id):
        self.id = id


class FakeChannel:
    def __init__(self, id=1):
        self.id = id
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return FakeMessage()


@pytest.mark.asyncio
async def test_sushi_command_posts_a_fresh_leaderboard():
    cog = sushi.Sushi(bot=None)
    channel = FakeChannel()
    ctx = FakeApplicationContext(author=FakeUser(1), channel=channel)

    await sushi.Sushi.sushi.callback(cog, ctx)

    assert ctx.deferred
    assert len(channel.sent) == 1
    assert channel.sent[0]["embed"].description == "nobody's eaten any sushi yet"
    assert isinstance(channel.sent[0]["view"], sushi.SushiView)
    assert cog.board_messages[channel.id] is not None
    assert len(ctx.followup.send_calls) == 1


@pytest.mark.asyncio
async def test_sushi_command_reflects_existing_scores():
    cog = sushi.Sushi(bot=None)
    cog.boards[42] = {7: 3, 8: 1}
    channel = FakeChannel(id=42)
    ctx = FakeApplicationContext(author=FakeUser(1), channel=channel)

    await sushi.Sushi.sushi.callback(cog, ctx)

    description = channel.sent[0]["embed"].description
    assert "<@7> — 3" in description
    assert "<@8> — 1" in description


@pytest.mark.asyncio
async def test_sushi_command_deletes_previous_leaderboard_message():
    cog = sushi.Sushi(bot=None)
    channel = FakeChannel()
    old_message = FakeMessage()
    cog.board_messages[channel.id] = old_message

    ctx = FakeApplicationContext(author=FakeUser(1), channel=channel)
    await sushi.Sushi.sushi.callback(cog, ctx)

    assert old_message.deleted
