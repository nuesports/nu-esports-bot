import asyncio
from datetime import datetime, timedelta, timezone

from utils import config, db
from .league import LeagueClient

RANK_STALE_AFTER = timedelta(minutes=45)

CLIENTS = {
    "league": LeagueClient(),
}

_fetch_locks: dict[tuple[int, str], asyncio.Lock] = {}

ACCOUNT_COLUMNS = "game, external_id, display_name, region, provider_account_id, provider_secondary_id"

async def _is_stale(discordid: int, game:str) -> bool:
    if config.is_per_role_ranks(game):
        row = await db.fetch_one(
            "SELECT MIN(updated_at) FROM profile_role_ranks WHERE discordid = %s AND game = %s;",
            (discordid, game)
        )
    else:
        row = await db.fetch_one(
            "SELECT update_at FROM profile_rstats WHERE discordid = %s AND game = %s;",
            (discordid, game)
        )
    last_updated = row[0] if row else None
    return last_updated is None or (datetime.now(timezone.utc) - last_updated) > RANK_STALE_AFTER

async def _fetch_with_lock(discordid: int, game: str, account_row: tuple, force: bool) -> None:
    client = CLIENTS.get(game)
    if not client:
        return
    lock = _fetch_locks.setdefault((discordid, game), asyncio.Lock())
    async with lock:
        try:
            if not force and not await _is_stale(discordid, game):
                return # someone else refreshed
            await client.fetch_and_store(discordid, account_row)
        except Exception as e:
            print(f"[game_apis] refresh failed for {discordid}/{game}: {e}")
        finally:
            _fetch_locks.pop((discordid, game), None)

async def refresh_stale_ranks(discordid: int) -> None:
    """Called by `/profile view` before rendering. Refreshes all stale links,
    and swallows api errors (falls back to whatever's already on file)"""

    accounts = await db.fetch_all(
        f"SELECT {ACCOUNT_COLUMNS} FROM game_accounts WHERE discordid = %s;",
        (discordid,),
    )

    for row in accounts:
        game = row[0]
        if not game in CLIENTS or not await _is_stale(discordid, game):
            continue
        await _fetch_with_lock(discordid, game, row, force=False)

async def force_refresh(discordid: int, game: str) -> None:
    """Fetch regardless of staleness; called after linking"""
    row = await db.fetch_all(
        f"SELECT {ACCOUNT_COLUMNS} FROM game_accounts WHERE discordid = %s AND game = %s;",
        (discordid, game),
    )
    if row and game in CLIENTS:
        await _fetch_with_lock(discordid, game, row, force=True)