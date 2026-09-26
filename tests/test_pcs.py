import base64
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from conftest import FakeInteraction, FakeMessage, select_interaction

from cogs import pcs


class FakeBooker:
    """The member submitting the modal -- complete() only reads their name."""

    def __init__(self, name="lilac", discriminator="0"):
        self.name = name
        self.discriminator = discriminator


@pytest.fixture
def cog():
    """PCs.__init__ wants a live bot, and the helpers never touch cog state."""
    return pcs.PCs.__new__(pcs.PCs)


def build(cog, slot, warnings, resolved):
    start, end = slot
    return pcs.build_warnings_embed(cog, start, end, warnings, resolved)


@pytest.mark.parametrize(
    "written",
    ["7:00PM", "7PM", "7pm", "7 PM", "7:00 pm", "07:00PM"],
    ids=[
        "canonical",
        "no minutes",
        "lowercase",
        "spaced",
        "spaced lowercase",
        "padded",
    ],
)
def test_parse_clock_accepts_every_shorthand(cog, written):
    assert cog.parse_clock(written).hour == 19


def test_parse_clock_keeps_minutes(cog):
    parsed = cog.parse_clock("7:30 pm")
    assert (parsed.hour, parsed.minute) == (19, 30)


def test_parse_clock_rejects_nonsense(cog):
    with pytest.raises(ValueError):
        cog.parse_clock("half past seven")


def test_parse_time_range_accepts_shorthand_on_both_ends(cog):
    start, end = cog.parse_time_range("2026-09-28 7pm-9:30 PM")
    assert (start.hour, start.minute) == (19, 0)
    assert (end.hour, end.minute) == (21, 30)
    assert start.date().isoformat() == "2026-09-28"


def test_parse_time_range_still_rejects_a_bad_time(cog):
    with pytest.raises(ValueError):
        cog.parse_time_range("2026-09-28 lunchtime-9:30PM")


@pytest.mark.parametrize(
    ("times", "closed"),
    [
        ("12:00AM-2:00AM", True),
        ("6:00AM-7:00AM", True),
        ("7:59AM-11:00AM", True),
        ("8:00AM-11:00AM", False),
        ("3:00PM-5:00PM", False),
    ],
)
def test_is_building_closed_covers_the_overnight_window(cog, times, closed):
    start, end = cog.parse_time_range(f"2026-09-28 {times}")
    assert cog.is_building_closed(start, end) is closed


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("7", 19),
        ("11", 23),
        ("12", 12),
        ("3", 15),
        ("07", 19),
    ],
    ids=["seven", "eleven", "noon stays noon", "three", "zero padded"],
)
def test_parse_clock_assumes_pm_when_unmarked(cog, written, expected):
    assert cog.parse_clock(written).hour == expected


def test_parse_clock_assumes_pm_with_minutes(cog):
    parsed = cog.parse_clock("7:30")
    assert (parsed.hour, parsed.minute) == (19, 30)


def test_parse_clock_still_honours_an_explicit_am(cog):
    assert cog.parse_clock("7am").hour == 7


def test_parse_time_range_reads_a_bare_range_as_evening(cog):
    start, end = cog.parse_time_range("2026-09-28 7-9:30")
    assert (start.hour, start.minute) == (19, 0)
    assert (end.hour, end.minute) == (21, 30)


# --- 24 hour input -----------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "expected"),
    [("19:00", (19, 0)), ("19", (19, 0)), ("23:30", (23, 30)), ("00:30", (0, 30))],
    ids=["19:00", "bare 19", "23:30", "after midnight"],
)
def test_parse_clock_reads_unambiguous_24_hour_times(cog, written, expected):
    parsed = cog.parse_clock(written)
    assert (parsed.hour, parsed.minute) == expected


@pytest.mark.parametrize("written", ["7:30", "7", "12:30"])
def test_parse_clock_leaves_ambiguous_times_to_the_pm_default(cog, written):
    # 7:30 is a valid 24-hour time too, but nobody booking a gameroom means 7:30am
    assert cog.parse_clock(written).hour >= 12


def test_parse_time_range_mixes_24_hour_and_shorthand(cog):
    start, end = cog.parse_time_range("2026-09-28 19:00-21:30")
    assert (start.hour, end.hour) == (19, 21)


# --- the two team lists cannot drift -----------------------------------------


def test_every_reservable_team_has_a_prime_time_quota():
    # the drift this branch exists to kill: Deadlock was in one list and not the other
    assert set(pcs.RESERVABLE_TEAMS) <= set(pcs.TEAM_PRIME_TIME_QUOTA)


def test_external_is_quota_only_and_never_offered_in_the_dropdown():
    assert "External" in pcs.TEAM_PRIME_TIME_QUOTA
    assert "External" not in pcs.RESERVABLE_TEAMS


def test_deadlock_is_reservable():
    assert "Deadlock Purple" in pcs.RESERVABLE_TEAMS


# --- the confirm embed --------------------------------------------------------


@pytest.fixture
def slot(cog):
    return cog.parse_time_range("2026-09-28 9:00AM-11:00AM")


def test_embed_warns_in_orange_while_anything_is_outstanding(cog, slot):
    embed = build(cog, slot, [pcs.WARN_OUTSIDE_HOURS], [pcs.WARN_SHORT_NOTICE])
    assert embed.title == "⚠️ Booking Warnings"
    assert embed.colour == discord.Color.orange()


def test_embed_turns_green_once_everything_is_struck_through(cog, slot):
    embed = build(cog, slot, [], [pcs.WARN_OUTSIDE_HOURS, pcs.WARN_SHORT_NOTICE])
    assert embed.title == "✅ Booking Ready"
    assert embed.colour == discord.Color.green()


def test_embed_strikes_only_the_resolved_lines(cog, slot):
    embed = build(cog, slot, [pcs.WARN_OUTSIDE_HOURS], [pcs.WARN_SHORT_NOTICE])
    lines = embed.description.split("\n")
    assert lines == [pcs.WARN_OUTSIDE_HOURS, f"~~{pcs.WARN_SHORT_NOTICE}~~"]


def test_embed_lists_warnings_in_a_fixed_order(cog, slot):
    # resolved first on input, but render order follows WARNING_ORDER either way
    embed = build(cog, slot, [pcs.WARN_SHORT_NOTICE], [pcs.WARN_OUTSIDE_HOURS])
    assert embed.description.split("\n") == [
        f"~~{pcs.WARN_OUTSIDE_HOURS}~~",
        pcs.WARN_SHORT_NOTICE,
    ]


def test_embed_shows_the_slot_and_that_days_hours(cog, slot):
    embed = build(cog, slot, [pcs.WARN_OUTSIDE_HOURS], [])
    booked, hours = embed.fields
    assert booked.name == "You booked"
    assert booked.value == "Monday, September 28, 2026\n09:00 AM - 11:00 AM"
    assert hours.name == "Monday's hours"
    assert hours.value == "02:30 PM - 11:00 PM"


# --- the confirm / edit / cancel buttons -------------------------------------


@pytest_asyncio.fixture
async def modal(cog):
    return pcs.ReservationTimeModal(
        cog,
        "Deadlock Purple",
        2,
        "Scrim",
        False,
        date_value="2026-09-28",
        start_value="9:00AM",
        end_value="11:00AM",
    )


@pytest_asyncio.fixture
async def view(modal, slot):
    start, end = slot
    return pcs.BookingWarningsView(
        modal,
        start,
        end,
        ("2026-09-28", "9:00AM", "11:00AM"),
        [pcs.WARN_OUTSIDE_HOURS],
        [],
    )


def labels(view):
    return [child.label for child in view.children if hasattr(child, "label")]


@pytest.mark.asyncio
async def test_confirm_button_reads_book_it_anyway_while_warned(view):
    assert labels(view) == ["Book it anyway", "Edit times", "Cancel"]


@pytest.mark.asyncio
async def test_confirm_button_reads_book_now_once_clean(modal, slot):
    start, end = slot
    clean = pcs.BookingWarningsView(
        modal, start, end, ("2026-09-28", "3PM", "5PM"), [], [pcs.WARN_OUTSIDE_HOURS]
    )
    assert labels(clean)[0] == "Book now"


@pytest.mark.asyncio
async def test_confirm_hands_every_outstanding_warning_to_complete(view, modal):
    passed = {}

    async def fake_complete(interaction, start_time, end_time, warnings):
        passed["warnings"] = warnings

    modal.complete = fake_complete
    interaction = FakeInteraction(FakeBooker())
    await view.children[0].callback(interaction)

    assert passed["warnings"] == [pcs.WARN_OUTSIDE_HOURS]
    assert view.is_finished()


@pytest.mark.asyncio
async def test_edit_reopens_the_modal_with_what_was_typed(view):
    interaction = FakeInteraction(FakeBooker())
    await view.children[1].callback(interaction)

    reopened = interaction.response.modals[0]
    assert [child.value for child in reopened.children] == [
        "2026-09-28",
        "9:00AM",
        "11:00AM",
    ]
    assert reopened.team == "Deadlock Purple"
    assert reopened.num_pcs == 2
    assert reopened.origin_view is view


@pytest.mark.asyncio
async def test_edit_leaves_the_buttons_live(view):
    # dismissing a modal fires no event, so retiring here would strand the booker
    await view.children[1].callback(FakeInteraction(FakeBooker()))
    assert not any(child.disabled for child in view.children)
    assert not view.is_finished()


@pytest.mark.asyncio
async def test_retire_disables_everything_and_says_why(view):
    view.prompt = FakeMessage()
    await view.retire("gone")

    assert all(child.disabled for child in view.children)
    assert view.prompt.edit_calls[0]["content"] == "gone"
    assert view.is_finished()


@pytest.mark.asyncio
async def test_timeout_retires_without_booking(view):
    view.prompt = FakeMessage()
    await view.on_timeout()
    assert "Nothing was booked" in view.prompt.edit_calls[0]["content"]


@pytest.mark.asyncio
async def test_cancel_asks_before_it_throws_the_times_away(view):
    interaction = FakeInteraction(FakeBooker())
    await view.children[2].callback(interaction)

    swapped = interaction.response.edits[0]["view"]
    assert isinstance(swapped, pcs.ReservationCancelView)
    assert swapped.parent is view
    # the booking is not gone yet -- the parent has to survive "No, go back"
    assert not view.is_finished()


# --- the cancel confirmation and the remake button ---------------------------


def choose(view, value):
    """Force a Select choice the way the other view tests in this suite do."""
    view.select._selected_values = [value]
    view.select._interaction = select_interaction()


@pytest.mark.asyncio
async def test_going_back_restores_the_prompt_untouched(view):
    cancel = pcs.ReservationCancelView(view)
    choose(cancel, "back")
    interaction = FakeInteraction(FakeBooker())

    await cancel.on_select(interaction)

    edit = interaction.response.edits[0]
    assert edit["view"] is view
    assert edit["embed"].title == "⚠️ Booking Warnings"
    assert not view.is_finished()


@pytest.mark.asyncio
async def test_confirming_the_cancel_reports_what_was_dropped(view):
    cancel = pcs.ReservationCancelView(view)
    choose(cancel, "confirm")
    interaction = FakeInteraction(FakeBooker())

    await cancel.on_select(interaction)

    edit = interaction.response.edits[0]
    assert edit["embed"].title == "❌ Booking Cancelled"
    assert edit["embed"].colour == discord.Color.red()
    assert edit["embed"].fields[0].name == "You Tried Booking"
    assert (
        edit["embed"].fields[0].value
        == "Monday, September 28, 2026\n09:00 AM - 11:00 AM"
    )
    assert isinstance(edit["view"], pcs.RemakeBookingView)
    assert view.is_finished()


@pytest.mark.asyncio
async def test_remake_reopens_the_modal_as_it_was(view):
    remake = pcs.RemakeBookingView(view.modal, view.raw_values)
    interaction = FakeInteraction(FakeBooker())

    await remake.children[0].callback(interaction)

    reopened = interaction.response.modals[0]
    assert [child.value for child in reopened.children] == [
        "2026-09-28",
        "9:00AM",
        "11:00AM",
    ]
    assert reopened.res_type == "Scrim"
    # a cancelled booking carries no live prompt to retire
    assert reopened.origin_view is None


# --- what the modal refuses outright -----------------------------------------


async def submit(modal, date, start, end):
    """Run the modal callback over the three fields, as a submission would."""
    for child, value in zip(modal.children, (date, start, end), strict=True):
        child.value = value
    interaction = FakeInteraction(FakeBooker())
    await modal.callback(interaction)
    return interaction


@pytest.mark.asyncio
async def test_an_unreadable_time_is_refused(modal):
    interaction = await submit(modal, "2026-09-28", "lunchtime", "5PM")
    assert "Invalid time format" in interaction.followup.send_calls[0]["content"]


@pytest.mark.asyncio
async def test_a_backwards_slot_is_refused(modal):
    interaction = await submit(modal, "2026-09-28", "9:00PM", "5:00PM")
    assert (
        "after the requested end time" in interaction.followup.send_calls[0]["content"]
    )


@pytest.mark.asyncio
async def test_norris_being_shut_is_a_hard_refusal(modal):
    interaction = await submit(modal, "2026-09-28", "3:00AM", "5:00AM")
    content = interaction.followup.send_calls[0]["content"]
    assert "Norris is closed" in content
    # no prompt, no override: a warning view would imply it could be waved through
    assert "view" not in interaction.followup.send_calls[0]


@pytest.mark.asyncio
async def test_a_refused_edit_leaves_its_prompt_standing(modal, view):
    # the bug this guards: retiring before the hard checks lost the booker's times
    modal.origin_view = view
    view.prompt = FakeMessage()

    await submit(modal, "2026-09-28", "3:00AM", "5:00AM")

    assert not view.is_finished()
    assert view.prompt.edit_calls == []


# --- what reaches the confirm prompt ------------------------------------------


@pytest.mark.asyncio
async def test_a_warned_slot_is_held_for_confirmation(modal, monkeypatch):
    completed = []
    monkeypatch.setattr(
        modal, "complete", lambda *a, **k: completed.append(a), raising=False
    )

    interaction = await submit(modal, "2026-09-28", "9:00AM", "11:00AM")

    sent = interaction.followup.send_calls[0]
    assert isinstance(sent["view"], pcs.BookingWarningsView)
    assert sent["embed"].title == "⚠️ Booking Warnings"
    assert completed == []


@pytest.mark.asyncio
async def test_a_clean_slot_books_without_a_prompt(modal):
    seen = {}

    async def fake_complete(interaction, start_time, end_time, warnings):
        seen["warnings"] = warnings

    modal.complete = fake_complete
    far_off = (datetime.now(pcs.CENTRAL_TZ) + timedelta(days=5)).strftime("%Y-%m-%d")

    interaction = await submit(modal, far_off, "3:00PM", "5:00PM")

    assert seen["warnings"] == []
    assert interaction.followup.send_calls == []


@pytest.mark.asyncio
async def test_an_edit_that_clears_one_warning_still_confirms(modal, view):
    # editing out of hours while still short-notice must not book on the booker's behalf
    modal.origin_view = view
    view.prompt = FakeMessage()
    view.warnings = [pcs.WARN_OUTSIDE_HOURS, pcs.WARN_SHORT_NOTICE]
    tomorrow = (datetime.now(pcs.CENTRAL_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")

    interaction = await submit(modal, tomorrow, "3:00PM", "5:00PM")

    prompt = interaction.followup.send_calls[0]
    assert prompt["view"].warnings == [pcs.WARN_SHORT_NOTICE]
    assert prompt["view"].resolved == [pcs.WARN_OUTSIDE_HOURS]
    assert view.is_finished()


@pytest.mark.asyncio
async def test_an_edit_that_clears_everything_asks_one_last_time(modal, view):
    modal.origin_view = view
    view.prompt = FakeMessage()
    view.warnings = [pcs.WARN_OUTSIDE_HOURS]
    far_off = (datetime.now(pcs.CENTRAL_TZ) + timedelta(days=5)).strftime("%Y-%m-%d")

    interaction = await submit(modal, far_off, "3:00PM", "5:00PM")

    prompt = interaction.followup.send_calls[0]
    assert prompt["embed"].title == "✅ Booking Ready"
    assert prompt["view"].warnings == []


# --- complete(): the prime time quota path ------------------------------------


class FakeRole:
    def __init__(self, id=77):
        self.id = id
        self.mention = f"<@&{id}>"


class FakeGuild:
    def __init__(self, role=None):
        self.role = role

    def get_role(self, role_id):
        return self.role


class FakeSentMessage(FakeMessage):
    """What channel.send() hands back -- complete() keys its ack tracker off the id."""

    id = 4321


class FakeReservationsChannel:
    def __init__(self, role=None):
        self.id = 5
        self.guild = FakeGuild(role)
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return FakeSentMessage()


@pytest_asyncio.fixture
async def booked(cog, modal, monkeypatch):
    """A cog whose db-backed steps all succeed, so complete() runs end to end."""

    async def no_conflict(*args):
        return (False, None, None)

    async def allocate(*args):
        return [1, 2]

    async def unlimited_quota(*args):
        return (True, 0)

    async def save(*args):
        return None

    cog.check_conflicts = no_conflict
    cog.allocate_pcs = allocate
    cog.check_prime_time_quota = unlimited_quota
    cog.save_reservation = save
    cog.team_prime_time_quota = pcs.TEAM_PRIME_TIME_QUOTA
    cog.pending_acknowledgments = {}
    cog.bot = SimpleNamespace(get_channel=lambda channel_id: None)
    monkeypatch.setattr(pcs, "STAFF_LIST", [])
    return cog


async def run_complete(modal, times="7:00PM-9:00PM", date="2026-09-28", warnings=None):
    start, end = modal.cog.parse_time_range(f"{date} {times}")
    interaction = FakeInteraction(FakeBooker())
    await modal.complete(interaction, start, end, warnings if warnings else [])
    return interaction


@pytest.mark.asyncio
async def test_a_conflict_refuses_before_anything_is_written(booked, modal):
    async def clash(*args):
        return (True, "Valorant White", "someone")

    booked.check_conflicts = clash
    interaction = await run_complete(modal)

    assert "Conflict with team" in interaction.followup.send_calls[0]["content"]


@pytest.mark.asyncio
async def test_no_free_pcs_refuses(booked, modal):
    async def nothing_free(*args):
        return []

    booked.allocate_pcs = nothing_free
    interaction = await run_complete(modal)

    assert "Unable to allocate" in interaction.followup.send_calls[0]["content"]


@pytest.mark.asyncio
async def test_an_exhausted_prime_time_quota_warns_rather_than_blocks(booked, modal):
    async def used_up(team, start_time):
        return (False, 1)

    booked.check_prime_time_quota = used_up
    interaction = await run_complete(modal)

    body = interaction.followup.send_calls[0]["content"]
    assert "Reservation confirmed" in body
    assert pcs.warn_prime_quota(1, 1) in body


@pytest.mark.asyncio
async def test_the_quota_is_read_from_the_team_table(booked, modal):
    asked = {}

    async def used_up(team, start_time):
        asked["team"] = team
        return (False, 2)

    booked.check_prime_time_quota = used_up
    interaction = await run_complete(modal)

    assert asked["team"] == "Deadlock Purple"
    # 2 used against Deadlock's allowance of 1
    assert "(2/1 used this week)" in interaction.followup.send_calls[0]["content"]


@pytest.mark.asyncio
async def test_bot_devs_skip_the_quota_check_entirely(booked, modal):
    async def explode(*args):
        raise AssertionError("bot devs must not be quota checked")

    booked.check_prime_time_quota = explode
    modal.is_bot_dev = True

    interaction = await run_complete(modal)

    assert "Reservation confirmed" in interaction.followup.send_calls[0]["content"]


@pytest.mark.asyncio
async def test_a_long_prime_time_booking_is_flagged(booked, modal):
    interaction = await run_complete(modal, times="7:00PM-10:30PM")
    assert pcs.WARN_LONG_PRIME in interaction.followup.send_calls[0]["content"]


@pytest.mark.asyncio
async def test_an_off_peak_booking_raises_neither_prime_warning(booked, modal):
    interaction = await run_complete(modal, times="12:00PM-3:30PM")
    body = interaction.followup.send_calls[0]["content"]
    assert pcs.WARN_LONG_PRIME not in body
    assert "Prime time quota" not in body


# --- what staff see -----------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_get_the_warnings_and_a_role_ping(booked, modal, monkeypatch):
    role = FakeRole()
    channel = FakeReservationsChannel(role)
    booked.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
    monkeypatch.setattr(pcs, "STAFF_LIST", [99])

    async def rotation():
        return 0

    booked.next_staff_index = rotation
    monkeypatch.setattr(pcs.config, "staff_role_id", lambda: role.id)

    await run_complete(modal, warnings=[pcs.WARN_OUTSIDE_HOURS])

    posted = channel.sent[0]
    assert posted["content"] == f"<@99> // ⚠️ {role.mention}"
    notes = next(f for f in posted["embed"].fields if f.name == "⚠️ Notes")
    assert notes.value == pcs.WARN_OUTSIDE_HOURS
    assert posted["allowed_mentions"].roles == [role]


@pytest.mark.asyncio
async def test_a_clean_booking_pings_only_the_rotation(booked, modal, monkeypatch):
    channel = FakeReservationsChannel(FakeRole())
    booked.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
    monkeypatch.setattr(pcs, "STAFF_LIST", [99])

    async def rotation():
        return 0

    booked.next_staff_index = rotation

    await run_complete(modal, times="12:00PM-2:00PM")

    posted = channel.sent[0]
    assert posted["content"] == "<@99>"
    assert not [f for f in posted["embed"].fields if f.name == "⚠️ Notes"]


def decode_booking_url(url):
    base, fragment = url.split("#nue=")
    padded = fragment + "=" * (-len(fragment) % 4)
    return base, json.loads(base64.urlsafe_b64decode(padded))


def test_booking_url_carries_the_reservation_in_the_fragment(cog):
    start, end = cog.parse_time_range("2026-09-29 5:15PM-6:45PM")
    url = pcs.ggleap_booking_url("Valorant White", [3, 1, 0], start, end, "a@b.edu")

    base, payload = decode_booking_url(url)
    assert base == pcs.GGLEAP_BOOKING_GRID_URL
    assert payload == {
        "v": 1,
        "team": "Valorant White",
        "pcs": [0, 1, 3],
        "start": "2026-09-29T17:15:00",
        "duration": 90,
        "email": "a@b.edu",
    }


def test_booking_url_fits_in_a_discord_button(cog):
    start, end = cog.parse_time_range("2026-09-29 12PM-11:59PM")
    every_pc = pcs.BACK_ROOM_PCS + pcs.MAIN_ROOM_PCS
    email = "someone.with.a.long.name2029@u.northwestern.edu"
    assert (
        len(pcs.ggleap_booking_url("Rocket League Purple", every_pc, start, end, email))
        <= 512
    )


@pytest.mark.asyncio
async def test_staff_get_a_book_in_ggleap_button(booked, modal, monkeypatch):
    channel = FakeReservationsChannel()
    booked.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
    monkeypatch.setattr(pcs.config, "gamehead_email", lambda username: "lilac@u.edu")

    await run_complete(modal, times="12:00PM-2:00PM")

    [button] = channel.sent[0]["view"].children
    assert button.label == "Book in ggLeap"
    _, payload = decode_booking_url(button.url)
    assert payload["pcs"] == [1, 2]
    assert payload["email"] == "lilac@u.edu"


# --- /reserve-external ---------------------------------------------------------


@pytest_asyncio.fixture
async def external(cog):
    """External skips advance notice and gameroom hours, but not the locked building."""

    async def nothing_overlaps(*args):
        return []

    async def save(*args):
        return None

    cog.get_reservations_in_range = nothing_overlaps
    cog.save_reservation = save
    return pcs.ExternalReservationTimeModal(cog)


@pytest.mark.asyncio
async def test_external_is_refused_while_norris_is_locked(external):
    interaction = await submit(external, "2026-09-28", "6:00AM", "10:00AM")
    assert "Norris is closed" in interaction.followup.send_calls[0]["content"]


@pytest.mark.asyncio
async def test_external_may_still_ignore_gameroom_hours(external):
    # 9am is outside gameroom hours, which staff can override, unlike the closure
    interaction = await submit(external, "2026-09-28", "9:00AM", "11:00AM")
    assert (
        "External reservation confirmed"
        in interaction.followup.send_calls[0]["content"]
    )


@pytest.mark.asyncio
async def test_external_still_refuses_a_backwards_slot(external):
    interaction = await submit(external, "2026-09-28", "9:00PM", "5:00PM")
    assert (
        "after the requested end time" in interaction.followup.send_calls[0]["content"]
    )


@pytest.mark.asyncio
async def test_external_refuses_when_something_already_holds_a_pc(external, cog):
    async def occupied(*args):
        return [{"team": "Valorant White"}]

    cog.get_reservations_in_range = occupied
    interaction = await submit(external, "2026-09-28", "3:00PM", "5:00PM")

    body = interaction.followup.send_calls[0]["content"]
    assert "Cannot reserve all PCs" in body
    assert "Valorant White" in body
