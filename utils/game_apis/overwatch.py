from utils import config, db
from utils.ranks import compute_rank_value, format_rank_label

from .base import (
    GameAPIError,
    LinkError,
    LinkResult,
    has_profile_mains,
    has_profile_roles,
    readable_payload,
    seed_mains,
    seed_roles,
)
from .http import fetch_json_with_retries

OVERFAST_BASE_URL = "https://overfast-api.tekrop.fr"
ROLE_MAP = {"tank": "Tank", "damage": "Damage", "support": "Support"}


class OverwatchClient:
    game = "overwatch"

    async def link(self, raw_identifier: str) -> LinkResult:
        if "#" not in raw_identifier:
            raise LinkError("BattleTag must be in the form Name#1234")
        name, tag = (p.strip() for p in raw_identifier.split("#", 1))
        if not name or not tag:
            raise LinkError("BattleTag must be in the form Name#1234")

        player_id = f"{name}-{tag}"
        try:
            await fetch_json_with_retries(f"{OVERFAST_BASE_URL}/players/{player_id}/summary")
        except GameAPIError as e:
            if e.status == 404:
                raise LinkError(
                    f"Couldn't find {name}#{tag} -- double check the BattleTag, and make sure your Career "
                    "Profile is set to Public (Esc -> Options -> Social -> Career Profile Visibility -> Public)."
                ) from e
            raise LinkError("OverFast's having troubles right now, try again soon") from e

        resolved_id = f"{name}#{tag}"
        return LinkResult(external_id=resolved_id, display_name=resolved_id)

    async def fetch_and_store(self, discordid: int, account_row: tuple) -> None:
        with readable_payload(self.game):
            external_id = account_row[1]  # no puuid-equivalent here -- BattleTag is the key
            name, tag = external_id.split("#", 1)
            player_id = f"{name}-{tag}"

        summary = await fetch_json_with_retries(f"{OVERFAST_BASE_URL}/players/{player_id}/summary")

        rows = []
        with readable_payload(self.game):
            competitive = (summary.get("competitive") or {}).get("pc") or {}
            for api_role, our_role in ROLE_MAP.items():
                rank = competitive.get(api_role)
                if rank is None:
                    continue  # unranked in this role -- don't overwrite with "no rank"
                tier = rank["division"].title()   # OverFast's "division" = our "tier"
                division = rank["tier"]           # OverFast's "tier" = our "division"; 1 is best, matches League
                rank_value = compute_rank_value("overwatch", tier, division)
                rank_label = format_rank_label("overwatch", tier, division)
                rows.append((discordid, "overwatch", our_role, rank_value, rank_label))

        if not rows:
            return

        await db.perform_many(
            """
            INSERT INTO profile_role_ranks (discordid, game, role, rank_value, rank_label, updated_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (discordid, game, role) DO UPDATE SET
                rank_value = EXCLUDED.rank_value,
                rank_label = EXCLUDED.rank_label,
                updated_at = CURRENT_TIMESTAMP;
            """,
            rows,
        )

        await self._maybe_seed_mains(discordid, player_id)

    async def _maybe_seed_mains(self, discordid: int, player_id: str) -> None:
        """Seed mains from the 3 most-played heroes in competitive (by time_played), but
        only the first time -- once a player has any mains on file, refresh never overwrites them."""
        if await has_profile_mains(discordid, "overwatch"):
            return

        career = await fetch_json_with_retries(
            f"{OVERFAST_BASE_URL}/players/{player_id}/stats/career",
            params={"gamemode": "competitive"},
        )
        with readable_payload(self.game):
            hero_keys = config.game_data["overwatch"]["hero_keys"]
            played = [
                (key, stats["game"]["time_played"])
                for key, stats in career.items()
                if key != "all-heroes" and key in hero_keys and "game" in stats
            ]
            if not played:
                return

            top_heroes = sorted(played, key=lambda item: item[1], reverse=True)[:3]
            mains = [hero_keys[key] for key, _ in top_heroes]
        await seed_mains(discordid, "overwatch", mains)

        await self._maybe_seed_roles(discordid, [key for key, _ in top_heroes])

    async def _maybe_seed_roles(self, discordid: int, hero_keys_played: list[str]) -> None:
        """Seed roles from the same top-3 mains just seeded above (e.g. Kiriko/Mercy/Ana
        -> just Support; Genji/Sigma/Juno -> all three), but only if the player has no
        roles on file -- checked separately from mains, since /profile set main allows
        clearing mains to empty without touching roles."""
        if await has_profile_roles(discordid, "overwatch"):
            return

        hero_roles = config.game_data["overwatch"]["hero_roles"]
        roles = {hero_roles[key] for key in hero_keys_played if key in hero_roles}
        await seed_roles(discordid, "overwatch", roles)
