from utils import config


def get_tiers(game: str) -> list[str]:
    """Returns the ordered list of rank tiers for a game, lowest to highest."""
    return config.game_data[game]["tiers"]

def get_divisions(game: str) -> int:
    """Return how many divisions each tier has for a game (e.g. 4 for League)."""
    return config.game_data[game]["divisions"]

def tier_has_divisions(game: str, tier: str) -> bool:
    """Return whether a given tier is divided (e.g. "Gold 3") rather than flat (e.g. "Challenger")."""
    return tier not in config.game_data[game]["no_division_tiers"]

def compute_rank_value(game: str, tier: str, division: int) -> int:
    """Convert a tier+division into a single comprable integer."""
    index = get_tiers(game).index(tier)
    divisions = get_divisions(game)
    if tier_has_divisions(game, tier):
        return index * divisions + (division - 1)
    else:
        return index * divisions

def format_rank_label(game: str, tier: str, division: int) -> str:
    """Format a tier+division as a human-readable string, e.g. "Gold 3" or "Challenger"."""
    return f"{tier} {division}" if tier_has_divisions(game, tier) else tier

def validate_tier_division(game: str, tier: str | None, division: str) -> tuple[int, None] | tuple[None, str]:
    """Validate a tier+division pair. Returns (division_int, None) on success, or
    (None, error_message) to show the user on failure."""
    if tier is None or tier not in get_tiers(game):
        return None, "Invalid tier. Please select from dropdown."
    try:
        division_int = int(division)
    except ValueError:
        return None, "Invalid division. Please select from dropdown."
    if division_int > get_divisions(game):
        return None, "Invalid division. Please select from dropdown."
    return division_int, None
