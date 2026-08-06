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
