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
    assert "points >= %s" in sql        # the guard lives in the statement
    assert params == (100, 7, 100)


@pytest.mark.asyncio
async def test_try_deduct_reports_failure_when_the_guard_rejected_it(fake_db):
    fake_db.rowcount = 0
    assert await wallet.try_deduct(7, 100) is False
