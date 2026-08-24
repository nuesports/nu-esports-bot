import discord
import pytest

from cogs import leaderboard


class FakeMember:
    def __init__(self, display_name):
        self.display_name = display_name


class FakeGuild:
    def __init__(self, members: dict[int, FakeMember]):
        self._members = members

    def get_member(self, discordid):
        return self._members.get(discordid)


def test_leaderboard_label_no_role():
    assert leaderboard.leaderboard_label("valorant", None) == "Valorant"


def test_leaderboard_label_with_role():
    assert leaderboard.leaderboard_label("overwatch", "Tank") == "Overwatch Tank"


def test_format_entry_known_member():
    guild = FakeGuild({1: FakeMember("Alex")})
    line = leaderboard.format_entry(guild, 1, 1, 10, 5, "🍣", "valorant", None)
    assert line == "1. 🍣 *Alex* — **10W** / **5L**"


def test_format_entry_unknown_member_uses_mention():
    guild = FakeGuild({})
    line = leaderboard.format_entry(guild, 1, 999, 10, 5, "🍣", "valorant", None)
    assert "<@999>" in line


def test_format_entry_default_tag_is_star():
    guild = FakeGuild({1: FakeMember("Alex")})
    line = leaderboard.format_entry(guild, 1, 1, 10, 5, None, "valorant", None)
    assert "⭐" in line


def test_format_entry_shows_role_icon_when_entry_role_set(monkeypatch):
    monkeypatch.setattr(leaderboard.config, "game_data", {
        "overwatch": {"role_icons": {"Tank": "🛡️"}}
    })
    guild = FakeGuild({1: FakeMember("Alex")})
    line = leaderboard.format_entry(guild, 1, 1, 5, 2, "🍣", "overwatch", "Tank")
    assert "🛡️" in line


def test_build_leaderboard_pages_no_present_members_returns_none():
    guild = FakeGuild({})
    rows = [(1, 5, 2, None, None)]
    assert leaderboard.build_leaderboard_pages(guild, "valorant", rows, caller_id=1) is None


def test_build_leaderboard_pages_splits_into_pages_of_ten():
    members = {i: FakeMember(f"Player{i}") for i in range(1, 13)}
    guild = FakeGuild(members)
    rows = [(i, 10, 0, None, None) for i in range(1, 13)]
    pages = leaderboard.build_leaderboard_pages(guild, "valorant", rows, caller_id=1)
    assert len(pages) == 2
    assert pages[0].footer.text == "Page 1/2"
    assert pages[1].footer.text == "Page 2/2"


def test_build_leaderboard_pages_caller_not_ranked_shows_prompt():
    members = {1: FakeMember("Player1")}
    guild = FakeGuild(members)
    rows = [(1, 10, 0, None, None)]
    pages = leaderboard.build_leaderboard_pages(guild, "valorant", rows, caller_id=999)
    assert "haven't played" in pages[0].description


def test_build_leaderboard_pages_pins_caller_when_below_current_page():
    members = {i: FakeMember(f"Player{i}") for i in range(1, 12)}
    guild = FakeGuild(members)
    rows = [(i, 10, 0, None, None) for i in range(1, 12)]
    pages = leaderboard.build_leaderboard_pages(guild, "valorant", rows, caller_id=11)
    assert "..." in pages[0].description
    assert "Player11" in pages[0].description


# --- points board ---


class FakeAutocompleteContext:
    def __init__(self, options):
        self.options = options


@pytest.fixture
def fake_fetch_all(monkeypatch):
    """Records the leaderboard's fetch_all calls and replays a canned result instead of
    running them. leaderboard does `from utils import db`, so patching the attribute on
    that module object covers every call site in the cog."""
    class Recorder:
        def __init__(self):
            self.calls = []
            self.result = []

    rec = Recorder()

    async def fetch_all(sql, parameters=None):
        rec.calls.append((sql, parameters))
        return rec.result

    monkeypatch.setattr(leaderboard.db, "fetch_all", fetch_all)
    return rec


def test_is_game_board_rejects_the_points_board():
    assert leaderboard.is_game_board("valorant") is True
    assert leaderboard.is_game_board("points") is False


def test_leaderboard_label_ignores_a_role_on_the_points_board():
    """Points isn't per-role, so a role passed alongside it can't reach the title."""
    assert leaderboard.leaderboard_label("points", "Tank") == "Points"


def test_format_points_entry_groups_the_balance_with_commas():
    guild = FakeGuild({1: FakeMember("Alex")})
    line = leaderboard.format_points_entry(guild, 1, 1, 12480, "🍣")
    assert line == "1. 🍣 *Alex* — **12,480 points**"


def test_format_points_entry_unknown_member_uses_mention():
    guild = FakeGuild({})
    line = leaderboard.format_points_entry(guild, 1, 999, 10, "🍣")
    assert "<@999>" in line


def test_format_points_entry_default_tag_is_star():
    guild = FakeGuild({1: FakeMember("Alex")})
    line = leaderboard.format_points_entry(guild, 1, 1, 10, None)
    assert "⭐" in line


def test_build_leaderboard_pages_accepts_points_shaped_rows():
    """The builder only assumes row[0] is a discord id -- the rest is the formatter's
    business, which is what lets a three-wide points row reuse the game paging."""
    members = {i: FakeMember(f"Player{i}") for i in range(1, 13)}
    guild = FakeGuild(members)
    rows = [(i, 100 - i, None) for i in range(1, 13)]
    pages = leaderboard.build_leaderboard_pages(
        guild, "points", rows, caller_id=1,
        format_row=lambda rank, row: leaderboard.format_points_entry(guild, rank, *row),
    )
    assert len(pages) == 2
    assert pages[0].footer.text == "Page 1/2"
    assert pages[0].title == "Points Leaderboard"
    assert "**99 points**" in pages[0].description


def test_build_leaderboard_pages_pins_caller_on_a_points_board():
    members = {i: FakeMember(f"Player{i}") for i in range(1, 12)}
    guild = FakeGuild(members)
    rows = [(i, 100 - i, None) for i in range(1, 12)]
    pages = leaderboard.build_leaderboard_pages(
        guild, "points", rows, caller_id=11,
        format_row=lambda rank, row: leaderboard.format_points_entry(guild, rank, *row),
    )
    assert "..." in pages[0].description
    assert "Player11" in pages[0].description


def test_build_leaderboard_pages_uses_the_given_unranked_note():
    """"You haven't played Points yet!" would be nonsense on a board everyone is on."""
    guild = FakeGuild({1: FakeMember("Player1")})
    rows = [(1, 50, None)]
    pages = leaderboard.build_leaderboard_pages(
        guild, "points", rows, caller_id=999,
        format_row=lambda rank, row: leaderboard.format_points_entry(guild, rank, *row),
        unranked_note="You have no points yet!",
    )
    assert "You have no points yet!" in pages[0].description
    assert "haven't played" not in pages[0].description


@pytest.mark.asyncio
async def test_fetch_points_rows_orders_by_a_coalesced_balance(fake_fetch_all):
    """users.points is nullable and Postgres sorts NULLs first under DESC, so a bare
    ORDER BY points DESC would seat a NULL row at rank 1."""
    fake_fetch_all.result = [(1, 50, "🍣"), (2, 10, None)]

    rows = await leaderboard.fetch_points_rows(caller_id=1)

    sql, _ = fake_fetch_all.calls[0]
    assert "FROM users u" in sql
    assert "COALESCE(u.points, 0) DESC" in sql
    assert "LEFT JOIN profiles" in sql
    assert rows == [(1, 50, "🍣"), (2, 10, None)]


@pytest.mark.asyncio
async def test_fetch_points_rows_appends_the_caller_at_zero(fake_fetch_all):
    """Someone who's never spoken has no users row at all, and should read as 0 rather
    than dropping off a board everyone is technically on."""
    fake_fetch_all.result = [(1, 50, "🍣")]

    rows = await leaderboard.fetch_points_rows(caller_id=999)

    assert rows[-1] == (999, 0, None)


@pytest.mark.asyncio
async def test_fetch_points_rows_does_not_duplicate_a_caller_who_already_has_a_row(fake_fetch_all):
    fake_fetch_all.result = [(1, 50, "🍣"), (999, 0, None)]

    rows = await leaderboard.fetch_points_rows(caller_id=999)

    assert [row[0] for row in rows].count(999) == 1


@pytest.mark.asyncio
async def test_build_points_pages_reads_as_a_points_board(fake_fetch_all):
    fake_fetch_all.result = [(1, 12480, "🍣"), (2, 9003, None)]
    guild = FakeGuild({1: FakeMember("Lilac"), 2: FakeMember("somebody")})

    pages = await leaderboard.build_points_pages(guild, caller_id=1)

    assert pages[0].title == "Points Leaderboard"
    assert "1. 🍣 *Lilac* — **12,480 points**" in pages[0].description
    assert "2. ⭐ *somebody* — **9,003 points**" in pages[0].description


@pytest.mark.asyncio
async def test_role_autocomplete_offers_nothing_for_the_points_board():
    """config.is_per_role_ranks subscripts game_data directly, so an unguarded "points"
    would raise KeyError here rather than returning no suggestions."""
    ctx = FakeAutocompleteContext({"game": "points"})
    assert await leaderboard.role_autocomplete(ctx) == []


@pytest.mark.asyncio
async def test_paginator_has_no_change_role_button_on_the_points_board():
    """is_per_role_ranks would KeyError on "points" here, before the button ever mattered."""
    paginator = leaderboard.LeaderboardPaginator(
        requester_id=1, pages=[discord.Embed()], guild=FakeGuild({}), game="points"
    )
    assert not any(getattr(child, "label", None) == "Change Role" for child in paginator.children)


@pytest.mark.asyncio
async def test_game_select_offers_points_alongside_the_games():
    view = leaderboard.GameSelectView(requester_id=1, guild=FakeGuild({}))
    values = [option.value for option in view.select.options]
    assert "points" in values
    assert set(leaderboard.GAME_CHOICES).issubset(values)
    assert next(o.label for o in view.select.options if o.value == "points") == "Points"


# --- role picker timeout ---


class FakeCaller:
    def __init__(self, id):
        self.id = id


class FakeRoleSelectResponse:
    def __init__(self):
        self.edits = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class FakeRoleSelectInteraction:
    """interaction.original_response() is what a handler uses to recover the message it
    just edited, which conftest's FakeInteraction has no need for."""
    def __init__(self, user_id):
        self.user = FakeCaller(user_id)
        self.response = FakeRoleSelectResponse()
        self.original = object()

    async def original_response(self):
        return self.original


@pytest.mark.asyncio
async def test_role_select_greys_itself_out_on_timeout():
    """Its three sibling views all pass this. Without it the dropdown still renders as
    live after the timeout, but the click is no longer routed anywhere."""
    view = leaderboard.LeaderboardRoleSelectView(requester_id=1, guild=FakeGuild({}), game="overwatch")
    assert view.disable_on_timeout is True


@pytest.mark.asyncio
async def test_role_select_gives_its_paginator_a_message_handle(monkeypatch):
    """discord.ui.View.message is only set once someone clicks, so a paginator opened
    from here had nothing for on_timeout to edit and stayed un-greyed."""
    async def fake_rows(game, role=None):
        return [(1, 10, 0, None, None)]

    monkeypatch.setattr(leaderboard, "fetch_leaderboard_rows", fake_rows)

    view = leaderboard.LeaderboardRoleSelectView(
        requester_id=1, guild=FakeGuild({1: FakeMember("Alex")}), game="overwatch"
    )
    view.select._selected_values = [view._MIXED]
    view.select._interaction = object()

    interaction = FakeRoleSelectInteraction(1)
    await view.on_select(interaction)

    paginator = interaction.response.edits[0]["view"]
    assert paginator.message is interaction.original
