import asyncio

import pytest

from utils.game_apis import refresh


class FakeClient:
    def __init__(self, exc=None, delay=0.0):
        self.exc = exc
        self.delay = delay
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0

    async def fetch_and_store(self, discordid, account_row):
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.concurrent -= 1
        if self.exc:
            raise self.exc


def _always_stale(monkeypatch):
    async def _stale(discordid, game):
        return True
    monkeypatch.setattr(refresh, "_is_stale", _stale)


@pytest.mark.asyncio
async def test_fetch_with_lock_swallows_exceptions(monkeypatch, capsys):
    client = FakeClient(exc=ValueError("boom"))
    monkeypatch.setitem(refresh.CLIENTS, "fakegame", client)
    _always_stale(monkeypatch)

    await refresh._fetch_with_lock(123, "fakegame", (), force=False)  # must not raise

    assert client.calls == 1
    assert "refresh failed for 123/fakegame" in capsys.readouterr().out


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
