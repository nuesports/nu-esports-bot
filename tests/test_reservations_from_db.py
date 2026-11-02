"""/reservations and the status checks read the bot's own database, not ggLeap."""

import io
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import psycopg
import pytest
import pytest_asyncio
from conftest import FakeApplicationContext, FakeInteraction, FakeMessage
from PIL import Image

from cogs import pcs
from utils import db

TEAM = "Reservations Test Team"
MESSAGE_ID = 900000000000000002
# the grid's cell colors
PURPLE = (155, 89, 182)
ORANGE = (255, 165, 0)
GREEN = (87, 242, 135)


def central(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=pcs.CENTRAL_TZ)


def saved(team=TEAM, reserved_pcs=(1, 2), start=None, end=None):
    """A reservation as get_reservations_in_range hands it back."""
    return {
        "id": 1,
        "team": team,
        "pcs": list(reserved_pcs),
        "start_time": start or central(2026, 11, 4, 19),
        "end_time": end or central(2026, 11, 4, 21),
        "manager": "lilac",
        "is_prime_time": True,
    }


@pytest.fixture
def cog():
    """No ggLeap anywhere: reaching for it fails the test."""
    cog = pcs.PCs.__new__(pcs.PCs)

    async def forbidden(url):
        raise AssertionError(f"reached out to ggLeap: {url}")

    cog.fetch_json_with_retries = forbidden
    return cog


def with_reservations(cog, *rows, confirmed=True):
    """Saved rows, all reacted to unless told otherwise."""

    async def in_range(start, end):
        return list(rows)

    async def reacted(reservations):
        return {res["id"] for res in reservations} if confirmed else set()

    cog.get_reservations_in_range = in_range
    cog.confirmed_reservation_ids = reacted
    return cog


async def show(cog, date="2026-11-04"):
    ctx = FakeApplicationContext(SimpleNamespace())
    await pcs.PCs.reservations.callback(cog, ctx, date)
    return ctx.followup.send_calls[0]


def grid_color(file, desk_row, slot):
    """The color of one cell: desk rows from the top, half hour slots from the left."""
    file.fp.seek(0)
    image = Image.open(io.BytesIO(file.fp.read())).convert("RGB")
    return image.getpixel((80 + slot * 30 + 15, 40 + desk_row * 30 + 15))


@pytest.mark.asyncio
async def test_reservations_shows_what_is_saved_without_ggleap(cog):
    with_reservations(cog, saved())

    sent = await show(cog)

    [grid, details] = sent["embeds"]
    assert "November 04, 2026" in grid.title
    assert "Orange = Unconfirmed" in grid.footer.text
    assert "Unconfirmed" not in details.fields[0].value
    assert details.fields[0].value.startswith(f"**Team:** {TEAM}\n")
    assert "PC 1, PC 2" in details.fields[0].value
    assert "07:00 PM - 09:00 PM" in details.fields[0].value
    assert "**Manager:** lilac" in details.fields[0].value


@pytest.mark.asyncio
async def test_the_grid_fills_only_the_reserved_desks_and_slots(cog):
    with_reservations(cog, saved())

    sent = await show(cog)

    # wednesdays open at 2pm, so 7pm is 10 slots in, and desk 1 is the top row
    assert grid_color(sent["file"], desk_row=0, slot=10) == PURPLE
    assert grid_color(sent["file"], desk_row=1, slot=13) == PURPLE
    assert grid_color(sent["file"], desk_row=0, slot=9) == GREEN
    assert grid_color(sent["file"], desk_row=0, slot=14) == GREEN
    assert grid_color(sent["file"], desk_row=2, slot=10) == GREEN


@pytest.mark.asyncio
async def test_an_unconfirmed_reservation_is_orange_and_says_so(cog):
    with_reservations(cog, saved(), confirmed=False)

    sent = await show(cog)

    assert grid_color(sent["file"], desk_row=0, slot=10) == ORANGE
    assert grid_color(sent["file"], desk_row=0, slot=9) == GREEN
    assert "**Status:** 🟧 Unconfirmed" in sent["embeds"][1].fields[0].value


@pytest.mark.asyncio
async def test_a_confirmed_booking_wins_a_shared_slot(cog):
    other = {**saved(), "id": 2}

    async def only_the_first(reservations):
        return {1}

    with_reservations(cog, saved(), other)
    cog.confirmed_reservation_ids = only_the_first

    sent = await show(cog)

    assert grid_color(sent["file"], desk_row=0, slot=10) == PURPLE


@pytest.mark.asyncio
async def test_an_empty_day_says_so(cog):
    with_reservations(cog)

    sent = await show(cog)

    assert sent["embed"].description == "No reservations found for this date."


@pytest.mark.asyncio
async def test_a_database_failure_is_reported(cog):
    async def broken(start, end):
        raise psycopg.OperationalError("database went away")

    cog.get_reservations_in_range = broken

    sent = await show(cog)

    assert "Failed to fetch reservations" in sent["content"]
    assert sent["ephemeral"] is True


@pytest.mark.asyncio
async def test_the_day_buttons_read_the_new_day(cog):
    asked = []

    async def in_range(start, end):
        asked.append((start, end))
        return [saved(team="Next Day Team")]

    async def reacted(reservations):
        return {1}

    cog.get_reservations_in_range = in_range
    cog.confirmed_reservation_ids = reacted
    view = pcs.ReservationView([], central(2026, 11, 4, 12), cog)
    interaction = FakeInteraction(SimpleNamespace())
    interaction.message = FakeMessage()

    await view.next_day_button.callback(interaction)

    assert asked == [(central(2026, 11, 5, 0), central(2026, 11, 6, 0))]
    edit = interaction.message.edit_calls[0]
    assert edit["embeds"][1].fields[0].value.startswith("**Team:** Next Day Team")


@pytest.mark.asyncio
async def test_the_status_checks_get_saved_reservations_by_desk_name(cog):
    with_reservations(cog, saved(reserved_pcs=(0, 3)))

    data = await cog.fetch_reservations("2026-11-04")

    assert data == {
        "reservations": [
            {
                "machines": ["Desk 000 - Streaming", "Desk 003"],
                "start_time": central(2026, 11, 4, 19).isoformat(),
                "end_time": central(2026, 11, 4, 21).isoformat(),
            }
        ]
    }


# --- against the real table ----------------------------------------------------


@pytest_asyncio.fixture
async def clean_reservations(migrated_db):
    await db.perform_one("DELETE FROM reservations WHERE team LIKE 'Reservations %'")
    yield
    await db.perform_one("DELETE FROM reservations WHERE team LIKE 'Reservations %'")


async def insert(start, end, reserved_pcs=(1, 2), team=TEAM):
    [reservation_id] = await db.fetch_one(
        """
        INSERT INTO reservations (team, pcs, start_time, end_time, manager)
        VALUES (%s, %s, %s, %s, 'lilac')
        RETURNING id
        """,
        (team, list(reserved_pcs), start, end),
    )
    return reservation_id


async def mine(rows):
    return [row for row in rows if row["team"].startswith("Reservations ")]


@pytest.mark.asyncio
async def test_a_day_is_a_central_day_not_a_utc_one(cog, clean_reservations):
    # 11pm Central on the 4th is already the 5th in UTC
    start = central(2026, 11, 4, 23)
    await insert(start, central(2026, 11, 4, 23, 30))

    on_the_4th = await mine(await cog.get_day_reservations(central(2026, 11, 4, 12)))
    on_the_5th = await mine(await cog.get_day_reservations(central(2026, 11, 5, 12)))

    assert [row["start_time"] for row in on_the_4th] == [start]
    assert on_the_5th == []


@pytest.mark.asyncio
async def test_a_day_leaves_out_its_neighbours(cog, clean_reservations):
    await insert(central(2026, 11, 3, 19), central(2026, 11, 3, 21))
    await insert(central(2026, 11, 4, 19), central(2026, 11, 4, 21))
    await insert(central(2026, 11, 5, 19), central(2026, 11, 5, 21))

    rows = await mine(await cog.get_day_reservations(central(2026, 11, 4, 8)))

    assert [row["start_time"] for row in rows] == [central(2026, 11, 4, 19)]


@pytest.mark.asyncio
async def test_a_saved_reservation_shows_up_end_to_end(cog, clean_reservations):
    await cog.save_reservation(
        TEAM, [1, 2], central(2026, 11, 4, 19), central(2026, 11, 4, 21), "lilac", True
    )

    sent = await show(cog)

    values = [field.value for field in sent["embeds"][1].fields]
    # with no ping behind it, nothing can have confirmed it
    assert any(
        value.startswith(f"**Team:** {TEAM}\n") and "Unconfirmed" in value
        for value in values
    )


class FakeChannel:
    def __init__(self, reactions=None):
        self.reactions = reactions

    async def fetch_message(self, message_id):
        if self.reactions is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
        return SimpleNamespace(reactions=self.reactions)


async def link_ping(reservation_id, message_id=MESSAGE_ID):
    await db.perform_one(
        """
        INSERT INTO reservation_pings
            (message_id, channel_id, staff_id, reservation_id, team, start_time)
        VALUES (%s, 5, 42, %s, %s, %s)
        """,
        (message_id, reservation_id, TEAM, datetime.now(UTC)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "confirmed"),
    [
        (FakeChannel([SimpleNamespace(count=1, me=False)]), True),
        (FakeChannel([SimpleNamespace(count=1, me=True)]), False),
        (FakeChannel([]), False),
        (FakeChannel(None), False),
        (None, False),
    ],
    ids=["staff reacted", "only the bot", "no reaction", "ping deleted", "no channel"],
)
async def test_confirmed_means_the_ping_has_a_reaction(
    cog, clean_reservations, channel, confirmed
):
    day = central(2026, 11, 4, 12)
    reservation_id = await insert(central(2026, 11, 4, 19), central(2026, 11, 4, 21))
    await link_ping(reservation_id)
    cog.bot = SimpleNamespace(get_channel=lambda channel_id: channel)

    rows = await mine(await cog.get_day_reservations_with_status(day))

    assert [row["confirmed"] for row in rows] == [confirmed]


@pytest.mark.asyncio
async def test_an_external_reservation_needs_no_ping(cog, clean_reservations):
    start, end = central(2026, 11, 4, 19), central(2026, 11, 4, 21)
    external = await insert(start, end, team="External")
    try:
        rows = await cog.get_day_reservations_with_status(central(2026, 11, 4, 12))
    finally:
        await db.perform_one("DELETE FROM reservations WHERE id = %s", (external,))

    assert [row["confirmed"] for row in rows if row["id"] == external] == [True]


@pytest.mark.asyncio
async def test_an_edited_ping_updates_the_saved_reservation(cog, clean_reservations):
    reservation_id = await insert(central(2026, 11, 4, 12), central(2026, 11, 4, 14))
    other_id = await insert(central(2026, 11, 4, 12), central(2026, 11, 4, 14))
    await link_ping(reservation_id)
    start, end = central(2026, 11, 4, 19), central(2026, 11, 4, 21, 15)

    await cog.update_saved_reservation(
        MESSAGE_ID, "Reservations Edited", [3, 14], start, end
    )

    select = """
        SELECT team, pcs, start_time, end_time, is_prime_time
        FROM reservations WHERE id = %s
    """
    # 7pm on a main room PC is prime time now, where noon wasn't
    assert await db.fetch_one(select, (reservation_id,)) == (
        "Reservations Edited",
        [3, 14],
        start,
        end,
        True,
    )
    [other_team] = await db.fetch_one(
        "SELECT team FROM reservations WHERE id = %s", (other_id,)
    )
    assert other_team == TEAM


@pytest.mark.asyncio
async def test_a_ping_with_no_saved_reservation_changes_nothing(
    cog, clean_reservations
):
    reservation_id = await insert(central(2026, 11, 4, 12), central(2026, 11, 4, 14))

    await cog.update_saved_reservation(
        MESSAGE_ID,
        "Reservations Edited",
        [3],
        central(2026, 11, 4, 19),
        central(2026, 11, 4, 21),
    )

    [team] = await db.fetch_one(
        "SELECT team FROM reservations WHERE id = %s", (reservation_id,)
    )
    assert team == TEAM
