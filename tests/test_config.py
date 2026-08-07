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


def test_load_game_data_raises_if_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        config.load_game_data()
