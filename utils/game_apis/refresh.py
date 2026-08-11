import asyncio
from datetime import datetime, timedelta, timezone

from utils import config, db
from .league import LeagueClient
from .overwatch import OverwatchClient
from .deadlock import DeadlockClient
from .valorant import ValorantClient

RANK_STALE_AFTER = timedelta(minutes=45)

CLIENTS = {
    "league": LeagueClient(),
    "overwatch": OverwatchClient(),
    "deadlock": DeadlockClient(),
    "valorant": ValorantClient(),
}

class _FetchLock:
    """Per-(discordid, game) lock plus a waiter count. The dict entry is only removed
    once the last waiter finishes: removing it while a blocked caller still holds the
    lock would let a concurrent setdefault() create a fresh, already-unlocked lock and
    run fetch_and_store in parallel -- defeating the dedup this lock exists for."""
    __slots__ = ("lock", "waiters")

    def __init__(self):
        self.lock = asyncio.Lock()
        self.waiters = 0


_fetch_locks: dict[tuple[int, str], _FetchLock] = {}

ACCOUNT_COLUMNS = "game, external_id, display_name, region, provider_account_id, provider_secondary_id"

async def _is_stale(discordid: int, game:str) -> bool:
    if config.is_per_role_ranks(game):
        row = await db.fetch_one(
            "SELECT MIN(updated_at) FROM profile_role_ranks WHERE discordid = %s AND game = %s;",
            (discordid, game)
        )
    else:
        row = await db.fetch_one(
            "SELECT updated_at FROM profile_stats WHERE discordid = %s AND game = %s;",
            (discordid, game)
        )
    last_updated = row[0] if row else None
    return last_updated is None or (datetime.now(timezone.utc) - last_updated) > RANK_STALE_AFTER

async def _fetch_with_lock(discordid: int, game: str, account_row: tuple, force: bool) -> None:
    client = CLIENTS.get(game)
    if not client:
        return
    entry = _fetch_locks.setdefault((discordid, game), _FetchLock())
    entry.waiters += 1
    try:
        async with entry.lock:
            try:
                if not force and not await _is_stale(discordid, game):
                    return # someone else refreshed
                await client.fetch_and_store(discordid, account_row)
            except Exception as e:
                print(f"[game_apis] refresh failed for {discordid}/{game}: {e}")
    finally:
        entry.waiters -= 1
        if entry.waiters == 0:
            _fetch_locks.pop((discordid, game), None)

async def refresh_stale_ranks(discordid: int) -> None:
    """Called by `/profile view` before rendering. Refreshes all stale links,
    and swallows api errors (falls back to whatever's already on file)

    Staleness checks stay sequential (cheap local DB reads), but the actual
    per-game fetches run concurrently -- otherwise a single slow/down upstream
    API serializes and stalls every other linked game behind it too."""

    accounts = await db.fetch_all(
        f"SELECT {ACCOUNT_COLUMNS} FROM game_accounts WHERE discordid = %s;",
        (discordid,),
    )

    to_refresh = []
    for row in accounts:
        game = row[0]
        if game in CLIENTS and await _is_stale(discordid, game):
            to_refresh.append((game, row))

    if to_refresh:
        await asyncio.gather(*(_fetch_with_lock(discordid, game, row, force=False) for game, row in to_refresh))

async def force_refresh(discordid: int, game: str) -> None:
    """Fetch regardless of staleness; called after linking"""
    row = await db.fetch_one(
        f"SELECT {ACCOUNT_COLUMNS} FROM game_accounts WHERE discordid = %s AND game = %s;",
        (discordid, game),
    )
    if row and game in CLIENTS:
        await _fetch_with_lock(discordid, game, row, force=True)