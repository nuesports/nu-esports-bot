import asyncio

import psycopg
import pytest

from utils.game_apis import refresh
from utils.game_apis.base import GameAPIError, LinkError


class FakeClient:
    def __init__(self, exc=None, delay=0.0, gates=None):
        self.exc = exc
        self.delay = delay
        self.gates = list(gates or [])
        self.entered = asyncio.Event()  # set each time a fetch begins
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0

    async def fetch_and_store(self, discordid, account_row):
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.entered.set()
        if self.gates:
            gate = self.gates.pop(0)
            await gate.wait()  # hold the fetch until the test releases its gate
        elif self.delay:
            await asyncio.sleep(self.delay)
        self.concurrent -= 1
        if self.exc:
            raise self.exc


def _always_stale(monkeypatch):
    async def _stale(discordid, game):
        return True
    monkeypatch.setattr(refresh, "_is_stale", _stale)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    GameAPIError("provider fell over"),
    LinkError("API key not configured"),
    psycopg.OperationalError("database is gone"),
])
async def test_fetch_with_lock_swallows_what_fetch_and_store_can_fail_at(monkeypatch, capsys, exc):
    """The three things that legitimately go wrong on a refresh: the provider, the
    API key, and the write. Any of them skips one player rather than reaching
    /profile view, which calls this through asyncio.gather with no handler of its own."""
    client = FakeClient(exc=exc)
    monkeypatch.setitem(refresh.CLIENTS, "fakegame", client)
    _always_stale(monkeypatch)

    await refresh._fetch_with_lock(123, "fakegame", (), force=False)  # must not raise

    assert client.calls == 1
    assert "refresh failed for 123/fakegame" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_fetch_with_lock_lets_an_unexpected_error_through(monkeypatch):
    """Deliberate: the catch names what the API and the DB can do, so a bug in our own
    code surfaces instead of being logged and lost like everything used to be."""
    client = FakeClient(exc=AttributeError("someone typoed a field name"))
    monkeypatch.setitem(refresh.CLIENTS, "fakegame", client)
    _always_stale(monkeypatch)

    with pytest.raises(AttributeError):
        await refresh._fetch_with_lock(123, "fakegame", (), force=False)


@pytest.mark.asyncio
async def test_fetch_with_lock_serializes_concurrent_calls_for_same_key(monkeypatch):
    client = FakeClient(delay=0.02)
    monkeypatch.setitem(refresh.CLIENTS, "fakegame", client)
    _always_stale(monkeypatch)

    await asyncio.gather(
        refresh._fetch_with_lock(123, "fakegame", (), force=False),
        refresh._fetch_with_lock(123, "fakegame", (), force=False),
    )

    assert client.max_concurrent == 1  # never ran in parallel for the same key


@pytest.mark.asyncio
async def test_fetch_with_lock_removes_lock_entry_after_completion(monkeypatch):
    client = FakeClient()
    monkeypatch.setitem(refresh.CLIENTS, "fakegame", client)
    _always_stale(monkeypatch)

    await refresh._fetch_with_lock(123, "fakegame", (), force=False)

    assert (123, "fakegame") not in refresh._fetch_locks


@pytest.mark.asyncio
async def test_fetch_with_lock_force_skips_staleness_check(monkeypatch):
    client = FakeClient()
    monkeypatch.setitem(refresh.CLIENTS, "fakegame", client)
    stale_calls = []

    async def spy_stale(discordid, game):
        stale_calls.append((discordid, game))
        return False  # not stale -- force should still run the fetch

    monkeypatch.setattr(refresh, "_is_stale", spy_stale)

    await refresh._fetch_with_lock(123, "fakegame", (), force=True)

    assert client.calls == 1
    assert stale_calls == []


@pytest.mark.asyncio
async def test_fetch_with_lock_returns_silently_for_unknown_game():
    await refresh._fetch_with_lock(123, "not-a-real-game", (), force=False)  # must not raise


@pytest.mark.asyncio
async def test_fetch_with_lock_third_caller_waits_after_lock_entry_popped(monkeypatch):
    """Regression: a third caller arriving after the first caller finished and popped its
    lock entry must still wait on the second caller's lock -- not get a fresh unlocked
    lock and run fetch_and_store in parallel with it."""
    gates = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
    client = FakeClient(gates=gates)
    monkeypatch.setitem(refresh.CLIENTS, "fakegame", client)
    _always_stale(monkeypatch)

    first = asyncio.create_task(refresh._fetch_with_lock(123, "fakegame", (), force=False))
    await client.entered.wait()      # first is mid-fetch, holds the lock
    second = asyncio.create_task(refresh._fetch_with_lock(123, "fakegame", (), force=False))

    client.entered.clear()           # clear before first finishes so we can catch second's signal
    gates[0].set()                   # let first finish; its finally pops the lock entry
    await first
    await client.entered.wait()      # second is now mid-fetch, holds the lock

    third = asyncio.create_task(refresh._fetch_with_lock(123, "fakegame", (), force=False))
    await asyncio.sleep(0.01)        # give third a chance to run if it can
    assert client.max_concurrent == 1  # third must wait, not run alongside second

    gates[1].set()
    gates[2].set()
    await asyncio.gather(second, third)
