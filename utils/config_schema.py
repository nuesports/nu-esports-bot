"""Every config.yaml setting /config can touch, and what a valid value looks like.

Nothing outside this list is reachable from Discord, which is what keeps secrets.yaml
and the free-form sections (fun.special_users) out of it.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    ROLE = "role"
    CHANNEL = "channel"
    USER = "user"
    NUMBER = "number"
    BOOL = "bool"
    USER_LIST = "user list"
    EMAIL_MAP = "email roster"


@dataclass(frozen=True)
class Setting:
    # dotted path, "*" stands for any one key (a game, a sticker name)
    path: str
    kind: Kind
    description: str
    minimum: int | None = None
    maximum: int | None = None


SETTINGS: list[Setting] = [
    Setting("roles.staff_role", Kind.ROLE, "Pinged when something needs a human"),
    Setting("roles.bot_devs.role", Kind.ROLE, "Bot dev role"),
    Setting("roles.bot_devs.users", Kind.USER_LIST, "Bot devs without the role"),
    Setting("roles.gameroom_staff.role", Kind.ROLE, "Gameroom staff role"),
    Setting(
        "roles.gameroom_staff.users", Kind.USER_LIST, "Gameroom staff, pinged in turn"
    ),
    Setting("roles.leadership.role", Kind.ROLE, "Leadership role"),
    Setting("roles.stream_team.role", Kind.ROLE, "Stream team role"),
    Setting("roles.gameheads.*", Kind.ROLE, "Gamehead role for a game"),
    Setting("reservations.channel", Kind.CHANNEL, "Where reservations get posted"),
    Setting("gameheads.*", Kind.EMAIL_MAP, "Username to email, for reservations"),
    Setting("github_backlog.pr_channel", Kind.CHANNEL, "Where PRs get pinned"),
    Setting("github_backlog.issue_channel", Kind.CHANNEL, "Where issues get pinned"),
    Setting("antiscam.alert_channel", Kind.CHANNEL, "Where flagged scams get posted"),
    Setting("antiscam.timeout_days", Kind.NUMBER, "Scam hold length", 1, 28),
    Setting(
        "antiscam.ban_delete_message_days", Kind.NUMBER, "History a ban purges", 0, 7
    ),
    Setting(
        "antiscam.purge_window_minutes",
        Kind.NUMBER,
        "How far back a flag sweeps",
        0,
        1440,
    ),
    Setting("antiscam.exempt_staff", Kind.BOOL, "Never hold leadership and bot devs"),
    Setting("config_log.channel", Kind.CHANNEL, "Where /config changes get posted"),
    Setting("fun.hannah", Kind.USER, "hannah"),
    Setting("fun.hannah-haters", Kind.ROLE, "hannah haters"),
    Setting("fun.stickers.*", Kind.NUMBER, "Sticker id"),
    Setting("fun.chess_emojis.*", Kind.NUMBER, "Chess emoji id"),
]

# a wildcard can name a new key, so it has to look like the ones already there
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def find(path: str) -> Setting | None:
    """The setting a concrete path falls under, or None if it isn't editable."""
    parts = path.split(".")
    for setting in SETTINGS:
        pattern = setting.path.split(".")
        if len(pattern) != len(parts):
            continue
        if all(
            want == got or (want == "*" and KEY_PATTERN.match(got))
            for want, got in zip(pattern, parts, strict=True)
        ):
            return setting
    return None


def concrete_paths(data: dict, setting: Setting) -> list[str]:
    """Expand a setting's wildcard against what the config actually holds."""
    found = [""]
    node_sets: list[Any] = [data]
    for part in setting.path.split("."):
        next_found, next_nodes = [], []
        for prefix, node in zip(found, node_sets, strict=True):
            if not isinstance(node, dict):
                continue
            keys = [k for k in node if isinstance(k, str)] if part == "*" else [part]
            for key in keys:
                if key in node:
                    next_found.append(f"{prefix}.{key}" if prefix else key)
                    next_nodes.append(node[key])
        found, node_sets = next_found, next_nodes
    return found


def lookup(data: dict, path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _is_id(value: Any) -> bool:
    # bool is an int subclass, and `true` is never a snowflake
    return isinstance(value, int) and not isinstance(value, bool)


def check(setting: Setting, value: Any) -> str | None:
    """Why value doesn't fit setting, or None if it does. Unset (None) always fits."""
    if value is None:
        return None
    kind = setting.kind
    if kind in (Kind.CHANNEL, Kind.USER) and not _is_id(value):
        return "must be an id"
    if kind == Kind.ROLE and not (
        _is_id(value) or (isinstance(value, list) and all(_is_id(v) for v in value))
    ):
        return "must be a role id or a list of them"
    if kind == Kind.NUMBER:
        if not _is_id(value):
            return "must be a whole number"
        if setting.minimum is not None and value < setting.minimum:
            return f"must be at least {setting.minimum}"
        if setting.maximum is not None and value > setting.maximum:
            return f"must be at most {setting.maximum}"
    if kind == Kind.BOOL and not isinstance(value, bool):
        return "must be true or false"
    if kind == Kind.USER_LIST and not (
        isinstance(value, list) and all(_is_id(v) for v in value)
    ):
        return "must be a list of user ids"
    if kind == Kind.EMAIL_MAP:
        if not isinstance(value, dict):
            return "must be username: email pairs"
        for name, email in value.items():
            if not isinstance(email, str) or "@" not in email:
                return f"{name} needs an email"
    return None


def problems(data: dict) -> list[str]:
    """Every registered value in data that doesn't fit its setting."""
    found = []
    for setting in SETTINGS:
        for path in concrete_paths(data, setting):
            reason = check(setting, lookup(data, path))
            if reason:
                found.append(f"{path} {reason}")
    return found
