import yaml
from pathlib import Path


def load_config():
    """Load config from config.yaml file."""
    config_file = Path("config.yaml")
    if not config_file.exists():
        raise FileNotFoundError("config.yaml not found in local directory")
    with open(config_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_secrets():
    """Load secrets from secrets.yaml file."""
    secrets_file = Path("secrets.yaml")
    if not secrets_file.exists():
        raise FileNotFoundError("secrets.yaml not found in local directory")
    with open(secrets_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_game_data():
    """Load game data from data/games/*.yaml file."""
    game_data = {}
    for path in Path("data/games").glob("*.yaml"):
        with open(path, "r", encoding="utf-8") as f:
            game_data[path.stem] = yaml.safe_load(f)
    if not game_data:
        raise FileNotFoundError("data/game/<game>.yaml not found in local directory")
    return game_data

def load_gameroom_data():
    gameroom_file = Path("data/gameroom.yaml")
    if not gameroom_file.exists():
        raise FileNotFoundError("data/gameroom.yaml not found in local directory")
    with open(gameroom_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
    
def load_matchmaking_data():
    matchmaking_file = Path("data/matchmaking.yaml")
    if not matchmaking_file.exists():
        raise FileNotFoundError("data/matchmaking.yaml not found in local directory")
    with open(matchmaking_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        data.setdefault("team_names", [])
        return data

config = load_config()
secrets = load_secrets()
game_data = load_game_data()
gameroom_data = load_gameroom_data()
matchmaking_data = load_matchmaking_data()

def is_per_role_ranks(game: str) -> bool:
    """Whether a game tracks rank (and elo) separately per role instead of once per game."""
    return bool(game_data[game].get("per_role_ranks"))

def rankable_roles(game: str) -> list[str]:
    """Roles a player can set a rank for in a per-role-ranks game (role_requirements keys, excludes Flex)."""
    return list(game_data[game].get("role_requirements") or {})

def role_icon(game: str, role: str) -> str:
    """Emoji for a role, shown next to a player's name on a mixed per-role leaderboard entry."""
    return game_data[game].get("role_icons", {}).get(role, "")

def main_aliases(game: str) -> dict[str, str]:
    """Alternate spellings accepted for /profile set main (e.g. old names after a rename),
    mapped to the canonical entry in this game's characters list."""
    return game_data[game].get("aliases", {})