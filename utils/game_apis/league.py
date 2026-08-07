import aiohttp

from utils import config, db
from utils.ranks import compute_rank_value, format_rank_label
from .base import LinkError, LinkResult
from .http import fetch_json_with_retries

REGIONAL_ROUTING = "americas"
PLATFORM_ROUTING = "na1"
RANKED_SOLO_QUEUE = "RANKED_SOLO_5x5"
ROMAN_TO_DIVISION = {"I": 1, "II": 2, "III": 3, "IV": 4}

def _get_riot_api_key() -> str:
    apis = config.secrets.get("apis", {})
    key = apis.get("riot-api-key") if isinstance(apis, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise LinkError("Riot API key not configured. Ask a bot dev to set `secrets.yaml -> apis.riot-api-key`")
    return key.strip()

class LeagueClient:
    game = "league"

    async def _maybe_seed_mains(self, discordid: int, puuid: str, headers: dict) -> None:
        """Seed mains via top 3 mastery, but only when mains is empty (aka the first time)"""
        existing = await db.fetch_one(
            "SELECT 1 from profile_mains WHERE discordid = %s and game = 'league' LIMIT 1;",
            (discordid, ),
        )
        if existing:
            return

        mastery_url = (
            f"https://{PLATFORM_ROUTING}.api.riotgames.com/lol/champion-mastery/v4/"
            f"champion-masteries/by-puuid/{puuid}/top?count=3"
        )
        top_champs = await fetch_json_with_retries(mastery_url, headers=headers)
        if not top_champs:
            return

        champion_names = config.game_data["league"]["champion_ids"]
        mains = [champion_names[c["championId"]] for c in top_champs if c["championId"] in champion_names]
        if not mains:
            return

        await db.perform_many(
            "INSERT INTO profile_mains (discordid, game, main) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING;",
            [(discordid, "league", m) for m in mains]
        )

    async def link(self, raw_identifier: str) -> LinkResult:
        if "#" not in raw_identifier:
            raise LinkError("Riot ID must be in the form Name#Tag")
        game_name, tag_line = (p.strip() for p in raw_identifier.split("#", 1))
        if not game_name or not tag_line:
            raise LinkError("Riot ID must be in the form Name#Tag")

        headers = {"X-Riot-Token": _get_riot_api_key()}

        account_url = (
            f"https://{REGIONAL_ROUTING}.api.riotgames.com/riot/account/v1/accounts/"
            f"by-riot-id/{game_name}/{tag_line}"
        )
        try:
            account = await fetch_json_with_retries(account_url, headers=headers)
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                raise LinkError(f"No Riot account found for {game_name}#{tag_line}")
            raise LinkError("Riot API's having troubles right now, try again soon")

        puuid = account["puuid"]
        resolved_id = f"{account.get('gameName', game_name)}#{account.get('tagLine', tag_line)}"

        return LinkResult(
            external_id = resolved_id,
            display_name = resolved_id,
            provider_account_id = puuid,
            provider_secondary_id=None
        )

    async def fetch_and_store(self, discordid: int, account_row: tuple) -> None:
        puuid = account_row[4]
        headers = {"X-Riot-Token": _get_riot_api_key()}

        entries_url = f"https://{PLATFORM_ROUTING}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        entries = await fetch_json_with_retries(entries_url, headers=headers)

        solo_entry = next((e for e in entries if e.get("queueType") == RANKED_SOLO_QUEUE), None)
        if solo_entry is not None:
            tier = solo_entry["tier"].title()
            division = ROMAN_TO_DIVISION.get(solo_entry.get("rank"), 1)
            rank_value = compute_rank_value("league", tier, division)
            rank_label = format_rank_label("league", tier, division)

            await db.perform_one(
                """
                INSERT INTO profile_stats (discordid, game, rank_value, rank_label, updated_at)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (discordid, game) DO UPDATE SET
                    rank_value = EXCLUDED.rank_value,
                    rank_label = EXCLUDED.rank_label,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (discordid, "league", rank_value, rank_label),
            )

        await self._maybe_seed_mains(discordid, puuid, headers)