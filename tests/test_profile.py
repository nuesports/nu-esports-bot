import pytest
from cogs import profile


@pytest.fixture
def game_data(monkeypatch):
    fake = {
        "fakegame": {
            "tiers": ["Bronze", "Silver", "Gold"],
            "divisions": 3,
            "no_division_tiers": ["Gold"],
            "roles": ["Tank", "Support", "Flex"],
            "characters": ["Agent1", "Agent2"],
        }
    }
    monkeypatch.setattr(profile.config, "game_data", fake)
    return "fakegame"


# --- compute_rank_value / format_rank_label ---

def test_compute_rank_value_divided_tier(game_data):
    assert profile.compute_rank_value(game_data, "Bronze", 1) == 0
    assert profile.compute_rank_value(game_data, "Silver", 2) == 4


def test_compute_rank_value_flat_tier_ignores_division(game_data):
    assert profile.compute_rank_value(game_data, "Gold", 1) == 6


def test_format_rank_label(game_data):
    assert profile.format_rank_label(game_data, "Silver", 2) == "Silver 2"
    assert profile.format_rank_label(game_data, "Gold", 1) == "Gold"


# --- thin config.game_data passthroughs ---

def test_get_tiers(game_data):
    assert profile.get_tiers(game_data) == ["Bronze", "Silver", "Gold"]


def test_get_divisions(game_data):
    assert profile.get_divisions(game_data) == 3


def test_get_roles(game_data):
    assert profile.get_roles(game_data) == ["Tank", "Support", "Flex"]


def test_get_mains(game_data):
    assert profile.get_mains(game_data) == ["Agent1", "Agent2"]


def test_tier_has_divisions_true_for_divided_tier(game_data):
    assert profile.tier_has_divisions(game_data, "Bronze") is True


def test_tier_has_divisions_false_for_flat_tier(game_data):
    assert profile.tier_has_divisions(game_data, "Gold") is False


# --- validate_tier_division ---

def test_validate_tier_division_valid(game_data):
    assert profile.validate_tier_division(game_data, "Bronze", "2") == (2, None)


def test_validate_tier_division_missing_tier(game_data):
    division, error = profile.validate_tier_division(game_data, None, "1")
    assert division is None
    assert error is not None


def test_validate_tier_division_not_a_number(game_data):
    division, error = profile.validate_tier_division(game_data, "Bronze", "abc")
    assert division is None
    assert error is not None


def test_validate_tier_division_out_of_range(game_data):
    division, error = profile.validate_tier_division(game_data, "Bronze", "5")
    assert division is None
    assert error is not None


# --- effective_primary (no config needed at all) ---

def test_effective_primary_uses_explicit_value():
    assert profile.effective_primary(["A", "B"], "B") == "B"


def test_effective_primary_falls_back_to_first_main():
    assert profile.effective_primary(["A", "B"], None) == "A"


def test_effective_primary_no_mains_returns_none():
    assert profile.effective_primary([], None) is None


# --- is_game_head ---

class FakeGuildPermissions:
    def __init__(self, administrator=False):
        self.administrator = administrator


class FakeRole:
    def __init__(self, name):
        self.name = name


class FakeMember:
    def __init__(self, roles=None, administrator=False):
        self.roles = roles or []
        self.guild_permissions = FakeGuildPermissions(administrator)


def test_is_game_head_true_for_admin():
    member = FakeMember(administrator=True)
    assert profile.is_game_head(member) is True


def test_is_game_head_true_for_matching_role_case_insensitive():
    member = FakeMember(roles=[FakeRole("Valorant Game Head")])
    assert profile.is_game_head(member) is True


def test_is_game_head_false_otherwise():
    member = FakeMember(roles=[FakeRole("Member")])
    assert profile.is_game_head(member) is False


# --- normalize_tag ---

class FakeBot:
    def __init__(self, known_emoji_ids=None):
        self._known = set(known_emoji_ids or [])

    def get_emoji(self, id):
        return object() if id in self._known else None


def test_normalize_tag_accepts_real_unicode_emoji():
    assert profile.normalize_tag("🍣", FakeBot()) == "🍣"


def test_normalize_tag_converts_known_shortcode():
    assert profile.normalize_tag(":sushi:", FakeBot()) == "🍣"


def test_normalize_tag_accepts_known_custom_discord_emoji():
    bot = FakeBot(known_emoji_ids=[123456789012345678])
    value = "<:testemoji:123456789012345678>"
    assert profile.normalize_tag(value, bot) == value


def test_normalize_tag_rejects_unknown_custom_discord_emoji():
    bot = FakeBot(known_emoji_ids=[])
    assert profile.normalize_tag("<:testemoji:123456789012345678>", bot) is None


def test_normalize_tag_rejects_plain_text():
    assert profile.normalize_tag("not an emoji", FakeBot()) is None


def test_normalize_tag_rejects_empty_value():
    assert profile.normalize_tag("", FakeBot()) is None
    assert profile.normalize_tag(None, FakeBot()) is None
