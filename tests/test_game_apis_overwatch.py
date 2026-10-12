import pytest

from utils.game_apis import overwatch
from utils.game_apis.base import GameAPIError, LinkError


def _client_returning(monkeypatch, payload):
    async def fake_fetch(url, headers=None, params=None):
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(overwatch, "fetch_json_with_retries", fake_fetch)
    return overwatch.OverwatchClient()


@pytest.mark.asyncio
async def test_link_reports_a_private_or_missing_profile(monkeypatch):
    client = _client_returning(monkeypatch, GameAPIError("not found", status=404))

    with pytest.raises(LinkError, match="Career Profile"):
        await client.link("Alex#1234")


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, "NoHashHere", ""])
async def test_an_unusable_stored_battletag_is_a_game_api_error(monkeypatch, stored):
    """A BattleTag is split on its '#' to build the OverFast url. A row that's NULL or
    lost its '#' used to escape as AttributeError/ValueError, which _fetch_with_lock
    doesn't catch -- so one bad row took /profile view down instead of skipping a game."""
    client = _client_returning(monkeypatch, {})

    with pytest.raises(GameAPIError):
        await client.fetch_and_store(123, ("overwatch", stored, None, None, None, None))


@pytest.mark.asyncio
async def test_a_well_formed_battletag_still_goes_through(monkeypatch):
    """No competitive block, so it returns before touching the database."""
    client = _client_returning(monkeypatch, {"competitive": None})

    await client.fetch_and_store(
        123, ("overwatch", "Alex#1234", None, None, None, None)
    )


@pytest.mark.asyncio
async def test_an_emerald_rank_is_stored_between_platinum_and_diamond(monkeypatch):
    client = _client_returning(
        monkeypatch,
        {
            "competitive": {
                "pc": {
                    "tank": {"division": "platinum", "tier": 1},
                    "damage": {"division": "emerald", "tier": 3},
                    "support": {"division": "diamond", "tier": 5},
                }
            }
        },
    )
    stored = []

    async def capture(sql, rows):
        stored.extend(rows)

    async def has_mains(*args):
        return True

    monkeypatch.setattr(overwatch.db, "perform_many", capture)
    monkeypatch.setattr(overwatch, "has_profile_mains", has_mains)

    await client.fetch_and_store(
        123, ("overwatch", "Alex#1234", None, None, None, None)
    )

    ranks = {role: (value, label) for _, _, role, value, label in stored}
    assert ranks["Damage"][1] == "Emerald 3"
    assert ranks["Tank"][0] < ranks["Damage"][0] < ranks["Support"][0]
