import discord
import pytest

from utils import statuses


def test_load_statuses_builds_activities_from_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "statuses.yaml").write_text(
        "statuses:\n"
        "  - type: playing\n"
        '    name: "chess"\n'
        "  - type: watching\n"
        '    name: "the leaderboard"\n'
    )

    result = statuses.load_statuses()

    assert len(result) == 2
    assert result[0].type == discord.ActivityType.playing
    assert result[0].name == "chess"
    assert result[1].type == discord.ActivityType.watching
    assert result[1].name == "the leaderboard"


def test_load_statuses_raises_if_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        statuses.load_statuses()
