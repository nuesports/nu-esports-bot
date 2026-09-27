"""Writes to config.yaml from Discord: comment-preserving, validated, then applied live."""

import asyncio
import copy
import difflib
import io
import os
from collections.abc import Callable
from typing import Any, NamedTuple

import discord
import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from utils import config, config_schema

# one edit at a time, or two quick ones would each save over the other
_lock = asyncio.Lock()


class EditRefused(Exception):
    """An edit that was checked and turned down, with a reason fit to show the user."""


class Saved(NamedTuple):
    old: Any
    new: Any
    snippet: str
    before_text: str
    after_text: str


class Reloaded(NamedTuple):
    problems: list[str]
    text: str


# returned by a change to drop the key entirely, how undo takes back a new roster
REMOVE = object()


# discord's ansi code blocks keep yaml's indentation and #/- lines intact
BOLD, RED, RESET = "\x1b[1m", "\x1b[31m", "\x1b[0m"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _sections(lines: list[str]) -> list[int]:
    """Which top-level key each line falls under, as that key's line number."""
    owners, current = [], -1
    for number, line in enumerate(lines):
        if line.strip() and not _indent(line) and not line.startswith("#"):
            current = number
        owners.append(current)
    return owners


def _context(lines: list[str], start: int, stop: int, limit: int) -> tuple[list, list]:
    """Up to limit lines either side of lines[start:stop] that belong with them:
    same section, same block, with the parent key shown once on the way up."""
    depth = min(
        (_indent(line) for line in lines[start:stop] if line.strip()), default=0
    )
    section = _sections(lines)
    above: list[str] = []
    for number in range(start - 1, -1, -1):
        line = lines[number]
        if len(above) == limit or not line.strip() or section[number] != section[start]:
            break
        if _indent(line) < depth:
            # the parent key, not a stray comment sitting further out
            if not line.lstrip().startswith("#"):
                above.insert(0, line)
            break
        above.insert(0, line)
    below: list[str] = []
    for number in range(stop, len(lines)):
        line = lines[number]
        if len(below) == limit or not line.strip() or section[number] != section[start]:
            break
        if _indent(line) < depth:
            break
        below.append(line)
    return above, below


def snippet(old_text: str, new_text: str, context: int = 2) -> str:
    """The changed lines with what belongs around them, new lines bold, removals red."""
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
    parts = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        # a pure removal has no new line to point at, so show what went
        if tag == "delete":
            side, start, stop, style = old_lines, i1, i2, RED
        else:
            side, start, stop, style = new_lines, j1, j2, BOLD
        above, below = _context(side, start, stop, context)
        changed = [f"{style}{line}{RESET}" for line in side[start:stop]]
        parts.append("\n".join(above + changed + below))
    return "```ansi\n" + "\n...\n".join(parts) + "\n```"


def _round_trip(text: str) -> YAML:
    y = YAML()
    y.preserve_quotes = True
    # matches how config.yaml is laid out, so a save only changes the edited line
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    y.explicit_start = text.startswith("---")
    return y


def _parent(doc: CommentedMap, path: str) -> tuple[CommentedMap, str]:
    """The map holding path's last key, creating any missing maps along the way."""
    node = doc
    parts = path.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = CommentedMap()
            node[part] = child
        if not isinstance(child, CommentedMap):
            raise EditRefused(f"`{path}` runs through a value that isn't a section")
        node = child
    return node, parts[-1]


def _write(new_text: str, old_text: str) -> None:
    path = config.CONFIG_PATH
    # a one-step undo over ssh if an edit turns out wrong
    path.with_name(path.name + ".prev").write_text(
        old_text, encoding="utf-8", newline="\n"
    )
    # written aside then swapped in, so a crash mid-write can't leave half a file
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


async def edit(
    member: discord.Member, path: str, change: Callable[[Any], Any]
) -> Saved:
    """Apply change to path's current value and save it live."""
    if config_schema.find(path) is None:
        raise EditRefused(f"`{path}` isn't an editable setting")

    async with _lock:
        old_text = config.CONFIG_PATH.read_text(encoding="utf-8")
        y = _round_trip(old_text)
        try:
            doc = y.load(old_text)
        except YAMLError as error:
            # someone broke it over ssh -- fix it there, not on top of it
            raise EditRefused(f"the file on disk doesn't parse ({error})") from error
        parent, key = _parent(doc, path)
        value = change(parent.get(key))
        if value is REMOVE:
            parent.pop(key, None)
        else:
            parent[key] = value

        buffer = io.StringIO()
        y.dump(doc, buffer)
        new_text = buffer.getvalue()
        if new_text == old_text:
            raise EditRefused(f"`{path}` is already set to that")

        before = yaml.safe_load(old_text)
        after = yaml.safe_load(new_text)
        # a file already carrying a bad value shouldn't block every unrelated fix
        old_problems = config_schema.problems(before)
        introduced = [p for p in config_schema.problems(after) if p not in old_problems]
        if introduced:
            raise EditRefused("; ".join(introduced))
        if not config.is_bot_dev(member, after):
            raise EditRefused("that would take away your own access to /config")

        _write(new_text, old_text)
        config.replace_config(after)
    return Saved(
        config_schema.lookup(before, path),
        config_schema.lookup(after, path),
        snippet(old_text, new_text),
        old_text,
        new_text,
    )


async def revert(member: discord.Member, path: str, before_text: str) -> Saved:
    """Put path back how before_text had it, leaving every other setting alone."""
    node: Any = _round_trip(before_text).load(before_text)
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            node = REMOVE
            break
        node = node[part]
    # copied whole so a restored roster keeps its quotes and comments
    value = node if node is REMOVE else copy.deepcopy(node)
    return await edit(member, path, lambda _: value)


async def reload() -> Reloaded:
    """Pick up edits made on disk, returning any problems the file has."""
    async with _lock:
        try:
            text = config.CONFIG_PATH.read_text(encoding="utf-8")
            fresh = yaml.safe_load(text)
        except (OSError, yaml.YAMLError) as error:
            raise EditRefused(f"the file can't be read ({error})") from error
        if not isinstance(fresh, dict):
            raise EditRefused("the file isn't a set of settings")
        config.replace_config(fresh)
    return Reloaded(config_schema.problems(fresh), text)
