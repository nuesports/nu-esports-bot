import pytest

from cogs import valorant


@pytest.fixture
def valorant_data(monkeypatch):
    fake = {
        "valorant": {
            "maps": ["Ascent", "Bind", "Haven"],
            "maps_active": [0, 2],
            "characters": ["Jett", "Sage", "Sova", "Omen", "Killjoy", "Reyna"],
            "agents_roles": {
                "Duelist": [0, 5],
                "Sentinel": [1, 4],
                "Initiator": [2],
                "Controller": [3],
            },
        }
    }
    monkeypatch.setattr(valorant.config, "game_data", fake)


def test_random_map_newest_returns_last_map(valorant_data):
    assert valorant.random_map("newest") == "Haven"


def test_random_map_active_only_picks_from_active_pool(valorant_data):
    for _ in range(20):
        assert valorant.random_map("active") in ("Ascent", "Haven")


def test_random_map_all_can_pick_any_map(valorant_data):
    for _ in range(20):
        assert valorant.random_map("all") in ("Ascent", "Bind", "Haven")


def test_random_team_role_balanced_returns_five_unique_agents(valorant_data):
    team = valorant.random_team("role-balanced")
    assert len(team) == 5
    assert len(set(team)) == 5


def test_random_team_random_returns_five_unique_agents(valorant_data):
    team = valorant.random_team("random")
    assert len(team) == 5
    assert len(set(team)) == 5
