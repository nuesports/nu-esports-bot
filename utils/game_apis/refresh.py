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