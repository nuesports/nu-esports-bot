import pytest

from utils import ranks


@pytest.fixture
def game_data(monkeypatch):
    fake = {
        "fakegame": {
            "tiers": ["Bronze", "Silver", "Gold", "Challenger"],
            "divisions": 4,
            "no_division_tiers": ["Challenger"],
        },
    }
    monkeypatch.setattr(ranks.config, "game_data", fake)


def test_compute_rank_value_divisioned_tier(game_data):
    assert ranks.compute_rank_value("fakegame", "Silver", 2) == 5


def test_compute_rank_value_flat_tier_ignores_division(game_data):
    assert ranks.compute_rank_value("fakegame", "Challenger", 1) == 12


def test_compute_rank_value_max_division_boundary(game_data):
    assert ranks.compute_rank_value("fakegame", "Gold", 4) == 11


def test_format_rank_label_divisioned_tier(game_data):
    assert ranks.format_rank_label("fakegame", "Silver", 2) == "Silver 2"


def test_format_rank_label_flat_tier_omits_division(game_data):
    assert ranks.format_rank_label("fakegame", "Challenger", 1) == "Challenger"
