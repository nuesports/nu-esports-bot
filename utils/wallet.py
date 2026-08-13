from utils import db


def payout_multiplier(own_pot: float, opposing_pot: float) -> float:
    """Payout multiplier for a proportional pot split. Winners get their stake back
    plus a share of the opposing pot proportional to their own pot share.
    Returns 1.0 if own_pot is zero.
    """
    if own_pot <= 0:
        return 1.0
    return 1 + opposing_pot / own_pot


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
