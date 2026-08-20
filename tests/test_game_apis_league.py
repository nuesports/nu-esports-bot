import pytest

from utils.game_apis import league
from utils.game_apis.base import GameAPIError


class FakeDB:
    def __init__(self):
        self.perform_one_calls = []

    async def perform_one(self, sql, params=None):
        self.perform_one_calls.append(params)


ACCOUNT_ROW = (None, None, None, None, "puuid-abc")  # index 4 is puuid, per ACCOUNT_COLUMNS ordering


@pytest.fixture(autouse=True)
def stub_api_key(monkeypatch):
    monkeypatch.setattr(league, "_get_riot_api_key", lambda: "dummy-key")


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(league, "db", fake)
    return fake


@pytest.fixture
def skip_mains_seed(monkeypatch):
    async def _has_mains(discordid, game):
        return True
    monkeypatch.setattr(league, "has_profile_mains", _has_mains)


@pytest.fixture
def stub_rank_formatting(monkeypatch):
    monkeypatch.setattr(league, "compute_rank_value", lambda game, tier, division: (tier, division))
    monkeypatch.setattr(league, "format_rank_label", lambda game, tier, division: f"{tier} {division}")


def _entries_client(monkeypatch, entries):
    async def fake_fetch(url, headers=None, params=None):
        return entries
    monkeypatch.setattr(league, "fetch_json_with_retries", fake_fetch)


@pytest.mark.asyncio
async def test_fetch_and_store_raises_on_unrecognized_rank(monkeypatch, skip_mains_seed):
    _entries_client(monkeypatch, [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "V"}])
    client = league.LeagueClient()
    with pytest.raises(GameAPIError, match="Unrecognized League division"):
        await client.fetch_and_store(123, ACCOUNT_ROW)


@pytest.mark.asyncio
async def test_fetch_and_store_flat_tier_without_rank_field(fake_db, skip_mains_seed, monkeypatch):
    # Master/Grandmaster/Challenger have no division; Riot omits the `rank` field for them.
    _entries_client(monkeypatch, [{"queueType": "RANKED_SOLO_5x5", "tier": "MASTER"}])
    client = league.LeagueClient()
    await client.fetch_and_store(123, ACCOUNT_ROW)  # must not raise

    # Real compute/format run here: Master is tier index 7, flat tiers ignore division,
    # so rank_value = 7 * 4 = 28 and the label is just "Master".
    assert fake_db.perform_one_calls == [(123, "league", 28, "Master")]


@pytest.mark.asyncio
async def test_fetch_and_store_maps_roman_division_correctly(
    monkeypatch, fake_db, skip_mains_seed, stub_rank_formatting
):
    _entries_client(monkeypatch, [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "III"}])
    client = league.LeagueClient()
    await client.fetch_and_store(123, ACCOUNT_ROW)

    assert fake_db.perform_one_calls == [(123, "league", ("Gold", 3), "Gold 3")]


@pytest.mark.asyncio
async def test_fetch_and_store_skips_insert_when_no_solo_queue_entry(monkeypatch, fake_db, skip_mains_seed):
    _entries_client(monkeypatch, [{"queueType": "RANKED_FLEX_SR", "tier": "GOLD", "rank": "III"}])
    client = league.LeagueClient()
    await client.fetch_and_store(123, ACCOUNT_ROW)

    assert fake_db.perform_one_calls == []
