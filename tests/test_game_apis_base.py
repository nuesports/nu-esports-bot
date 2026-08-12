import pytest

from utils.game_apis import base


class FakeDB:
    def __init__(self, fetch_one_result=None):
        self.fetch_one_result = fetch_one_result
        self.perform_many_calls = []

    async def fetch_one(self, sql, params=None):
        return self.fetch_one_result

    async def perform_many(self, sql, rows):
        self.perform_many_calls.append(rows)


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(base, "db", fake)
    return fake


@pytest.mark.asyncio
async def test_has_profile_mains_true_when_row_exists(fake_db):
    fake_db.fetch_one_result = (1,)
    assert await base.has_profile_mains(123, "league") is True


@pytest.mark.asyncio
async def test_has_profile_mains_false_when_no_row(fake_db):
    fake_db.fetch_one_result = None
    assert await base.has_profile_mains(123, "league") is False


@pytest.mark.asyncio
async def test_seed_mains_inserts_rows(fake_db):
    await base.seed_mains(123, "league", ["Ahri", "Yasuo"])
    assert fake_db.perform_many_calls == [[(123, "league", "Ahri"), (123, "league", "Yasuo")]]


@pytest.mark.asyncio
async def test_seed_mains_noop_on_empty_list(fake_db):
    await base.seed_mains(123, "league", [])
    assert fake_db.perform_many_calls == []


@pytest.mark.asyncio
async def test_has_profile_roles_false_when_no_row(fake_db):
    fake_db.fetch_one_result = None
    assert await base.has_profile_roles(123, "valorant") is False


@pytest.mark.asyncio
async def test_seed_roles_inserts_rows(fake_db):
    await base.seed_roles(123, "valorant", {"Duelist"})
    assert fake_db.perform_many_calls == [[(123, "valorant", "Duelist")]]


@pytest.mark.asyncio
async def test_seed_roles_noop_on_empty(fake_db):
    await base.seed_roles(123, "valorant", set())
    assert fake_db.perform_many_calls == []
