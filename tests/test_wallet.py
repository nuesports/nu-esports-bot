import pytest

from utils import wallet
from utils.wallet import payout_multiplier


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


# --- credit / try_deduct ---


class Recorder:
    """Captures the statements wallet would have run, and dictates what rowcount
    perform_one reports back."""

    def __init__(self):
        self.calls = []
        self.rowcount = 1


@pytest.fixture
def fake_db(monkeypatch):
    rec = Recorder()

    async def perform_one(sql, parameters=None):
        rec.calls.append((sql, parameters))
        return rec.rowcount

    async def perform_many(sql, parameters):
        rec.calls.append((sql, list(parameters)))

    monkeypatch.setattr(wallet.db, "perform_one", perform_one)
    monkeypatch.setattr(wallet.db, "perform_many", perform_many)
    return rec


@pytest.mark.asyncio
async def test_credit_adds_points_to_one_user(fake_db):
    await wallet.credit(7, 250)

    sql, params = fake_db.calls[0]
    assert "points = points + %s" in sql
    assert params == (250, 7)


@pytest.mark.asyncio
async def test_credit_many_takes_amount_first(fake_db):
    """Rows are (amount, discordid) to match the statement's parameter order, not
    the (user, amount) order the callers' dicts are keyed in."""
    await wallet.credit_many([(100, 7), (25, 8)])

    _, rows = fake_db.calls[0]
    assert rows == [(100, 7), (25, 8)]


@pytest.mark.asyncio
async def test_credit_many_skips_the_query_when_there_is_nothing_to_pay(fake_db):
    await wallet.credit_many([])
    assert fake_db.calls == []


@pytest.mark.asyncio
async def test_try_deduct_reports_success_when_the_balance_covered_it(fake_db):
    fake_db.rowcount = 1

    assert await wallet.try_deduct(7, 100) is True

    sql, params = fake_db.calls[0]
    assert "points >= %s" in sql  # the guard lives in the statement
    assert params == (100, 7, 100)


@pytest.mark.asyncio
async def test_try_deduct_reports_failure_when_the_guard_rejected_it(fake_db):
    fake_db.rowcount = 0
    assert await wallet.try_deduct(7, 100) is False


# --- distribute_payouts ---


def test_payouts_always_add_up_to_the_whole_pot():
    """Rounding each share on its own paid out 399 of this 400-point pot."""
    payouts = wallet.distribute_payouts({1: 100, 2: 100, 3: 100}, 100)

    assert sum(payouts.values()) == 400
    assert sorted(payouts.values()) == [133, 133, 134]


def test_payouts_do_not_mint_points_on_a_half_share():
    """round() rounds 1.5 up for both winners, paying 4 out of a 3-point pot."""
    payouts = wallet.distribute_payouts({1: 1, 2: 1}, 1)

    assert sum(payouts.values()) == 3


def test_a_lone_winner_takes_the_whole_pot():
    payouts = wallet.distribute_payouts({1: 250}, 750)

    assert payouts == {1: 1000}


def test_the_leftover_goes_to_the_largest_fractional_share_first():
    """One point to hand out, and the 200-point stake has the bigger remainder."""
    payouts = wallet.distribute_payouts({1: 200, 2: 100}, 100)

    assert sum(payouts.values()) == 400
    assert payouts[1] > payouts[2] * 2  # 267 vs 133, not 266 vs 134


def test_payouts_are_break_even_when_nobody_opposed():
    payouts = wallet.distribute_payouts({1: 100, 2: 50}, 0)

    assert payouts == {1: 100, 2: 50}


def test_the_same_book_always_splits_the_same_way():
    book = {1: 100, 2: 100, 3: 100}

    assert wallet.distribute_payouts(book, 100) == wallet.distribute_payouts(book, 100)
