from utils.payout import payout_multiplier


def test_multiplier_is_one_plus_pot_ratio():
    # 100 staked against 300 means every winner gets their stake back plus 3x it
    assert payout_multiplier(100, 300) == 4.0


def test_multiplier_is_break_even_when_nobody_opposed():
    assert payout_multiplier(100, 0) == 1.0


def test_multiplier_defaults_to_one_when_own_pot_is_empty():
    # Guards the division. Callers hit this whenever a side has no backers at all.
    assert payout_multiplier(0, 500) == 1.0


def test_multiplier_defaults_to_one_when_own_pot_is_negative():
    assert payout_multiplier(-50, 500) == 1.0


def test_multiplier_handles_equal_pots():
    assert payout_multiplier(250, 250) == 2.0


def test_multiplier_matches_the_inline_formula_it_replaced():
    """cogs/points.py used to compute this by hand. Same numbers, or the refactor
    silently changed everyone's prediction payouts."""
    for own, opposing in [(1, 1), (10, 90), (37, 113), (500, 1), (2, 999)]:
        assert payout_multiplier(own, opposing) == 1 + (opposing / own)
