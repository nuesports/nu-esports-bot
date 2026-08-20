import pytest

from utils import elo


@pytest.fixture
def game_data(monkeypatch):
    """A timy fake game so that tests dont depend on real `data/` yamls"""
    fake = {
        "fakegame": {
            "tiers": ["Bronze", "Silver", "Gold"],
            "divisions": 3,
            "no_division_tiers": ["Gold"],
            "rank_points": {"Bronze": 0, "Silver": 300, "Gold": 600},
            "divisions_ascend": False,
            "default_tier": "Bronze",
        }
    }
    monkeypatch.setattr(elo.config, "game_data", fake)
    return "fakegame"

def test_decode_rank_value_none():
    assert elo.decode_rank_value("fakegame", None) is None


def test_decode_rank_value_with_division(game_data):
    assert elo.decode_rank_value(game_data, 0) == ("Bronze", 1)
    assert elo.decode_rank_value(game_data, 2) == ("Bronze", 3)


def test_decode_rank_value_no_division_tier(game_data):
    assert elo.decode_rank_value(game_data, 6) == ("Gold", None)


def test_compute_rank_points_interpolates_within_tier(game_data):
    assert elo.compute_rank_points(game_data, "Bronze", 1) == pytest.approx(200)
    assert elo.compute_rank_points(game_data, "Bronze", 3) == pytest.approx(0)


def test_seed_elo_no_rank_falls_back_to_default(game_data):
    assert elo.seed_elo(game_data, None) == pytest.approx(0)


def test_compute_elo_deltas_equal_teams_winner_gains():
    team_a = {1: 1000, 2: 1000}
    team_b = {3: 1000, 4: 1000}
    deltas = elo.compute_elo_deltas(team_a, team_b, a_won=True)
    assert deltas[1] > 0
    assert deltas[3] < 0