import pytest
import pytest_asyncio

from cogs import points
from tests.conftest import FakeApplicationContext

TEST_DISCORDID = 900000000000000002  # obviously-fake id, isolated test DB anyway


class FakeUser:
    def __init__(self, id, display_name):
        self.id = id
        self.display_name = display_name


@pytest.mark.asyncio
async def test_balance_reads_own_points(migrated_db):
    from utils import db
    await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
    try:
        await db.perform_one(
            "INSERT INTO users (discordid, points) VALUES (%s, %s);",
            (TEST_DISCORDID, 42),
        )
        user = FakeUser(TEST_DISCORDID, "caviar")
        ctx = FakeApplicationContext(author=user)

        await points.Points.balance.callback(object(), ctx, None)

        assert ctx.deferred
        assert len(ctx.followup.send_calls) == 1
        embed = ctx.followup.send_calls[0]["embed"]
        assert embed.title == "caviar's points"
        assert embed.description == "42 points"
    finally:
        await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))


@pytest.mark.asyncio
async def test_balance_defaults_to_zero_when_no_row(migrated_db):
    from utils import db
    await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))

    user = FakeUser(TEST_DISCORDID, "caviar")
    ctx = FakeApplicationContext(author=user)

    await points.Points.balance.callback(object(), ctx, None)

    embed = ctx.followup.send_calls[0]["embed"]
    assert embed.description == "0 points"


@pytest.mark.asyncio
async def test_balance_targets_other_user_when_given(migrated_db):
    from utils import db
    other_id = TEST_DISCORDID + 1
    await db.perform_one("DELETE FROM users WHERE discordid = %s;", (other_id,))
    try:
        await db.perform_one(
            "INSERT INTO users (discordid, points) VALUES (%s, %s);",
            (other_id, 7),
        )
        requester = FakeUser(TEST_DISCORDID, "caviar")
        target = FakeUser(other_id, "benjamin")
        ctx = FakeApplicationContext(author=requester)

        await points.Points.balance.callback(object(), ctx, target)

        embed = ctx.followup.send_calls[0]["embed"]
        assert embed.title == "benjamin's points"
        assert embed.description == "7 points"
    finally:
        await db.perform_one("DELETE FROM users WHERE discordid = %s;", (other_id,))


# --- PredictionView ---

class FakeBettor:
    def __init__(self, id):
        self.id = id
        self.mention = f"<@{id}>"


@pytest_asyncio.fixture
async def prediction(monkeypatch):
    """A live prediction with its db writes recorded rather than executed."""
    import discord
    from tests.conftest import FakeMessage

    calls = []
    rowcount = {"value": 1}

    async def perform_one(sql, parameters=None):
        calls.append((sql, parameters))
        return rowcount["value"]

    monkeypatch.setattr(points.db, "perform_one", perform_one)

    view = points.PredictionView("Purple", "Gold", discord.Embed(title="who wins?"))
    view.message = FakeMessage()
    view.perform_one_calls = calls
    view.set_rowcount = lambda n: rowcount.__setitem__("value", n)
    return view


@pytest.mark.asyncio
async def test_prediction_deducts_the_stake_behind_a_balance_guard(prediction):
    await prediction.modal_callback(FakeBettor(7), 100, "Purple")

    sql, params = prediction.perform_one_calls[0]
    assert "points >= %s" in sql
    assert params == (100, 7, 100)
    assert prediction.option_a_points == {7: 100}


@pytest.mark.asyncio
async def test_prediction_rejects_a_bet_the_balance_cannot_cover(prediction):
    """The modal's balance check is a stale snapshot, so this guard is the real one.
    Recording the bet anyway would credit points that were never deducted."""
    prediction.set_rowcount(0)

    await prediction.modal_callback(FakeBettor(7), 100, "Purple")

    assert "tried to bet more points than they have" in prediction.message.replies[0]
    assert prediction.option_a_points == {}
    assert prediction.message.edit_calls == []


@pytest.mark.asyncio
async def test_prediction_accumulates_a_raised_bet(prediction):
    await prediction.modal_callback(FakeBettor(7), 100, "Purple")
    await prediction.modal_callback(FakeBettor(7), 50, "Purple")

    assert prediction.option_a_points == {7: 150}
    assert "(up from 100)" in prediction.message.replies[1]


@pytest.mark.asyncio
async def test_prediction_tracks_each_side_separately(prediction):
    await prediction.modal_callback(FakeBettor(7), 100, "Purple")
    await prediction.modal_callback(FakeBettor(8), 300, "Gold")

    assert prediction.option_a_points == {7: 100}
    assert prediction.option_b_points == {8: 300}


@pytest.mark.asyncio
async def test_prediction_odds_come_from_the_shared_payout_formula(prediction):
    await prediction.modal_callback(FakeBettor(7), 100, "Purple")
    await prediction.modal_callback(FakeBettor(8), 300, "Gold")

    prediction.update_embed()

    assert prediction.odds_a == 4.0            # 1 + 300/100
    assert prediction.odds_b == 1 + 100 / 300


@pytest.mark.asyncio
async def test_prediction_odds_are_break_even_while_one_side_is_empty(prediction):
    """Guards the division that the old inline formula handled with an if/else."""
    await prediction.modal_callback(FakeBettor(7), 100, "Purple")

    prediction.update_embed()

    assert prediction.odds_a == 1.0
    assert prediction.odds_b == 1.0


@pytest.mark.asyncio
async def test_prediction_refuses_a_stake_submitted_after_the_lock(prediction):
    """Disabling the buttons doesn't close a modal already open on someone's client,
    and a late stake would recompute the odds the payout was announced with."""
    await prediction.modal_callback(FakeBettor(7), 100, "Purple")
    await prediction.modal_callback(FakeBettor(8), 300, "Gold")
    prediction.update_embed()
    odds_before = prediction.odds_a
    prediction.locked = True

    await prediction.modal_callback(FakeBettor(9), 5000, "Purple")

    assert 9 not in prediction.option_a_points
    assert len(prediction.perform_one_calls) == 2   # no third deduction
    prediction.update_embed()
    assert prediction.odds_a == odds_before
    assert "locked prediction" in prediction.message.replies[-1]


@pytest.mark.asyncio
async def test_prediction_refunds_a_stake_the_lock_beat_to_the_punch(prediction):
    """The lock can land while the deduction is in flight, and complete_prediction pays
    out off these dicts -- a stake booked afterwards is deducted and never seen again."""
    original = points.db.perform_one

    async def lock_mid_deduction(sql, parameters=None):
        if "points >= %s" in sql:
            prediction.locked = True
        return await original(sql, parameters)

    points.db.perform_one = lock_mid_deduction
    try:
        await prediction.modal_callback(FakeBettor(7), 100, "Purple")
    finally:
        points.db.perform_one = original

    assert prediction.option_a_points == {}
    assert "locked prediction" in prediction.message.replies[0]
    credit_sql, credit_params = prediction.perform_one_calls[1]
    assert "points + %s" in credit_sql
    assert credit_params == (100, 7)


@pytest.mark.asyncio
async def test_balance_reads_a_null_balance_as_zero(migrated_db):
    """users.points is nullable, and the comma format spec raises TypeError on None
    rather than printing it, so the zero has to land before the value reaches the embed."""
    from utils import db
    await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
    try:
        await db.perform_one(
            "INSERT INTO users (discordid, points) VALUES (%s, NULL);",
            (TEST_DISCORDID,),
        )
        user = FakeUser(TEST_DISCORDID, "caviar")
        ctx = FakeApplicationContext(author=user)

        await points.Points.balance.callback(object(), ctx, None)

        embed = ctx.followup.send_calls[0]["embed"]
        assert embed.description == "0 points"
    finally:
        await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))


@pytest.mark.asyncio
async def test_balance_groups_a_large_balance_with_commas(migrated_db):
    from utils import db
    await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
    try:
        await db.perform_one(
            "INSERT INTO users (discordid, points) VALUES (%s, %s);",
            (TEST_DISCORDID, 12480),
        )
        user = FakeUser(TEST_DISCORDID, "caviar")
        ctx = FakeApplicationContext(author=user)

        await points.Points.balance.callback(object(), ctx, None)

        embed = ctx.followup.send_calls[0]["embed"]
        assert embed.description == "12,480 points"
    finally:
        await db.perform_one("DELETE FROM users WHERE discordid = %s;", (TEST_DISCORDID,))
