def payout_multiplier(own_pot: float, opposing_pot: float) -> float:
    """Payout multiplier for a proportional pot split. Winners get their stake back
    plus a share of the opposing pot proportional to their own pot share.
    Returns 1.0 if own_pot is zero.
    """
    if own_pot <= 0:
        return 1.0
    return 1 + opposing_pot / own_pot
