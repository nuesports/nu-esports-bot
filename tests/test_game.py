import asyncio

import discord
import pytest

from cogs import game
from tests.conftest import (
    FakeApplicationContext,
    FakeInteraction,
    FakeInteractionResponse,
)


class FakeUser:
    def __init__(self, id):
        self.id = id
        self.display_name = f"player{id}"


class FakeHTTPResponse:
    """The two attributes discord.HTTPException reads off a response to build its message."""
    def __init__(self, status, reason):
        self.status = status
        self.reason = reason


def not_found():
    return discord.NotFound(FakeHTTPResponse(404, "Not Found"), "Unknown Message")


def server_error():
    return discord.HTTPException(FakeHTTPResponse(500, "Internal Server Error"), "oops")


class FakeStackMessage:
    """One posted copy of the stack. The error hooks let a test make a delete or an edit
    fail the way an already-deleted or ratelimited message would."""
    def __init__(self, id, channel):
        self.id = id
        self.channel = channel
        self.delete_error = None
        self.edit_error = None
        self.deleted = False
        self.edit_calls = []

    async def delete(self):
        self.channel.events.append(("delete", self.id))
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True

    async def edit(self, **kwargs):
        if self.edit_error is not None:
            raise self.edit_error
        self.edit_calls.append(kwargs)


class FakeChannel:
    """Stands in for the text channel, and doubles as the message store the view re-fetches
    through -- plus a shared event log, since the order of the send and the delete is the
    whole point of the bump fix."""
    def __init__(self, id=1):
        self.id = id
        self.messages = {}
        self.events = []
        self._next_id = 100

    def post(self):
        message = FakeStackMessage(self._next_id, self)
        self.messages[message.id] = message
        self._next_id += 1
        return message

    async def fetch_message(self, id):
        return self.messages[id]


class RecordingResponse(FakeInteractionResponse):
    """Logs its sends into the channel's event list so a test can pin down that the bump
    answered Discord before it went off to delete anything."""
    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def send_message(self, content=None, **kwargs):
        self.channel.events.append(("send", kwargs.get("ephemeral", False)))
        await super().send_message(content, **kwargs)


class GatedResponse(RecordingResponse):
    """Parks the send until the test opens the gate, so a second click really does land
    while the first bump is still in flight -- pycord runs every click as its own task."""
    def __init__(self, channel, gate):
        super().__init__(channel)
        self.gate = gate

    async def send_message(self, content=None, **kwargs):
        await self.gate.wait()
        await super().send_message(content, **kwargs)


class FakeStackInteraction(FakeInteraction):
    """FakeInteraction plus what the bump path reaches for: the copy the button was clicked
    on, the channel to re-fetch through, and the message the response just posted."""
    def __init__(self, user, message, channel, response=None):
        super().__init__(user)
        self.message = message
        self.channel = channel
        self.response = response or RecordingResponse(channel)
        self._original = None

    async def original_response(self):
        if self._original is None:
            self._original = self.channel.post()
        return self._original


class FakeStackContext(FakeApplicationContext):
    """FakeApplicationContext plus the interaction handle the stack command needs to find
    the message it just posted."""
    def __init__(self, author, channel):
        super().__init__(author, channel)
        self.interaction = FakeStackInteraction(author, None, channel)


def stack_view(channel=None, size=5):
    """A view wired to one live copy, as the stack command would leave it."""
    embed = discord.Embed(title="test stack [5]")
    embed.add_field(name="squares", value="empty :/")
    view = game.GameStackView(embed, size)
    if channel is not None:
        view.current_message = channel.post()
    return view


# --- GameStackView.refresh_callback ---

@pytest.mark.asyncio
async def test_bump_answers_the_interaction_before_it_deletes_the_old_copy():
    """The ordering is the fix: a delete that runs first can sleep on Discord's delete
    bucket past the 3 second response deadline, leaving nothing to replace it."""
    channel = FakeChannel()
    view = stack_view(channel)
    live = view.current_message
    interaction = FakeStackInteraction(FakeUser(1), live, channel)

    await view.refresh_callback.callback(interaction)

    assert channel.events == [("send", False), ("delete", live.id)]
    assert live.deleted is True
    assert view.current_message.id != live.id
    assert view.current_message is channel.messages[view.current_message.id]


@pytest.mark.asyncio
async def test_second_bump_is_turned_away_while_the_first_is_in_flight():
    channel = FakeChannel()
    view = stack_view(channel)
    live = view.current_message

    gate = asyncio.Event()
    first = FakeStackInteraction(FakeUser(1), live, channel, GatedResponse(channel, gate))
    second = FakeStackInteraction(FakeUser(2), live, channel)

    in_flight = asyncio.create_task(view.refresh_callback.callback(first))
    await asyncio.sleep(0)  # let it take the lock and park on the gate
    # Bounded: the second click has to be turned away, not queued behind the lock. If it
    # ever goes back to waiting its turn it deadlocks here, and a hang reads as a pass
    # right up until CI times out.
    await asyncio.wait_for(view.refresh_callback.callback(second), timeout=5)
    gate.set()
    await in_flight

    assert second.response.messages[0]["ephemeral"] is True
    assert live.deleted is True
    # one live copy plus the one bump that got through, and nothing posted for the reject
    assert len(channel.messages) == 2


@pytest.mark.asyncio
async def test_bump_from_a_superseded_copy_leaves_the_live_one_alone():
    """A copy the view has already moved past still has working buttons until Discord
    finishes deleting it, and that click used to delete whatever was current instead."""
    channel = FakeChannel()
    view = stack_view(channel)
    stale = channel.post()
    live = view.current_message
    interaction = FakeStackInteraction(FakeUser(1), stale, channel)

    await view.refresh_callback.callback(interaction)

    assert interaction.response.messages[0]["ephemeral"] is True
    assert live.deleted is False
    assert stale.deleted is False
    assert view.current_message is live
    assert channel.events == [("send", True)]


@pytest.mark.asyncio
async def test_bump_still_lands_when_the_old_copy_is_already_gone():
    channel = FakeChannel()
    view = stack_view(channel)
    live = view.current_message
    live.delete_error = not_found()
    interaction = FakeStackInteraction(FakeUser(1), live, channel)

    await view.refresh_callback.callback(interaction)

    assert interaction.response.messages[0]["embed"] is view.embed
    assert view.current_message.id != live.id


@pytest.mark.asyncio
async def test_bump_strips_the_buttons_off_a_copy_it_couldnt_delete():
    channel = FakeChannel()
    view = stack_view(channel)
    live = view.current_message
    live.delete_error = server_error()
    interaction = FakeStackInteraction(FakeUser(1), live, channel)

    await view.refresh_callback.callback(interaction)

    assert live.edit_calls == [{"view": None}]
    assert view.current_message.id != live.id


# --- GameStackView.on_timeout ---

@pytest.mark.asyncio
async def test_timeout_on_an_untouched_stack_doesnt_raise():
    view = stack_view()

    await view.on_timeout()

    assert all(child.disabled for child in view.children)


@pytest.mark.asyncio
async def test_timeout_ignores_a_stack_that_was_already_deleted():
    channel = FakeChannel()
    view = stack_view(channel)
    view.current_message.edit_error = not_found()

    await view.on_timeout()

    assert all(child.disabled for child in view.children)


# --- /game stack ---

@pytest.mark.asyncio
async def test_stack_command_points_the_view_at_a_channel_fetched_message():
    """Through the channel, not the interaction: the webhook handle stops working at 15
    minutes and the view sticks around for 20."""
    cog = game.Game(bot=None)
    channel = FakeChannel()
    ctx = FakeStackContext(FakeUser(1), channel)

    await game.Game.stack.callback(cog, ctx, name="", size=5)

    view = ctx.respond_calls[0]["view"]
    assert view.current_message is channel.messages[view.current_message.id]
    assert ctx.respond_calls[0]["embed"].title == "player1's stack [5]"
