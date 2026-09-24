import pytest

from cogs import pcs


@pytest.fixture
def cog():
    """The time helpers never touch cog state, so an uninitialised instance is enough
    -- PCs.__init__ starts a background task and wants a live bot."""
    return pcs.PCs.__new__(pcs.PCs)


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
