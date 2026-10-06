import base64
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from conftest import (
    FakeApplicationContext,
    FakeInteraction,
    FakeMessage,
    select_interaction,
)

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


def test_both_smash_games_are_reservable():
    assert {"Smash Melee", "Smash Ultimate"} <= set(pcs.RESERVABLE_TEAMS)


@pytest_asyncio.fixture
async def empty_room(cog):
    async def nothing_booked(*args):
        return []

    cog.get_reservations_in_range = nothing_booked
    return cog


# 2026-09-29 is a tuesday, the back room used to close on it
TUESDAY_EVENING = (
    datetime(2026, 9, 29, 19, tzinfo=pcs.CENTRAL_TZ),
    datetime(2026, 9, 29, 21, tzinfo=pcs.CENTRAL_TZ),
)


@pytest.mark.asyncio
async def test_the_cap_is_exactly_what_the_allocator_can_hand_out(empty_room):
    assert pcs.MAX_RESERVABLE_PCS == 13
    most = await empty_room.allocate_pcs(*TUESDAY_EVENING, pcs.MAX_RESERVABLE_PCS)
    assert len(most) == pcs.MAX_RESERVABLE_PCS
    assert (
        await empty_room.allocate_pcs(*TUESDAY_EVENING, pcs.MAX_RESERVABLE_PCS + 1)
        == []
    )


@pytest.mark.asyncio
async def test_tuesday_books_the_back_room_first(empty_room):
    assert await empty_room.allocate_pcs(*TUESDAY_EVENING, 3) == [14, 15, 0]


@pytest.mark.asyncio
async def test_tuesday_conflicts_count_the_back_room(empty_room):
    clash, _, _ = await empty_room.check_conflicts(
        *TUESDAY_EVENING, pcs.MAX_RESERVABLE_PCS
    )
    assert not clash


# --- skipping the back room ---------------------------------------------------


def holding(team, held):
    """A reservation over the whole of TUESDAY_EVENING."""
    start, end = TUESDAY_EVENING
    return {
        "team": team,
        "manager": f"{team} head",
        "pcs": held,
        "start_time": start,
        "end_time": end,
    }


def occupy(cog, *reservations):
    async def overlapping(*args):
        return list(reservations)

    cog.get_reservations_in_range = overlapping


@pytest.mark.asyncio
async def test_skipping_the_back_room_books_the_main_room(empty_room):
    given = await empty_room.allocate_pcs(*TUESDAY_EVENING, 3, include_back_room=False)
    assert given == [1, 2, 3]


@pytest.mark.asyncio
async def test_skipping_the_back_room_caps_at_the_main_room(empty_room):
    whole_room = len(pcs.MAIN_ROOM_PCS)
    assert (
        await empty_room.allocate_pcs(
            *TUESDAY_EVENING, whole_room, include_back_room=False
        )
        == pcs.MAIN_ROOM_PCS
    )
    assert (
        await empty_room.allocate_pcs(
            *TUESDAY_EVENING, whole_room + 1, include_back_room=False
        )
        == []
    )


@pytest.mark.asyncio
async def test_a_booked_back_room_does_not_crowd_out_the_main_room(cog):
    occupy(cog, holding("Valorant White", [14, 15, 0]))
    whole_room = len(pcs.MAIN_ROOM_PCS)

    clash, _, _ = await cog.check_conflicts(
        *TUESDAY_EVENING, whole_room, include_back_room=False
    )

    assert not clash
    assert (
        await cog.allocate_pcs(*TUESDAY_EVENING, whole_room, include_back_room=False)
        == pcs.MAIN_ROOM_PCS
    )


@pytest.mark.asyncio
async def test_a_free_back_room_does_not_cover_for_a_full_main_room(cog):
    occupy(cog, holding("Apex White", list(range(1, 9))))

    skipped, team, _ = await cog.check_conflicts(
        *TUESDAY_EVENING, 3, include_back_room=False
    )
    with_back_room, _, _ = await cog.check_conflicts(*TUESDAY_EVENING, 3)

    assert skipped
    assert team == "Apex White"
    assert not with_back_room


@pytest.mark.asyncio
async def test_a_skipped_back_room_is_not_blamed_for_the_clash(cog):
    occupy(
        cog,
        holding("Valorant White", [14, 15, 0]),
        holding("Apex White", pcs.MAIN_ROOM_PCS),
    )

    clash, team, manager = await cog.check_conflicts(
        *TUESDAY_EVENING, 1, include_back_room=False
    )

    assert clash
    assert (team, manager) == ("Apex White", "Apex White head")


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
async def test_edit_keeps_the_back_room_skipped(view):
    view.modal.include_back_room = False
    interaction = FakeInteraction(FakeBooker())

    await view.children[1].callback(interaction)

    assert interaction.response.modals[0].include_back_room is False


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


@pytest.mark.asyncio
async def test_remake_keeps_the_back_room_skipped(view):
    view.modal.include_back_room = False
    remake = pcs.RemakeBookingView(view.modal, view.raw_values)
    interaction = FakeInteraction(FakeBooker())

    await remake.children[0].callback(interaction)

    assert interaction.response.modals[0].include_back_room is False


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
async def test_a_big_booking_is_held_for_confirmation(modal):
    modal.num_pcs = pcs.LARGE_BOOKING_PCS + 1
    far_off = (datetime.now(pcs.CENTRAL_TZ) + timedelta(days=5)).strftime("%Y-%m-%d")

    interaction = await submit(modal, far_off, "3:00PM", "5:00PM")

    assert interaction.followup.send_calls[0]["view"].warnings == [
        pcs.WARN_LARGE_BOOKING
    ]


@pytest.mark.asyncio
async def test_booking_right_at_the_threshold_does_not_warn(modal):
    seen = {}

    async def fake_complete(interaction, start_time, end_time, warnings):
        seen["warnings"] = warnings

    modal.complete = fake_complete
    modal.num_pcs = pcs.LARGE_BOOKING_PCS
    far_off = (datetime.now(pcs.CENTRAL_TZ) + timedelta(days=5)).strftime("%Y-%m-%d")

    await submit(modal, far_off, "3:00PM", "5:00PM")

    assert seen["warnings"] == []


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
    monkeypatch.setattr(pcs, "staff_list", list)
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
async def test_a_real_booking_is_saved(booked, modal):
    saved = []

    async def save(*args):
        saved.append(args)

    booked.save_reservation = save
    await run_complete(modal)

    assert len(saved) == 1


@pytest.mark.asyncio
async def test_a_test_booking_is_never_saved(booked, modal):
    async def explode(*args):
        raise AssertionError("test bookings must not be saved")

    booked.save_reservation = explode
    modal.is_test = True
    interaction = await run_complete(modal)

    assert "Test Reservation" in interaction.followup.send_calls[0]["content"]


@pytest.mark.asyncio
async def test_test_bookings_skip_the_quota_check_entirely(booked, modal):
    async def explode(*args):
        raise AssertionError("test bookings must not be quota checked")

    booked.check_prime_time_quota = explode
    modal.is_test = True

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


@pytest.mark.asyncio
async def test_complete_hands_the_skip_to_conflicts_and_allocation(booked, modal):
    seen = []

    async def no_conflict(*args):
        seen.append(("conflicts", args[-1]))
        return (False, None, None)

    async def allocate(*args):
        seen.append(("allocate", args[-1]))
        return [1, 2]

    booked.check_conflicts = no_conflict
    booked.allocate_pcs = allocate
    modal.include_back_room = False

    await run_complete(modal)

    assert seen == [("conflicts", False), ("allocate", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("include_back_room", "prime"), [(True, False), (False, True)])
async def test_skipping_the_back_room_makes_an_evening_prime_time(
    booked, modal, include_back_room, prime
):
    # the real allocator, so prime time judges what skipping actually hands out
    del booked.check_conflicts, booked.allocate_pcs
    occupy(booked)
    modal.num_pcs = 3
    modal.include_back_room = include_back_room

    interaction = await run_complete(modal)

    body = interaction.followup.send_calls[0]["content"]
    assert ("Prime Time Reservation" in body) is prime


# --- what staff see -----------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_get_the_warnings_and_a_role_ping(booked, modal, monkeypatch):
    role = FakeRole()
    channel = FakeReservationsChannel(role)
    booked.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
    monkeypatch.setattr(pcs, "staff_list", lambda: [99])

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
    monkeypatch.setattr(pcs, "staff_list", lambda: [99])

    async def rotation():
        return 0

    booked.next_staff_index = rotation

    await run_complete(modal, times="12:00PM-2:00PM")

    posted = channel.sent[0]
    assert posted["content"] == "<@99>"
    assert not [f for f in posted["embed"].fields if f.name == "⚠️ Notes"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_back_room", "listed"),
    [
        (True, "Main Room: PC 1, PC 2"),
        (False, "Back Room: skipped\nMain Room: PC 1, PC 2"),
    ],
)
async def test_staff_are_told_only_when_the_back_room_was_skipped(
    booked, modal, include_back_room, listed
):
    channel = FakeReservationsChannel()
    booked.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
    modal.include_back_room = include_back_room

    await run_complete(modal, times="12:00PM-2:00PM")

    field = next(f for f in channel.sent[0]["embed"].fields if f.name == "PCs")
    assert field.value == listed


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
        <= pcs.LINK_BUTTON_URL_LIMIT
    )


@pytest.mark.parametrize("email", ["a@b.edu", None], ids=["email", "no email"])
def test_a_booking_link_decodes_back_to_what_built_it(cog, email):
    start, end = cog.parse_time_range("2026-09-29 5:15PM-6:45PM")
    url = pcs.ggleap_booking_url("Valorant White", [3, 1, 0], start, end, email)

    booking = pcs.decode_ggleap_booking_url(url)

    assert booking == ("Valorant White", [0, 1, 3], start, end, email)
    assert pcs.ggleap_booking_url(*booking) == url


def encode_payload(payload):
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{pcs.GGLEAP_BOOKING_GRID_URL}#nue={raw}"


@pytest.mark.parametrize(
    "url",
    [
        pcs.GGLEAP_BOOKING_GRID_URL,
        f"{pcs.GGLEAP_BOOKING_GRID_URL}#nue=not*base64",
        encode_payload({"v": 2, "team": "Valorant White"}),
        encode_payload({"v": 1, "team": "Valorant White"}),
        encode_payload(["v", 1]),
    ],
    ids=["no fragment", "garbled", "newer version", "missing keys", "not an object"],
)
def test_decoding_refuses_a_link_it_did_not_write(url):
    with pytest.raises(ValueError):
        pcs.decode_ggleap_booking_url(url)


@pytest.mark.asyncio
async def test_staff_get_a_book_in_ggleap_button(booked, modal, monkeypatch):
    channel = FakeReservationsChannel()
    booked.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
    monkeypatch.setattr(pcs.config, "gamehead_email", lambda username: "lilac@u.edu")

    await run_complete(modal, times="12:00PM-2:00PM")

    [button, edit] = channel.sent[0]["view"].children
    assert button.label == "Book in ggLeap"
    _, payload = decode_booking_url(button.url)
    assert payload["pcs"] == [1, 2]
    assert payload["email"] == "lilac@u.edu"
    assert edit.custom_id == pcs.EDIT_BOOKING_ID


# --- staff editing the booking -------------------------------------------------


STAFF = SimpleNamespace(name="kai", mention="<@42>")


class FakeStaffPing(FakeMessage):
    """A posted staff ping as the edit button reads it: the embed and the real
    component types py-cord parses off the message, not the view that built them."""

    def __init__(self, embed, view):
        super().__init__()
        self.embeds = [embed]
        self.components = [
            discord.components.ActionRow(row) for row in view.to_components()
        ]


@pytest_asyncio.fixture
async def staff_ping(request, booked, modal, monkeypatch):
    # parametrise indirectly with False to book without the back room
    modal.include_back_room = getattr(request, "param", True)
    channel = FakeReservationsChannel()
    booked.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
    monkeypatch.setattr(pcs.config, "gamehead_email", lambda username: "lilac@u.edu")
    monkeypatch.setattr(pcs.config, "is_gameroom_staff", lambda member: member is STAFF)

    await run_complete(modal, times="12:00PM-2:00PM")

    return FakeStaffPing(channel.sent[0]["embed"], channel.sent[0]["view"])


def press(message, user, custom_id=pcs.EDIT_BOOKING_ID):
    interaction = FakeInteraction(user)
    interaction.data = {"custom_id": custom_id}
    interaction.message = message
    return interaction


async def submit_edit(cog, message, team, typed_pcs, date, start, end):
    """Press Edit booking, then submit its modal with the five fields."""
    pressed = press(message, STAFF)
    await cog.on_interaction(pressed)
    modal = pressed.response.modals[0]
    values = (team, typed_pcs, date, start, end)
    for child, value in zip(modal.children, values, strict=True):
        child.value = value
    interaction = press(message, STAFF)
    await modal.callback(interaction)
    return interaction


@pytest.mark.asyncio
async def test_only_gameroom_staff_may_edit_a_booking(booked, staff_ping):
    interaction = press(staff_ping, FakeBooker())
    await booked.on_interaction(interaction)

    refusal = interaction.response.messages[0]
    assert "Only gameroom staff" in refusal["content"]
    assert refusal["ephemeral"] is True
    assert interaction.response.modals == []


@pytest.mark.asyncio
async def test_other_buttons_are_not_mistaken_for_the_edit(booked, staff_ping):
    interaction = press(staff_ping, STAFF, custom_id="config-undo:3")
    await booked.on_interaction(interaction)

    assert interaction.response.messages == []
    assert interaction.response.modals == []


@pytest.mark.asyncio
async def test_edit_prefills_the_modal_from_the_book_link(booked, staff_ping):
    interaction = press(staff_ping, STAFF)
    await booked.on_interaction(interaction)

    modal = interaction.response.modals[0]
    assert isinstance(modal, pcs.EditBookingModal)
    assert [child.value for child in modal.children] == [
        "Deadlock Purple",
        "1, 2",
        "2026-09-28",
        "12:00PM",
        "2:00PM",
    ]


@pytest.mark.asyncio
async def test_an_edit_rebuilds_the_link_and_the_embed(booked, staff_ping):
    interaction = await submit_edit(
        booked, staff_ping, "Valorant White", "3, 0, 14", "2026-09-30", "5", "7:30"
    )

    edited = interaction.response.edits[0]
    [book, edit] = edited["view"].children
    start, end = booked.parse_time_range("2026-09-30 5PM-7:30PM")
    assert pcs.decode_ggleap_booking_url(book.url) == (
        "Valorant White",
        [0, 3, 14],
        start,
        end,
        "lilac@u.edu",
    )
    assert edit.custom_id == pcs.EDIT_BOOKING_ID

    fields = {field.name: field for field in edited["embed"].fields}
    assert fields["Team"].value == "Valorant White"
    assert fields["Date"].value == "Wednesday, September 30, 2026"
    assert fields["Time"].value == "05:00 PM - 07:30 PM CST"
    assert fields["PCs"].value == "Back Room: Streaming, PC 14\nMain Room: PC 3"
    assert fields["✏️ Edited"].value.startswith("by <@42> ")
    # the rest of the ping is what was booked, edited fields keep their place
    assert fields["Res Type"].value == "Scrim"
    assert fields["Manager"].value == "lilac"
    assert edited["embed"].fields[0].name == "Team"
    assert fields["Time"].inline is True


@pytest.mark.asyncio
async def test_an_edit_rounds_its_times_to_quarter_hours(booked, staff_ping):
    interaction = await submit_edit(
        booked, staff_ping, "Valorant White", "3", "2026-09-30", "7:42", "9:08"
    )

    edited = interaction.response.edits[0]
    time = next(f for f in edited["embed"].fields if f.name == "Time")
    assert time.value == "07:45 PM - 09:15 PM CST"
    start, end = pcs.decode_ggleap_booking_url(edited["view"].children[0].url)[2:4]
    assert (start.strftime("%H:%M"), end.strftime("%H:%M")) == ("19:45", "21:15")


@pytest.mark.asyncio
async def test_a_second_edit_reads_the_first_and_keeps_one_note(booked, staff_ping):
    first = await submit_edit(
        booked, staff_ping, "Valorant White", "3", "2026-09-30", "5", "7"
    )
    edited = first.response.edits[0]
    reposted = FakeStaffPing(edited["embed"], edited["view"])

    second = await submit_edit(
        booked, reposted, "Valorant White", "3, 4", "2026-09-30", "5", "8"
    )

    fields = second.response.edits[0]["embed"].fields
    assert [field.name for field in fields].count("✏️ Edited") == 1
    assert next(f for f in fields if f.name == "PCs").value == "Main Room: PC 3, PC 4"


@pytest.mark.asyncio
@pytest.mark.parametrize("staff_ping", [False], indirect=True)
async def test_an_edit_keeps_the_back_room_skipped_note(booked, staff_ping):
    interaction = await submit_edit(
        booked, staff_ping, "Valorant White", "3, 4", "2026-09-30", "5", "7"
    )

    fields = interaction.response.edits[0]["embed"].fields
    listed = next(f for f in fields if f.name == "PCs").value
    assert listed == "Back Room: skipped\nMain Room: PC 3, PC 4"


@pytest.mark.asyncio
@pytest.mark.parametrize("staff_ping", [False], indirect=True)
async def test_adding_a_back_room_pc_drops_the_skipped_note(booked, staff_ping):
    interaction = await submit_edit(
        booked, staff_ping, "Valorant White", "3, 14", "2026-09-30", "5", "7"
    )

    fields = interaction.response.edits[0]["embed"].fields
    listed = next(f for f in fields if f.name == "PCs").value
    assert listed == "Back Room: PC 14\nMain Room: PC 3"


@pytest.mark.asyncio
async def test_an_edit_leaves_the_saved_reservation_alone(booked, staff_ping):
    async def forbidden(*args):
        raise AssertionError("an edit must not write to the reservations table")

    booked.save_reservation = forbidden
    booked.pending_acknowledgments = {4321: {"team": "Deadlock Purple"}}

    await submit_edit(booked, staff_ping, "Valorant White", "3", "2026-09-30", "5", "7")

    assert booked.pending_acknowledgments == {4321: {"team": "Deadlock Purple"}}


@pytest.mark.parametrize(
    ("team", "typed_pcs", "start", "end", "refusal"),
    [
        ("  ", "1, 2", "12PM", "2PM", "team can't be blank"),
        ("Deadlock Purple", "1, 11", "12PM", "2PM", "There's no PC 11"),
        ("Deadlock Purple", "1, 2, 1", "12PM", "2PM", "PC 1 is listed more than once"),
        ("Deadlock Purple", "one, two", "12PM", "2PM", "PCs must be numbers"),
        ("Deadlock Purple", ",", "12PM", "2PM", "at least one PC"),
        ("Deadlock Purple", "1, 2", "lunchtime", "2PM", "Invalid time format"),
        ("Deadlock Purple", "1, 2", "4PM", "2PM", "has to end after it starts"),
        ("Deadlock Purple", "1, 2", "2PM", "2PM", "has to end after it starts"),
        ("x" * 400, "1, 2", "12PM", "2PM", "too long to fit in the ggLeap link"),
    ],
    ids=[
        "blank team",
        "no such pc",
        "repeated pc",
        "not numbers",
        "no pcs",
        "bad time",
        "backwards",
        "zero length",
        "link too long",
    ],
)
@pytest.mark.asyncio
async def test_a_bad_edit_is_refused_and_changes_nothing(
    booked, staff_ping, team, typed_pcs, start, end, refusal
):
    interaction = await submit_edit(
        booked, staff_ping, team, typed_pcs, "2026-09-28", start, end
    )

    sent = interaction.response.messages[0]
    assert sent["content"].startswith("❌")
    assert refusal in sent["content"]
    assert sent["ephemeral"] is True
    assert interaction.response.edits == []


def central(day, hour, minute, second=0):
    return datetime(2026, 9, day, hour, minute, second, tzinfo=pcs.CENTRAL_TZ)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (central(28, 19, 7), central(28, 19, 0)),
        (central(28, 19, 8), central(28, 19, 15)),
        (central(28, 19, 7, 30), central(28, 19, 15)),
        (central(28, 19, 52), central(28, 19, 45)),
        (central(28, 19, 53), central(28, 20, 0)),
        (central(28, 23, 55), central(29, 0, 0)),
    ],
    ids=[
        "7 rounds down",
        "8 rounds up",
        "half rounds up",
        "52 rounds down",
        "53 rolls into the next hour",
        "11:55 PM rolls into tomorrow",
    ],
)
def test_staff_times_round_to_the_nearest_quarter_hour(moment, expected):
    rounded = pcs.round_to_quarter_hour(moment)
    assert rounded == expected
    assert rounded.tzinfo is pcs.CENTRAL_TZ


def test_a_slot_that_rounds_to_nothing_still_lasts_a_quarter_hour():
    start, end = pcs.round_slot(central(28, 19, 1), central(28, 19, 6))
    assert (start, end) == (central(28, 19, 0), central(28, 19, 15))


@pytest.mark.asyncio
async def test_staff_get_quarter_hours_while_the_booking_keeps_its_minutes(
    booked, modal
):
    saved = []

    async def save(*args):
        saved.append(args)

    booked.save_reservation = save
    channel = FakeReservationsChannel()
    booked.bot = SimpleNamespace(get_channel=lambda channel_id: channel)

    interaction = await run_complete(modal, times="12:08PM-1:52PM")

    assert saved[0][2:4] == (central(28, 12, 8), central(28, 13, 52))
    assert "12:08 PM - 01:52 PM" in interaction.followup.send_calls[0]["content"]
    posted = channel.sent[0]
    time = next(f for f in posted["embed"].fields if f.name == "Time")
    assert time.value == "12:15 PM - 01:45 PM CST"
    button, _edit = posted["view"].children
    _, payload = decode_booking_url(button.url)
    assert (payload["start"], payload["duration"]) == ("2026-09-28T12:15:00", 90)


# --- /reserve -----------------------------------------------------------------


class FakeReserveContext(FakeApplicationContext):
    """/reserve ends on send_modal, and its refusals need their text kept."""

    def __init__(self, author):
        super().__init__(author)
        self.modals = []

    async def respond(self, content=None, **kwargs):
        self.respond_calls.append({"content": content, **kwargs})

    async def send_modal(self, modal):
        self.modals.append(modal)


async def reserve(cog, monkeypatch, num_pcs, back_room):
    monkeypatch.setattr(pcs.config, "can_reserve", lambda member: True)
    ctx = FakeReserveContext(FakeBooker())
    await pcs.PCs.reserve.callback(
        cog,
        ctx,
        "Deadlock Purple",
        num_pcs,
        "Scrim",
        "no" if back_room else "yes",
        False,
    )
    return ctx


@pytest.mark.asyncio
async def test_skipping_the_back_room_refuses_more_than_the_main_room_holds(
    cog, monkeypatch
):
    ctx = await reserve(cog, monkeypatch, len(pcs.MAIN_ROOM_PCS) + 1, False)

    [refusal] = ctx.respond_calls
    assert f"Only {len(pcs.MAIN_ROOM_PCS)} PCs" in refusal["content"]
    assert refusal["ephemeral"]
    assert ctx.modals == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("num_pcs", "back_room"),
    [(pcs.MAX_RESERVABLE_PCS, True), (len(pcs.MAIN_ROOM_PCS), False)],
    ids=["everything", "the whole main room"],
)
async def test_up_to_the_cap_opens_the_modal(cog, monkeypatch, num_pcs, back_room):
    ctx = await reserve(cog, monkeypatch, num_pcs, back_room)

    [opened] = ctx.modals
    assert opened.num_pcs == num_pcs
    assert opened.include_back_room is back_room
    assert ctx.respond_calls == []


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
