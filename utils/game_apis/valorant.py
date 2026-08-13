import aiohttp

from utils import config, db
from utils.ranks import compute_rank_value, format_rank_label

from .base import (
    LinkError,
    LinkResult,
    has_profile_mains,
    has_profile_roles,
    seed_mains,
    seed_roles,
)
from .http import fetch_json_with_retries

HENRIK_BASE_URL = "https://api.henrikdev.xyz"
PLATFORM = "pc"
MATCH_SAMPLE_SIZE = 10
MATCH_SAMPLE_CALLS = 3  # 30 matches total -- fine since this only runs once, at seed time
UNRANKED_TIERS = {"Unrated", "Unknown 1", "Unknown 2"}


def _get_henrik_api_key() -> str:
    apis = config.secrets.get("apis", {})
    key = apis.get("henrikdev-key") if isinstance(apis, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise LinkError("HenrikDev API key not configured. Ask a bot dev to set `secrets.yaml -> apis.henrikdev-key`")
    return key.strip()


class ValorantClient:
    game = "valorant"

    async def link(self, raw_identifier: str) -> LinkResult:
        if "#" not in raw_identifier:
            raise LinkError("Riot ID must be in the form Name#Tag")
        name, tag = (p.strip() for p in raw_identifier.split("#", 1))
        if not name or not tag:
            raise LinkError("Riot ID must be in the form Name#Tag")

        headers = {"Authorization": _get_henrik_api_key()}
        try:
            account = await fetch_json_with_retries(
                f"{HENRIK_BASE_URL}/valorant/v2/account/{name}/{tag}", headers=headers
            )
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                raise LinkError(f"No Riot account found for {name}#{tag}")
            raise LinkError("HenrikDev's API is having troubles right now, try again soon")

        data = account["data"]
        resolved_id = f"{data.get('name', name)}#{data.get('tag', tag)}"
        return LinkResult(
            external_id=resolved_id,
            display_name=resolved_id,
            region=data["region"],  # resolved automatically, no need to ask the player
            provider_account_id=data["puuid"],
        )

    async def fetch_and_store(self, discordid: int, account_row: tuple) -> None:
        region, puuid = account_row[3], account_row[4]
        headers = {"Authorization": _get_henrik_api_key()}

        mmr = await fetch_json_with_retries(
            f"{HENRIK_BASE_URL}/valorant/v3/by-puuid/mmr/{region}/{PLATFORM}/{puuid}", headers=headers
        )
        tier_name = mmr["data"]["current"]["tier"]["name"]  # e.g. "Gold 3", "Radiant", "Unrated"

        if tier_name not in UNRANKED_TIERS:
            if tier_name == "Radiant":
                tier, division = "Radiant", 1
            else:
                tier, division_str = tier_name.rsplit(" ", 1)
                division = int(division_str)
            rank_value = compute_rank_value("valorant", tier, division)
            rank_label = format_rank_label("valorant", tier, division)
            await db.perform_one(
                """
                INSERT INTO profile_stats (discordid, game, rank_value, rank_label, updated_at)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (discordid, game) DO UPDATE SET
                    rank_value = EXCLUDED.rank_value,
                    rank_label = EXCLUDED.rank_label,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (discordid, "valorant", rank_value, rank_label),
            )

        await self._maybe_seed_mains_and_roles(discordid, region, puuid, headers)

    async def _maybe_seed_mains_and_roles(self, discordid: int, region: str, puuid: str, headers: dict) -> None:
        if await has_profile_mains(discordid, "valorant"):
            return

        agent_counts: dict[str, int] = {}
        for start in range(0, MATCH_SAMPLE_SIZE * MATCH_SAMPLE_CALLS, MATCH_SAMPLE_SIZE):
            matches = await fetch_json_with_retries(
                f"{HENRIK_BASE_URL}/valorant/v4/by-puuid/matches/{region}/{PLATFORM}/{puuid}",
                headers=headers,
                params={"mode": "competitive", "size": MATCH_SAMPLE_SIZE, "start": start},
            )
            match_list = matches.get("data") or []
            if not match_list:
                break  # fewer than 30 competitive matches on record -- nothing more to page through
            for match in match_list:
                for player in match.get("players", []):
                    if player.get("puuid") == puuid:
                        agent_name = (player.get("agent") or {}).get("name")
                        if agent_name:
                            agent_counts[agent_name] = agent_counts.get(agent_name, 0) + 1
                        break

        if not agent_counts:
            return

        mains = [name for name, _ in sorted(agent_counts.items(), key=lambda item: item[1], reverse=True)[:3]]
        await seed_mains(discordid, "valorant", mains)

        await self._maybe_seed_roles(discordid, mains)

    async def _maybe_seed_roles(self, discordid: int, mains: list[str]) -> None:
        if await has_profile_roles(discordid, "valorant"):
            return

        characters = config.game_data["valorant"]["characters"]
        agents_roles = config.game_data["valorant"]["agents_roles"]
        agent_to_role = {characters[idx]: role.title() for role, idxs in agents_roles.items() for idx in idxs}

        roles = {agent_to_role[m] for m in mains if m in agent_to_role}
        await seed_roles(discordid, "valorant", roles)
