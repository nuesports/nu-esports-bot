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
        ...

    async def fetch_and_store(self, discordid: int, account_row: tuple) -> None:
        ...
    