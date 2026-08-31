import re

from utils import config, db
from utils.ranks import compute_rank_value, format_rank_label

from .base import (
    GameAPIError,
    LinkError,
    LinkResult,
    has_profile_mains,
    readable_payload,
    seed_mains,
)
from .http import fetch_json_with_retries

DEADLOCK_BASE_URL = "https://api.deadlock-api.com"
STEAM_API_BASE = "https://api.steampowered.com"
STEAMID64_OFFSET = 76561197960265728  # SteamID64 = account_id (SteamID3) + this, always

VANITY_URL_RE = re.compile(r"steamcommunity\.com/id/([^/]+)", re.IGNORECASE)
PROFILE_ID_RE = re.compile(r"steamcommunity\.com/profiles/(\d+)", re.IGNORECASE)


def _get_steam_api_key() -> str:
    apis = config.secrets.get("apis", {})
    key = apis.get("steam-api-key") if isinstance(apis, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise LinkError(
            "Steam API key not configured. Ask a bot dev to set `secrets.yaml -> apis.steam-api-key`"
        )
    return key.strip()


class DeadlockClient:
    game = "deadlock"

    async def _resolve_account_id(self, raw_identifier: str) -> tuple[int, str, str]:
        """Returns (account_id, personaname, profileurl) for whatever was typed --
        a SteamID64, a raw account_id (SteamID3), a pasted profile URL, or a name."""
        identifier = raw_identifier.strip()

        # pull a bare id/vanity slug out of a pasted profile URL, if that's what was given
        if (match := PROFILE_ID_RE.search(identifier)) or (
            match := VANITY_URL_RE.search(identifier)
        ):
            identifier = match.group(1)

        if identifier.isdigit():
            return await self._lookup_by_account_id(int(identifier))

        try:
            return await self._lookup_by_persona_search(identifier)
        except LinkError:
            return await self._lookup_by_vanity_url(
                identifier
            )  # deadlock-api doesn't know them -- try Steam directly

    async def _lookup_by_account_id(self, as_int: int) -> tuple[int, str, str]:
        account_id = as_int - STEAMID64_OFFSET if as_int >= STEAMID64_OFFSET else as_int
        try:
            profiles = await fetch_json_with_retries(
                f"{DEADLOCK_BASE_URL}/v1/players/steam",
                params={"account_ids": account_id},
            )
        except GameAPIError as e:
            if e.status == 404:
                raise LinkError("No Steam profile found for that ID.") from e
            raise LinkError(
                "deadlock-api's having troubles right now, try again soon"
            ) from e
        if not profiles:
            raise LinkError("No Steam profile found for that ID.")
        with readable_payload(self.game):
            profile = profiles[0]
            return profile["account_id"], profile["personaname"], profile["profileurl"]

    async def _lookup_by_persona_search(self, identifier: str) -> tuple[int, str, str]:
        try:
            results = await fetch_json_with_retries(
                f"{DEADLOCK_BASE_URL}/v1/players/steam-search",
                params={"search_query": identifier, "limit": 1},
            )
        except GameAPIError as e:
            if e.status == 404:
                raise LinkError(
                    f'No Steam profile found matching "{identifier}".'
                ) from e
            raise LinkError(
                "deadlock-api's having troubles right now, try again soon"
            ) from e
        if not results:
            raise LinkError(f'No Steam profile found matching "{identifier}".')
        with readable_payload(self.game):
            profile = results[0]
            return profile["account_id"], profile["personaname"], profile["profileurl"]

    async def _lookup_by_vanity_url(self, vanity: str) -> tuple[int, str, str]:
        key = _get_steam_api_key()
        resolved = await fetch_json_with_retries(
            f"{STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v0001/",
            params={"key": key, "vanityurl": vanity},
        )
        # LinkError and GameAPIError both pass through readable_payload untouched, so the
        # fetch and the not-found cases below keep their own meanings.
        with readable_payload(self.game):
            result = resolved.get("response", {})
            if result.get("success") != 1:
                raise LinkError(
                    f'No Steam account found for "{vanity}" -- double check the vanity URL or numeric SteamID.'
                )
            steamid64 = int(result["steamid"])

            summary = await fetch_json_with_retries(
                f"{STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v0002/",
                params={"key": key, "steamids": steamid64},
            )
            players = summary.get("response", {}).get("players", [])
            if not players:
                raise LinkError(
                    "Found the Steam account but couldn't load its profile. Try again in a bit."
                )
            player = players[0]
            return (
                steamid64 - STEAMID64_OFFSET,
                player["personaname"],
                player["profileurl"],
            )

    async def link(self, raw_identifier: str) -> LinkResult:
        account_id, personaname, profileurl = await self._resolve_account_id(
            raw_identifier
        )
        return LinkResult(
            external_id=raw_identifier.strip(),
            display_name=f"[{personaname}]({profileurl})",
            provider_account_id=str(account_id),
        )

    async def fetch_and_store(self, discordid: int, account_row: tuple) -> None:
        account_id = account_row[4]
        rank_data = await fetch_json_with_retries(
            f"{DEADLOCK_BASE_URL}/v1/players/{account_id}/rank"
        )

        with readable_payload(self.game):
            rank = rank_data["rank"]
            if rank == 0:
                return  # Obscurus / no ranked match on record yet -- don't overwrite with "no rank"

            tier = config.game_data["deadlock"]["tiers"][rank - 1]
            division = rank_data[
                "subrank"
            ]  # divisions_ascend: true here, use directly, no inversion
            rank_value = compute_rank_value("deadlock", tier, division)
            rank_label = format_rank_label("deadlock", tier, division)

        await db.perform_one(
            """
            INSERT INTO profile_stats (discordid, game, rank_value, rank_label, updated_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (discordid, game) DO UPDATE SET
                rank_value = EXCLUDED.rank_value,
                rank_label = EXCLUDED.rank_label,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (discordid, "deadlock", rank_value, rank_label),
        )

        await self._maybe_seed_mains(discordid, account_id)

    async def _maybe_seed_mains(self, discordid: int, account_id: int) -> None:
        """Seed mains from the 3 most-played heroes (by time_played), but only the
        first time -- once a player has any mains on file, refresh never overwrites them."""
        if await has_profile_mains(discordid, "deadlock"):
            return

        hero_stats = await fetch_json_with_retries(
            f"{DEADLOCK_BASE_URL}/v1/players/hero-stats",
            params={"account_ids": account_id},
        )
        if not hero_stats:
            return

        with readable_payload(self.game):
            top_heroes = sorted(
                hero_stats, key=lambda h: h["time_played"], reverse=True
            )[:3]
            hero_names = config.game_data["deadlock"]["hero_ids"]
            mains = [
                hero_names[h["hero_id"]]
                for h in top_heroes
                if h["hero_id"] in hero_names
            ]
        await seed_mains(discordid, "deadlock", mains)
