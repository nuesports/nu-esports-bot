from utils import db


def payout_multiplier(own_pot: float, opposing_pot: float) -> float:
    """Payout multiplier for a proportional pot split. Winners get their stake back
    plus a share of the opposing pot proportional to their own pot share.
    Returns 1.0 if own_pot is zero.
    """
    if own_pot <= 0:
        return 1.0
    return 1 + opposing_pot / own_pot


def distribute_payouts(winning_bets: dict[int, int], losing_pot: int) -> dict[int, int]:
    """Split the whole pot across the winners in whole points that still sum to it.

    Rounding each stake on its own leaks: three 100-point winners against a 100-point
    losing pot each round to 133, paying out 399 of a 400-point pot, so every settlement
    quietly mints or burns a point or two. Floor each share instead, then hand the
    leftover out one point at a time, largest fractional part first.
    """
    winning_pot = sum(winning_bets.values())
    multiplier = payout_multiplier(winning_pot, losing_pot)
    exact = {uid: stake * multiplier for uid, stake in winning_bets.items()}
    payouts = {uid: int(value) for uid, value in exact.items()}
    # Ties keep insertion order, so the same book always splits the same way.
    by_fraction = sorted(
        payouts, key=lambda uid: exact[uid] - payouts[uid], reverse=True
    )
    for uid in by_fraction[: winning_pot + losing_pot - sum(payouts.values())]:
        payouts[uid] += 1
    return payouts


async def credit(discordid: int, amount: int) -> None:
    """Add points to one user. Used for refunds and payouts, which are never conditional."""
    await db.perform_one(
        "UPDATE users SET points = points + %s WHERE discordid = %s;",
        (amount, discordid),
    )


async def credit_many(rows: list[tuple[int, int]]) -> None:
    """Add points to many users at once. Rows are (amount, discordid), matching the
    parameter order of the statement rather than the reading order."""
    if not rows:
        return
    await db.perform_many(
        "UPDATE users SET points = points + %s WHERE discordid = %s;",
        rows,
    )


async def try_deduct(discordid: int, amount: int) -> bool:
    """Take points off a user, but only if they actually have them. Returns whether it
    applied.

    The WHERE guard is what makes this safe, not the caller. A read-then-write can't be
    trusted here: the same user can be betting in another lobby or a prediction at the
    same time, and those run outside any lock this process holds. Postgres evaluating
    the balance and the deduction in one statement is the only thing stopping an
    overdraft.
    """
    applied = await db.perform_one(
        "UPDATE users SET points = points - %s WHERE discordid = %s AND points >= %s;",
        (amount, discordid, amount),
    )
    return bool(applied)
