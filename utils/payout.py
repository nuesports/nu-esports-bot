def payout_multiplier(own_pot: float, opposing_pot: float) -> float:
    """Twitch-Predictions-style payout multiplier for a proportional pot split.

    Winners get their stake back plus a share of the opposing pot proportional to
    their stake's share of their own side's pot. 1.0 (break-even) if own_pot is
    empty/zero, since there's nothing to divide a share by.
    """
    if own_pot <= 0:
        return 1.0
    return 1 + opposing_pot / own_pot
