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

    async def link(self, raw_identifier: str) -> LinkResult:
        if "#" not in raw_identifier:
            raise LinkError("Riot ID must be in the form Name#Tag")
        game_name, tag_line = (p.strip() for p in raw_identifier.split("#", 1))
        if not game_name or not tag_line:
            raise LinkError("Riot ID must be in the form Name#Tag")

        headers = {"X-Riot-Token": _get_riot_api_key()}

        account_url = {
            f"https://{REGIONAL_ROUTING}.api.riotgames.com/riot/accounts/v1/accounts/"
            f"by-riot-id/{game_name}/{tag_line}"
        }
        try:
            account = await fetch_json_with_retries(account_url, headers=headers)
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                raise LinkError(f"No Riot account found for {game_name}#{tag_line}")
            raise LinkError("Riot API's having troubles right now, try again soon")

        puuid = account["puuid"]
        resolved_id = f"{account.get('game_name', game_name)}#{account.get('tag_line', tag_line)}"

        summoner_url = f"https://{PLATFORM_ROUTING}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
        try:
            summoner = await fetch_json_with_retries(summoner_url, headers=headers)
        except aiohttp.ClientResponseError:
            raise LinkError("Found your Riot account, but couldn't load your summoner data. Try again in a bit")

        return LinkResult(
            external_id = resolved_id,
            display_name = resolved_id,
            provider_account_id = puuid,
            provider_secondary_id=summoner["id"]
        )

    async def fetch_and_store(self, discordid: int, account_row: tuple) -> None:
        ...
    