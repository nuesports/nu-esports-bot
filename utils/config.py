import os
from pathlib import Path

import discord
import yaml

# prod points this into a writable mount, since /config saves over it
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.yaml"))


def load_config() -> dict:
    """Load config from config.yaml file."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"{CONFIG_PATH} not found")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config_or_fallback() -> tuple[dict, str | None]:
    """The config, or the last good one from the database if config.yaml won't load.
    Returns why it fell back, so the bot can say so once it's online."""
    try:
        data = load_config()
        if isinstance(data, dict):
            return data, None
        reason = "it isn't a set of settings"
    except (OSError, yaml.YAMLError) as error:
        reason = str(error)

    # imported here since config_history needs secrets, which load after this module
    from utils import config_history

    fallback = config_history.latest_text_before_startup()
    if fallback is None:
        raise RuntimeError(f"{CONFIG_PATH} won't load ({reason}) and none is saved")
    print(f"[config] {CONFIG_PATH} won't load ({reason}), using the last saved copy")
    # kept aside for whoever fixes it, then replaced so /config works again
    if CONFIG_PATH.exists():
        CONFIG_PATH.replace(CONFIG_PATH.with_name(CONFIG_PATH.name + ".broken"))
    CONFIG_PATH.write_text(fallback, encoding="utf-8", newline="\n")
    return yaml.safe_load(fallback), reason


def replace_config(new: dict) -> None:
    """Swap in a new config without a restart."""
    # in place, since every cog holds a reference to this same dict
    config.clear()
    config.update(new)


def load_secrets() -> dict:
    """Load secrets from secrets.yaml file."""
    secrets_file = Path("secrets.yaml")
    if not secrets_file.exists():
        raise FileNotFoundError("secrets.yaml not found in local directory")
    with open(secrets_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_game_data() -> dict:
    """Load game data from data/games/*.yaml file."""
    game_data = {}
    for path in Path("data/games").glob("*.yaml"):
        with open(path, "r", encoding="utf-8") as f:
            game_data[path.stem] = yaml.safe_load(f)
    if not game_data:
        raise FileNotFoundError("data/game/<game>.yaml not found in local directory")
    return game_data


def load_gameroom_data() -> dict:
    gameroom_file = Path("data/gameroom.yaml")
    if not gameroom_file.exists():
        raise FileNotFoundError("data/gameroom.yaml not found in local directory")
    with open(gameroom_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_matchmaking_data() -> dict:
    matchmaking_file = Path("data/matchmaking.yaml")
    if not matchmaking_file.exists():
        raise FileNotFoundError("data/matchmaking.yaml not found in local directory")
    with open(matchmaking_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        data.setdefault("team_names", [])
        return data


def load_fun_data() -> dict:
    fun_file = Path("data/fun.yaml")
    if not fun_file.exists():
        raise FileNotFoundError("data/fun.yaml not found in local directory")
    with open(fun_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_antiscam_data() -> dict:
    """Load the scam-detector scoring rules from data/antiscam.yaml."""
    antiscam_file = Path("data/antiscam.yaml")
    if not antiscam_file.exists():
        raise FileNotFoundError("data/antiscam.yaml not found in local directory")
    with open(antiscam_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


secrets = load_secrets()
# set when config.yaml wouldn't load and the last saved one was used instead
config, config_fallback_reason = load_config_or_fallback()
game_data = load_game_data()
gameroom_data = load_gameroom_data()
matchmaking_data = load_matchmaking_data()
fun_data = load_fun_data()
antiscam_data = load_antiscam_data()


def _role_ids(value: int | list[int] | None) -> set[int]:
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


def is_bot_dev(member: discord.Member, cfg: dict | None = None) -> bool:
    """cfg checks against a proposed config instead of the live one."""
    return _in_role_group(member, (config if cfg is None else cfg)["roles"]["bot_devs"])


def is_gameroom_staff(member: discord.Member) -> bool:
    return _in_role_group(member, config["roles"]["gameroom_staff"])


def has_leadership(member: discord.Member) -> bool:
    return _in_role_group(member, config["roles"]["leadership"])


def is_stream_team(member: discord.Member) -> bool:
    return _in_role_group(member, config["roles"]["stream_team"])


def staff_role_id() -> int | None:
    """The role pinged when something needs a human."""
    return config["roles"].get("staff_role")


def can_reserve(member: discord.Member) -> bool:
    """Who can invoke reservation commands: bot devs, gameroom staff, leadership, or gameheads."""
    return (
        is_bot_dev(member)
        or is_gameroom_staff(member)
        or has_leadership(member)
        or is_game_head(member)
    )


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
