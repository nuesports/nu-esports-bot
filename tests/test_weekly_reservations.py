"""Reserving the same slot every week: the heads up, the skip, and the confirm."""

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from conftest import FakeApplicationContext, FakeInteraction, FakeMessage

from cogs import pcs
from utils import db

OWNER_ID = 900000000000000003
TEAM = "Valorant White"
# a wednesday, and far enough out that nothing here is short notice
RUN_DATE = date(2030, 1, 9)
START = datetime(2030, 1, 9, 19, tzinfo=pcs.CENTRAL_TZ)
HEADS_UP = START - timedelta(days=3)
CONFIRM = START - timedelta(days=2)


def slot(weekday=2, clock=time(19), first_date=date(2030, 1, 2)):
    return pcs.WeeklySlot(
        1,
        OWNER_ID,
        "lilac",
        TEAM,
        2,
        "Scrim",
        True,
        weekday,
        clock,
        time(21),
        first_date,
    )


# --- the dates ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2030, 1, 7, 12, tzinfo=pcs.CENTRAL_TZ), RUN_DATE),
        (datetime(2030, 1, 9, 18, 59, tzinfo=pcs.CENTRAL_TZ), RUN_DATE),
        (datetime(2030, 1, 9, 19, tzinfo=pcs.CENTRAL_TZ), date(2030, 1, 16)),
        (datetime(2030, 1, 10, 9, tzinfo=pcs.CENTRAL_TZ), date(2030, 1, 16)),
    ],
    ids=["earlier that week", "just before", "at the start", "the day after"],
)
def test_the_next_week_is_the_next_one_not_started(now, expected):
    assert pcs.next_weekly_date(slot(), now) == expected


def test_nothing_runs_before_the_first_week():
    now = datetime(2030, 1, 7, 12, tzinfo=pcs.CENTRAL_TZ)
    assert pcs.next_weekly_date(slot(first_date=date(2030, 1, 16)), now) == date(
        2030, 1, 16
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2030, 1, 6, 18, 59, tzinfo=pcs.CENTRAL_TZ), RUN_DATE),
        (datetime(2030, 1, 6, 19, tzinfo=pcs.CENTRAL_TZ), date(2030, 1, 16)),
        (datetime(2030, 1, 9, 12, tzinfo=pcs.CENTRAL_TZ), date(2030, 1, 16)),
    ],
    ids=["heads up still possible", "heads up just missed", "that same day"],
)
def test_the_first_week_always_gets_its_full_heads_up(now, expected):
    assert pcs.first_weekly_date(2, time(19), now) == expected


def test_a_slot_keeps_its_wall_clock_across_dst():
    # DST ends 2030-11-03
    before = pcs.weekly_datetime(date(2030, 10, 30), time(19))
    after = pcs.weekly_datetime(date(2030, 11, 6), time(19))
    assert (before.hour, after.hour) == (19, 19)
    assert before.utcoffset() != after.utcoffset()


@pytest.mark.asyncio
async def test_the_skip_button_carries_its_slot_and_week():
    [button] = pcs.weekly_skip_view(7, RUN_DATE).children
    assert button.label == "Skip this week"
    assert button.custom_id == f"{pcs.WEEKLY_SKIP_ID}:7:2030-01-09"


# --- /reserve-weekly refusals, nothing written ----------------------------------


@pytest.fixture
def cog():
    return pcs.PCs.__new__(pcs.PCs)


class RecordingContext(FakeApplicationContext):
    """Keeps the refusal text, which the commands pass positionally."""

    async def respond(self, content=None, **kwargs):
        self.respond_calls.append({"content": content, **kwargs})


class FakeAuthor:
    id = OWNER_ID
    name = "lilac"
    discriminator = "0"


async def command(cog, monkeypatch, **overrides):
    monkeypatch.setattr(pcs.config, "can_reserve", lambda member: True)
    ctx = RecordingContext(FakeAuthor())
    args = {
        "team": TEAM,
        "num_pcs": 2,
        "res_type": "Scrim",
        "day": "Wednesday",
        "start": "7",
        "end": "9",
        "skip_backroom": "no",
    }
    await pcs.PCs.reserve_weekly.callback(cog, ctx, **(args | overrides))
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [
        ({"start": "whenever"}, "Could not read the time"),
        ({"start": "9", "end": "7"}, "has to end after it starts"),
        ({"start": "7", "end": "7"}, "has to end after it starts"),
        ({"start": "3AM", "end": "5AM"}, "Norris is closed"),
        ({"num_pcs": 13, "skip_backroom": "yes"}, "without the back room"),
    ],
    ids=["bad time", "backwards", "zero length", "building closed", "too many pcs"],
)
async def test_a_bad_weekly_slot_is_refused(cog, monkeypatch, overrides, refusal):
    ctx = await command(cog, monkeypatch, **overrides)

    [reply] = ctx.respond_calls
    assert refusal in reply["content"]
    assert reply["ephemeral"] is True
    assert ctx.followup.send_calls == []


@pytest.mark.asyncio
async def test_only_people_who_can_reserve_get_a_weekly_slot(cog, monkeypatch):
    ctx = FakeApplicationContext(FakeAuthor())
    monkeypatch.setattr(pcs.config, "can_reserve", lambda member: False)

    await pcs.PCs.reserve_weekly.callback(
        cog, ctx, TEAM, 2, "Scrim", "Wednesday", "7", "9", "no"
    )

    assert ctx.respond_calls[0]["ephemeral"] is True
    assert ctx.followup.send_calls == []


@pytest.mark.asyncio
async def test_a_weekly_booking_says_so_on_the_staff_ping(cog, monkeypatch):
    class Sent(FakeMessage):
        id = 4321

    class Channel:
        id = 5
        guild = SimpleNamespace(get_role=lambda role_id: None)

        def __init__(self):
            self.sent = []

        async def send(self, content=None, **kwargs):
            self.sent.append({"content": content, **kwargs})
            return Sent()

    async def first_in_line():
        return 0

    async def reminder(*args):
        return None

    channel = Channel()
    cog.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
    cog.pending_acknowledgments = {}
    cog.next_staff_index = first_in_line
    cog.save_booking_reminder = reminder
    monkeypatch.setattr(pcs, "staff_list", lambda: [42])

    await cog.post_staff_ping(
        TEAM,
        "Scrim",
        [1, 2],
        START,
        START + timedelta(hours=2),
        "lilac",
        [],
        False,
        77,
        weekly=True,
    )

    fields = {field.name: field.value for field in channel.sent[0]["embed"].fields}
    assert channel.sent[0]["content"] == "<@42>"
    assert fields["Status"] == "🔁 Weekly auto reservation"


# --- the whole run, against the real tables --------------------------------------


class FakeUser:
    def __init__(self, shut=False):
        self.shut = shut
        self.dms = []

    async def send(self, content=None, **kwargs):
        if self.shut:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "shut")
        self.dms.append({"content": content, **kwargs})
        return FakeMessage()


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return FakeMessage()


@pytest_asyncio.fixture
async def weekly(migrated_db, monkeypatch):
    """A saved weekly slot and a cog whose booking steps all succeed."""

    async def wipe():
        await db.perform_one(
            "DELETE FROM weekly_reservations WHERE owner_id = %s", (OWNER_ID,)
        )
        await db.perform_one("DELETE FROM reservations WHERE manager = 'lilac-weekly'")

    await wipe()
    [weekly_id] = await db.fetch_one(
        """
        INSERT INTO weekly_reservations (
            owner_id, manager, team, num_pcs, res_type, include_back_room,
            weekday, start_time, end_time, first_date
        )
        VALUES (%s, 'lilac-weekly', %s, 2, 'Scrim', TRUE, 2, '19:00', '21:00', '2030-01-02')
        RETURNING id
        """,
        (OWNER_ID, TEAM),
    )
    [row] = await db.fetch_all(
        f"SELECT {pcs.WEEKLY_COLUMNS} FROM weekly_reservations WHERE id = %s",
        (weekly_id,),
    )

    cog = pcs.PCs.__new__(pcs.PCs)
    user = FakeUser()
    channel = FakeChannel()
    cog.bot = SimpleNamespace(
        get_user=lambda user_id: user, get_channel=lambda channel_id: channel
    )
    cog.pings = []

    async def no_conflict(*args):
        return (False, None, None)

    async def allocate(*args):
        return [1, 2]

    async def quota(*args):
        return (True, 0)

    async def post(*args, **kwargs):
        cog.pings.append((args, kwargs))

    cog.check_conflicts = no_conflict
    cog.allocate_pcs = allocate
    cog.check_prime_time_quota = quota
    cog.post_staff_ping = post
    cog.team_prime_time_quota = pcs.TEAM_PRIME_TIME_QUOTA
    monkeypatch.setattr(pcs.config, "is_bot_dev", lambda member: False)
    yield SimpleNamespace(
        cog=cog, slot=pcs.WeeklySlot(*row), user=user, channel=channel
    )
    await wipe()


async def run_status(env):
    row = await db.fetch_one(
        """
        SELECT status FROM weekly_reservation_runs
        WHERE weekly_id = %s AND run_date = %s
        """,
        (env.slot.id, RUN_DATE),
    )
    return row[0] if row else None


async def press_skip(env, user_id=OWNER_ID):
    interaction = FakeInteraction(SimpleNamespace(id=user_id))
    interaction.data = {"custom_id": f"{pcs.WEEKLY_SKIP_ID}:{env.slot.id}:2030-01-09"}
    await env.cog.on_interaction(interaction)
    return interaction


@pytest.mark.asyncio
async def test_nothing_happens_before_the_heads_up(weekly):
    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP - timedelta(minutes=1))

    assert weekly.user.dms == []
    assert await run_status(weekly) is None


@pytest.mark.asyncio
async def test_the_heads_up_goes_out_once_with_a_skip_button(weekly):
    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP)
    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP + timedelta(minutes=5))

    [dm] = weekly.user.dms
    assert dm["content"].startswith(f"Heads up, auto res for **{TEAM}, Wednesday")
    assert dm["content"].endswith("is being confirmed tomorrow.")
    assert dm["view"].children[0].label == "Skip this week"
    assert await run_status(weekly) == "notified"
    assert weekly.cog.pings == []


@pytest.mark.asyncio
async def test_a_day_later_it_books_and_pings_staff(weekly):
    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP)
    await weekly.cog.process_weekly_slot(weekly.slot, CONFIRM - timedelta(minutes=1))
    assert weekly.cog.pings == []

    await weekly.cog.process_weekly_slot(weekly.slot, CONFIRM)
    await weekly.cog.process_weekly_slot(weekly.slot, CONFIRM + timedelta(minutes=5))

    [(_, kwargs)] = weekly.cog.pings
    assert kwargs["weekly"] is True
    saved = await db.fetch_all(
        "SELECT team, pcs, start_time, manager FROM reservations WHERE manager = 'lilac-weekly'"
    )
    assert saved == [(TEAM, [1, 2], START, "lilac-weekly")]
    assert await run_status(weekly) == "booked"


@pytest.mark.asyncio
async def test_a_skipped_week_is_not_booked_and_next_week_still_runs(weekly):
    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP)

    interaction = await press_skip(weekly)

    edit = interaction.response.edits[0]
    assert edit["content"].startswith(f"Skipped **{TEAM}, Wednesday")
    assert edit["view"] is None
    assert await run_status(weekly) == "skipped"

    await weekly.cog.process_weekly_slot(weekly.slot, CONFIRM)
    assert weekly.cog.pings == []

    # the following wednesday's heads up
    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP + timedelta(days=7))
    assert len(weekly.user.dms) == 2
    assert "January 16" in weekly.user.dms[1]["content"]


@pytest.mark.asyncio
async def test_only_the_owner_can_skip(weekly):
    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP)

    interaction = await press_skip(weekly, user_id=1)

    assert "Only whoever set up" in interaction.response.messages[0]["content"]
    assert await run_status(weekly) == "notified"


@pytest.mark.asyncio
async def test_a_skip_after_the_confirm_is_too_late(weekly):
    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP)
    await weekly.cog.process_weekly_slot(weekly.slot, CONFIRM)

    interaction = await press_skip(weekly)

    assert "Too late" in interaction.response.messages[0]["content"]
    assert await run_status(weekly) == "booked"


@pytest.mark.asyncio
async def test_a_conflict_tells_the_owner_instead_of_booking(weekly):
    async def clash(*args):
        return (True, "Apex White", "someone")

    weekly.cog.check_conflicts = clash
    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP)
    await weekly.cog.process_weekly_slot(weekly.slot, CONFIRM)

    assert weekly.cog.pings == []
    assert "**Apex White** has that slot" in weekly.user.dms[1]["content"]
    assert await run_status(weekly) == "failed"


@pytest.mark.asyncio
async def test_no_free_pcs_tells_the_owner_instead_of_booking(weekly):
    async def nothing_free(*args):
        return []

    weekly.cog.allocate_pcs = nothing_free
    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP)
    await weekly.cog.process_weekly_slot(weekly.slot, CONFIRM)

    assert "there aren't 2 PCs free" in weekly.user.dms[1]["content"]
    assert await run_status(weekly) == "failed"


@pytest.mark.asyncio
async def test_shut_dms_fall_back_to_the_staff_channel(weekly):
    weekly.user.shut = True

    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP)

    [post] = weekly.channel.sent
    assert post["content"].startswith(f"<@{OWNER_ID}> Heads up")
    assert post["view"].children[0].label == "Skip this week"


@pytest.mark.asyncio
async def test_coming_back_late_still_gives_a_full_day(weekly):
    late = CONFIRM + timedelta(hours=6)

    await weekly.cog.process_weekly_slot(weekly.slot, late)
    await weekly.cog.process_weekly_slot(weekly.slot, late + timedelta(hours=23))
    assert weekly.cog.pings == []

    await weekly.cog.process_weekly_slot(weekly.slot, late + timedelta(days=1))
    assert len(weekly.cog.pings) == 1


@pytest.mark.asyncio
async def test_coming_back_too_late_says_it_was_missed(weekly):
    await weekly.cog.process_weekly_slot(weekly.slot, START - timedelta(hours=3))

    assert "won't be booked" in weekly.user.dms[0]["content"]
    assert await run_status(weekly) == "missed"
    assert weekly.cog.pings == []


@pytest.mark.asyncio
async def test_a_cancelled_slot_answers_its_old_button(weekly):
    await weekly.cog.process_weekly_slot(weekly.slot, HEADS_UP)
    await db.perform_one(
        "DELETE FROM weekly_reservations WHERE id = %s", (weekly.slot.id,)
    )

    interaction = await press_skip(weekly)

    assert "has been cancelled" in interaction.response.messages[0]["content"]
    assert await run_status(weekly) is None


@pytest.mark.asyncio
async def test_reserve_weekly_saves_the_slot_end_to_end(migrated_db, cog, monkeypatch):
    await db.perform_one(
        "DELETE FROM weekly_reservations WHERE owner_id = %s", (OWNER_ID,)
    )
    try:
        ctx = await command(cog, monkeypatch)
        again = await command(cog, monkeypatch)

        reply = ctx.followup.send_calls[0]["content"]
        assert f"**{TEAM}** is on every Wednesday" in reply
        assert (
            "already has a weekly reservation"
            in again.followup.send_calls[0]["content"]
        )
        rows = await db.fetch_all(
            "SELECT team, num_pcs, weekday, start_time, end_time FROM weekly_reservations WHERE owner_id = %s",
            (OWNER_ID,),
        )
        assert rows == [(TEAM, 2, 2, time(19), time(21))]
    finally:
        await db.perform_one(
            "DELETE FROM weekly_reservations WHERE owner_id = %s", (OWNER_ID,)
        )


@pytest.mark.asyncio
async def test_cancel_weekly_stops_it_for_the_owner_only(weekly, monkeypatch):
    other = FakeApplicationContext(SimpleNamespace(id=1))
    await pcs.PCs.cancel_weekly_reservation.callback(
        weekly.cog, other, str(weekly.slot.id)
    )
    assert "only cancel your own" in other.followup.send_calls[0]["content"]
    assert await weekly.cog.weekly_slots() != []

    ctx = FakeApplicationContext(FakeAuthor())
    await pcs.PCs.cancel_weekly_reservation.callback(
        weekly.cog, ctx, str(weekly.slot.id)
    )
    assert "Stopped" in ctx.followup.send_calls[0]["content"]
    assert [s for s in await weekly.cog.weekly_slots() if s.id == weekly.slot.id] == []
