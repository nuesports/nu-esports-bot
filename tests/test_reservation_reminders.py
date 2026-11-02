"""The re-ping staff get before a reservation starts if they still haven't made it."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import psycopg
import pytest
import pytest_asyncio
from conftest import FakeMessage

from cogs import pcs
from utils import db

MESSAGE_ID = 900000000000000001
CHANNEL_ID = 5
STAFF_ID = 42
TEAM = "Valorant White"


class FakePing(FakeMessage):
    def __init__(self, reactions=()):
        super().__init__()
        self.reactions = [
            SimpleNamespace(count=count, me=me) for count, me in reactions
        ]
        self.reply_calls = []

    async def reply(self, content=None, **kwargs):
        self.reply_calls.append({"content": content, **kwargs})
        return FakeMessage()


class FakeChannel:
    def __init__(self, ping):
        self.ping = ping
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        return FakeMessage()

    async def fetch_message(self, message_id):
        if self.ping is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
        return self.ping


def cog_with(ping):
    cog = pcs.PCs.__new__(pcs.PCs)
    cog.pending_acknowledgments = {MESSAGE_ID: {"team": TEAM}}
    channel = FakeChannel(ping)
    cog.bot = SimpleNamespace(get_channel=lambda channel_id: channel, channel=channel)
    return cog


@pytest_asyncio.fixture
async def clean_pings(migrated_db):
    await db.perform_one("DELETE FROM reservation_pings")
    await db.perform_one("DELETE FROM reservations WHERE team = %s", (TEAM,))
    yield
    await db.perform_one("DELETE FROM reservation_pings")
    await db.perform_one("DELETE FROM reservations WHERE team = %s", (TEAM,))


async def add_ping(start_in, posted_ago=timedelta(days=2), reminded=False):
    """A reservation and its staff ping, `start_in` from now, posted `posted_ago`."""
    now = datetime.now(UTC)
    start = now + start_in
    [reservation_id] = await db.fetch_one(
        """
        INSERT INTO reservations (team, pcs, start_time, end_time, manager)
        VALUES (%s, %s, %s, %s, 'lilac')
        RETURNING id
        """,
        (TEAM, [1, 2], start, start + timedelta(hours=2)),
    )
    await db.perform_one(
        """
        INSERT INTO reservation_pings (
            message_id, channel_id, staff_id, reservation_id, team, start_time,
            posted_at, reminded
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            MESSAGE_ID,
            CHANNEL_ID,
            STAFF_ID,
            reservation_id,
            TEAM,
            start,
            now - posted_ago,
            reminded,
        ),
    )
    return reservation_id, start


async def ping_row():
    return await db.fetch_one(
        "SELECT team, start_time, reminded FROM reservation_pings "
        "WHERE message_id = %s",
        (MESSAGE_ID,),
    )


@pytest.mark.parametrize(
    ("reactions", "acknowledged"),
    [
        ([], False),
        ([(1, True)], False),
        ([(1, False)], True),
        ([(2, True)], True),
        ([(1, True), (1, False)], True),
    ],
    ids=["nothing", "only the bot", "staff", "staff and bot", "two emoji"],
)
def test_only_someone_other_than_the_bot_acknowledges(reactions, acknowledged):
    assert pcs.is_acknowledged(FakePing(reactions)) is acknowledged


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_in", "posted_ago", "reminded", "due"),
    [
        (timedelta(hours=3, minutes=59), timedelta(days=2), False, True),
        (timedelta(hours=1), timedelta(days=2), False, True),
        (timedelta(hours=4, minutes=1), timedelta(days=2), False, False),
        (timedelta(hours=3), timedelta(days=2), True, False),
        (timedelta(minutes=-1), timedelta(days=2), False, False),
        (timedelta(hours=3), timedelta(minutes=5), False, False),
    ],
    ids=[
        "just inside",
        "late after downtime",
        "too early",
        "already reminded",
        "already started",
        "booked inside the window",
    ],
)
async def test_which_pings_are_due(clean_pings, start_in, posted_ago, reminded, due):
    await add_ping(start_in, posted_ago, reminded)

    rows = await cog_with(FakePing()).due_booking_reminders()

    assert [row[0] for row in rows] == ([MESSAGE_ID] if due else [])


@pytest.mark.asyncio
async def test_the_loop_reminds_once_and_names_who_still_has_to_book(clean_pings):
    ping = FakePing()
    cog = cog_with(ping)
    await add_ping(timedelta(hours=3))

    await cog.remind_unbooked_reservations()
    await cog.remind_unbooked_reservations()

    [reply] = ping.reply_calls
    assert reply["content"].startswith(f"<@{STAFF_ID}> **{TEAM}** starts <t:")
    assert reply["content"].endswith(":R> but hasn't been reserved yet.")
    # alone in its message, or discord shows the link above the gif
    assert cog.bot.channel.sent == [pcs.BOOKING_REMINDER_GIF]
    assert reply["mention_author"] is False
    assert (await ping_row())[2] is True
    # the 4 hour reminder replaces the 24 hour one, rather than stacking on it
    assert cog.pending_acknowledgments == {}


@pytest.mark.asyncio
async def test_a_reacted_ping_is_left_alone(clean_pings):
    ping = FakePing([(1, False)])
    cog = cog_with(ping)
    await add_ping(timedelta(hours=3))

    await cog.remind_unbooked_reservations()

    assert ping.reply_calls == []
    assert cog.bot.channel.sent == []
    assert (await ping_row())[2] is True


@pytest.mark.asyncio
async def test_a_failed_gif_does_not_re_ping(clean_pings):
    ping = FakePing()
    cog = cog_with(ping)

    async def refuse(content=None, **kwargs):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="no"), "no")

    cog.bot.channel.send = refuse
    await add_ping(timedelta(hours=3))

    await cog.remind_unbooked_reservations()
    await cog.remind_unbooked_reservations()

    assert len(ping.reply_calls) == 1


@pytest.mark.asyncio
async def test_a_deleted_ping_is_forgotten(clean_pings):
    await add_ping(timedelta(hours=3))

    await cog_with(None).remind_unbooked_reservations()

    assert await ping_row() is None


@pytest.mark.asyncio
async def test_a_failed_reply_is_tried_again_and_the_loop_lives(clean_pings):
    ping = FakePing()

    async def refuse(content=None, **kwargs):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="no"), "no")

    ping.reply = refuse
    await add_ping(timedelta(hours=3))

    await cog_with(ping).remind_unbooked_reservations()

    assert (await ping_row())[2] is False


@pytest.mark.asyncio
async def test_a_missing_channel_is_tried_again(clean_pings):
    cog = cog_with(FakePing())
    cog.bot = SimpleNamespace(get_channel=lambda channel_id: None)
    await add_ping(timedelta(hours=3))

    await cog.remind_unbooked_reservations()

    assert (await ping_row())[2] is False


@pytest.mark.asyncio
async def test_cancelling_a_reservation_stops_its_reminder(clean_pings):
    reservation_id, _ = await add_ping(timedelta(hours=3))

    await db.perform_one("DELETE FROM reservations WHERE id = %s", (reservation_id,))

    assert await ping_row() is None


@pytest.mark.asyncio
async def test_a_started_reservation_is_cleared_out(clean_pings):
    await add_ping(timedelta(minutes=-1))

    await cog_with(FakePing()).remind_unbooked_reservations()

    assert await ping_row() is None


@pytest.mark.asyncio
async def test_saving_a_reminder_arms_it(clean_pings):
    reservation_id, start = await add_ping(timedelta(hours=3))
    await db.perform_one("DELETE FROM reservation_pings")
    cog = cog_with(FakePing())

    await cog.save_booking_reminder(
        MESSAGE_ID, CHANNEL_ID, STAFF_ID, reservation_id, TEAM, start
    )

    assert await ping_row() == (TEAM, start, False)


@pytest.mark.asyncio
async def test_moving_the_start_re_arms_a_sent_reminder(clean_pings):
    _, start = await add_ping(timedelta(hours=3), reminded=True)
    later = start + timedelta(days=1)

    await cog_with(FakePing()).move_booking_reminder(MESSAGE_ID, "Apex White", later)

    assert await ping_row() == ("Apex White", later, False)


@pytest.mark.asyncio
async def test_editing_anything_else_does_not_re_arm(clean_pings):
    _, start = await add_ping(timedelta(hours=3), reminded=True)

    await cog_with(FakePing()).move_booking_reminder(MESSAGE_ID, "Apex White", start)

    assert await ping_row() == ("Apex White", start, True)


@pytest.mark.asyncio
async def test_a_failed_check_does_not_end_the_loop(monkeypatch):
    async def broken(*args):
        raise psycopg.OperationalError("database went away")

    monkeypatch.setattr(pcs.db, "pool", SimpleNamespace(closed=False))
    monkeypatch.setattr(pcs.db, "perform_one", broken)

    await cog_with(FakePing()).remind_unbooked_reservations()


@pytest.mark.asyncio
async def test_the_check_waits_for_the_pool(monkeypatch):
    async def forbidden(*args):
        raise AssertionError("the pool isn't open yet")

    monkeypatch.setattr(pcs.db, "pool", SimpleNamespace(closed=True))
    monkeypatch.setattr(pcs.db, "perform_one", forbidden)

    await cog_with(FakePing()).remind_unbooked_reservations()
