import pytest

from utils.game_apis import valorant
from utils.game_apis.base import GameAPIError, LinkError


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setattr(valorant, "_get_henrik_api_key", lambda: "dummy-key")


def _client_returning(monkeypatch, payload):
    async def fake_fetch(url, headers=None, params=None):
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(valorant, "fetch_json_with_retries", fake_fetch)
    return valorant.ValorantClient()


@pytest.mark.asyncio
async def test_link_rejects_an_identifier_without_a_tag(monkeypatch):
    client = _client_returning(monkeypatch, {})

    with pytest.raises(LinkError):
        await client.link("NoTagHere")


@pytest.mark.asyncio
async def test_link_reports_a_missing_account_as_a_link_error(monkeypatch):
    """A 404 still has to reach the user as "no account found" rather than as the
    generic API-is-broken message -- that's what GameAPIError.status is carrying."""
    client = _client_returning(monkeypatch, GameAPIError("not found", status=404))

    with pytest.raises(LinkError, match="No Riot account found"):
        await client.link("Name#Tag")


@pytest.mark.asyncio
async def test_link_reports_any_other_api_failure_as_troubles(monkeypatch):
    client = _client_returning(monkeypatch, GameAPIError("boom", status=503))

    with pytest.raises(LinkError, match="having troubles"):
        await client.link("Name#Tag")


@pytest.mark.asyncio
async def test_link_turns_a_reshaped_payload_into_a_game_api_error(monkeypatch):
    """HenrikDev dropping `region` used to escape as a bare KeyError that every caller
    had to have guessed at."""
    client = _client_returning(monkeypatch, {"data": {"puuid": "abc"}})  # no region

    with pytest.raises(GameAPIError):
        await client.link("Name#Tag")


@pytest.mark.asyncio
async def test_fetch_and_store_turns_a_null_rank_block_into_a_game_api_error(
    monkeypatch,
):
    """HenrikDev sends `current: null` for some unrated players, which used to escape
    as a TypeError out of the background refresh."""
    client = _client_returning(monkeypatch, {"data": {"current": None}})

    with pytest.raises(GameAPIError):
        await client.fetch_and_store(123, (None, None, None, "na", "puuid-abc"))
