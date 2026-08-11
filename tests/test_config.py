import pytest
from utils import config


@pytest.fixture
def game_data(monkeypatch):
    fake = {
        "fakegame": {
            "per_role_ranks": True,
            "role_requirements": {"Tank": 1, "Support": 1},
            "role_icons": {"Tank": "🛡️"},
        },
        "flatgame": {},
    }
    monkeypatch.setattr(config, "game_data", fake)


def test_is_per_role_ranks_true(game_data):
    assert config.is_per_role_ranks("fakegame") is True


def test_is_per_role_ranks_false_when_missing(game_data):
    assert config.is_per_role_ranks("flatgame") is False


def test_rankable_roles(game_data):
    assert config.rankable_roles("fakegame") == ["Tank", "Support"]


def test_rankable_roles_empty_when_missing(game_data):
    assert config.rankable_roles("flatgame") == []


def test_role_icon_known(game_data):
    assert config.role_icon("fakegame", "Tank") == "🛡️"


def test_role_icon_unknown_returns_empty_string(game_data):
    assert config.role_icon("fakegame", "Support") == ""


def test_load_config_raises_if_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        config.load_config()


def test_load_secrets_raises_if_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        config.load_secrets()


class FakeGuildPermissions:
    def __init__(self, administrator=False):
        self.administrator = administrator


class FakeRole:
    def __init__(self, id):
        self.id = id


class FakeMember:
    def __init__(self, roles=None, administrator=False):
        self.roles = roles or []
        self.guild_permissions = FakeGuildPermissions(administrator)


@pytest.fixture
def gamehead_roles(monkeypatch):
    monkeypatch.setattr(config, "config", {"roles": {"gameheads": {"valorant": 111}}})


def test_is_game_head_true_for_admin(gamehead_roles):
    assert config.is_game_head(FakeMember(administrator=True)) is True


def test_is_game_head_true_for_matching_role(gamehead_roles):
    assert config.is_game_head(FakeMember(roles=[FakeRole(111)])) is True


def test_is_game_head_false_otherwise(gamehead_roles):
    assert config.is_game_head(FakeMember(roles=[FakeRole(222)])) is False


def test_load_game_data_raises_if_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        config.load_game_data()
