import os
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

import discord

REPO_ROOT = Path(__file__).resolve().parent.parent
UNKNOWN = "unknown"


def _git(*args: str) -> str | None:
    """Output of a git command in the repo, or None if git can't answer."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        return None
    return output


def git_commit() -> str:
    """The running commit: GIT_COMMIT from the build if set, else asks git."""
    from_env = os.environ.get("GIT_COMMIT", "").strip()
    if from_env:
        return from_env[:7]
    return _git("rev-parse", "--short", "HEAD") or UNKNOWN


def commit_date() -> str | None:
    """When the running commit was made, or None if that isn't known."""
    from_env = os.environ.get("GIT_COMMIT_DATE", "").strip()
    if from_env:
        return from_env
    return _git("log", "-1", "--format=%cI")


def project_version() -> str:
    """The version in pyproject.toml."""
    try:
        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            return str(tomllib.load(f)["project"]["version"])
    except OSError, tomllib.TOMLDecodeError, KeyError:
        return UNKNOWN


def format_version(
    version: str, commit: str, date: str | None, started: datetime
) -> str:
    lines = [f"Version: {version}", f"Commit: {commit}"]
    if date:
        lines.append(f"Commit date: {date}")
    lines.append(f"Running since: {discord.utils.format_dt(started, 'R')}")
    return "\n".join(lines)
