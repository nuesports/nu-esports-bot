import discord
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

def _role_ids(value) -> set[int]:
    """Normalize a roles config value (int, list, or None) to a set of role IDs."""
    if value is None:
        return set()
    if isinstance(value, int):
        return {value}
    return set(value)

def _in_role_group(member: discord.Member, group: dict) -> bool:
    """True for admins, or if member is in the group's explicit user list or holds its role(s)."""
    if member.guild_permissions.administrator:
        return True
    if member.id in (group.get("users") or []):
        return True
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & _role_ids(group.get("role")))

def is_bot_dev(member: discord.Member) -> bool:
    return _in_role_group(member, config["roles"]["bot_devs"])

def is_gameroom_staff(member: discord.Member) -> bool:
    return _in_role_group(member, config["roles"]["gameroom_staff"])

def has_leadership(member: discord.Member) -> bool:
    return _in_role_group(member, config["roles"]["leadership"])

def is_stream_team(member: discord.Member) -> bool:
    return _in_role_group(member, config["roles"]["stream_team"])

def can_reserve(member: discord.Member) -> bool:
    """Who can invoke reservation commands: bot devs, gameroom staff, leadership, or gameheads."""
    return is_bot_dev(member) or is_gameroom_staff(member) or has_leadership(member) or is_game_head(member)

def is_game_head(member: discord.Member) -> bool:
    """True for admins or anyone holding any per-game gamehead role."""
    if member.guild_permissions.administrator:
        return True
    member_role_ids = {r.id for r in member.roles}
    gamehead_role_ids = {r for r in config["roles"]["gameheads"].values() if r}
    return bool(member_role_ids & gamehead_role_ids)

def gamehead_email(username: str) -> str | None:
    """Look up a gamehead's email by Discord username across every game's roster."""
    for roster in config["gameheads"].values():
        if roster and username in roster:
            return roster[username]
    return None

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