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
    assert fake_db.perform_many_calls == [
        [(123, "league", "Ahri"), (123, "league", "Yasuo")]
    ]


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


# --- readable_payload ---


@pytest.mark.parametrize(
    "exc",
    [
        KeyError("region"),
        IndexError("list index out of range"),
        TypeError("'NoneType' object is not subscriptable"),
        ValueError("not enough values to unpack"),
        AttributeError("'list' object has no attribute 'get'"),
    ],
)
def test_an_unreadable_payload_becomes_a_game_api_error(exc):
    """Every way a provider can change shape under us lands as one type, so callers
    don't have to enumerate five of them and still miss the sixth."""
    with (
        pytest.raises(base.GameAPIError) as exc_info,
        base.readable_payload("fakegame"),
    ):
        raise exc

    assert "fakegame" in str(exc_info.value)
    assert exc_info.value.__cause__ is exc  # original preserved for the traceback


def test_readable_payload_leaves_a_link_error_alone():
    """LinkError means the identifier was bad, which is a different answer to the user
    than "the API is broken" -- wrapping it would lose that."""
    with pytest.raises(base.LinkError), base.readable_payload("fakegame"):
        raise base.LinkError("Riot ID must be in the form Name#Tag")


def test_readable_payload_does_not_double_wrap_a_game_api_error():
    original = base.GameAPIError("already the right type", status=404)

    with (
        pytest.raises(base.GameAPIError) as exc_info,
        base.readable_payload("fakegame"),
    ):
        raise original

    assert exc_info.value is original
    assert (
        exc_info.value.status == 404
    )  # status survives, so the 404 branch still fires


def test_readable_payload_passes_a_clean_block_through():
    with base.readable_payload("fakegame"):
        value = {"data": {"region": "na"}}["data"]["region"]

    assert value == "na"
