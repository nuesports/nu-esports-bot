import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from utils import version


def _fail_if_called(*args, **kwargs):
    raise AssertionError("subprocess.run should not be called")


def test_git_commit_prefers_env_var(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "a" * 40)
    monkeypatch.setattr(version.subprocess, "run", _fail_if_called)

    assert version.git_commit() == "a" * 7


def test_git_commit_uses_git_when_env_unset(monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.setattr(
        version.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="abc1234\n"),
    )

    assert version.git_commit() == "abc1234"


def _raise(error):
    def run(*args, **kwargs):
        raise error

    return run


@pytest.mark.parametrize(
    "run",
    [
        _raise(FileNotFoundError("git")),
        _raise(subprocess.TimeoutExpired("git", 2)),
        lambda *a, **k: subprocess.CompletedProcess(a, 128, stdout=""),
    ],
    ids=["no-git", "timeout", "not-a-repo"],
)
def test_git_commit_unknown_when_git_unavailable(monkeypatch, run):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.setattr(version.subprocess, "run", run)

    assert version.git_commit() == "unknown"


def test_commit_date_prefers_env_var(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT_DATE", "2026-01-02T03:04:05+00:00")
    monkeypatch.setattr(version.subprocess, "run", _fail_if_called)

    assert version.commit_date() == "2026-01-02T03:04:05+00:00"


def test_commit_date_none_when_unavailable(monkeypatch):
    monkeypatch.delenv("GIT_COMMIT_DATE", raising=False)
    monkeypatch.setattr(version.subprocess, "run", _raise(FileNotFoundError("git")))

    assert version.commit_date() is None


def test_project_version_reads_pyproject(tmp_path, monkeypatch):
    with open(Path(version.__file__).parent.parent / "pyproject.toml", "rb") as f:
        expected = tomllib.load(f)["project"]["version"]
    assert version.project_version() == expected

    monkeypatch.setattr(version, "REPO_ROOT", tmp_path)
    assert version.project_version() == "unknown"


def test_format_version_includes_fields_and_skips_missing_date():
    started = datetime(2026, 1, 1, tzinfo=UTC)

    without_date = version.format_version("0.1.0", "abc1234", None, started)
    assert "0.1.0" in without_date
    assert "abc1234" in without_date
    assert "Commit date" not in without_date

    with_date = version.format_version("0.1.0", "abc1234", "2026-01-02", started)
    assert "2026-01-02" in with_date
