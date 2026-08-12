#!/usr/bin/env python3
"""Regenerates league.yaml's champion_ids block from Data Dragon.

New champ dropped? Do it in this order:
  1. drop the image in assets/games/league/characters/<name>.webp (needed regardless)
  2. add the name to the characters list in league.yaml (needed for /profile set main)
  3. run this (python scripts/update_champion_ids.py) to pick up the new id

You don't technically need to do 1/2 first, this script doesn't care what's already
in characters -- it just pulls the full roster from Data Dragon and warns you about
whatever's missing from characters so you don't forget. Only touches the champion_ids
block, nothing else in the file gets touched.
"""
import json
import urllib.request
from pathlib import Path

import yaml

LEAGUE_YAML = Path(__file__).resolve().parent.parent / "data" / "games" / "league.yaml"
VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CHAMPION_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"

HEADER = (
    "champion_ids: # championId -> name, for mapping champion-mastery-v4 results. Regenerate with\n"
    "              # scripts/update_champion_ids.py when a new champion is added to the\n"
    "              # characters list above.\n"
)


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


def main() -> None:
    versions = fetch_json(VERSIONS_URL)
    latest = versions[0]
    champion_data = fetch_json(CHAMPION_URL.format(version=latest))["data"]

    # data dragon also has 60000+ variant ids (60103 alongside Ahri's real 103) that
    # champion-mastery-v4 never actually returns, so skip those
    entries = sorted(
        ((c["name"], int(c["key"])) for c in champion_data.values() if int(c["key"]) < 10000),
        key=lambda item: item[0].lower(),
    )

    text = LEAGUE_YAML.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    yaml_characters = set(parsed.get("characters") or [])
    ddragon_names = {name for name, _ in entries}

    missing_from_yaml = ddragon_names - yaml_characters
    extra_in_yaml = yaml_characters - ddragon_names
    if missing_from_yaml:
        print(f"heads up: {len(missing_from_yaml)} champ(s) in data dragon but not in `characters:` yet, "
              "add them there too + drop an image in assets/games/league/characters/:")
        for name in sorted(missing_from_yaml):
            print(f"  - {name}")
    if extra_in_yaml:
        print(f"heads up: {len(extra_in_yaml)} name(s) in `characters:` that data dragon doesn't know about "
              "(typo? riot rename?):")
        for name in sorted(extra_in_yaml):
            print(f"  - {name}")

    # yaml treats ": " inside an unquoted scalar as another mapping, so anything with
    # a colon in the name (none today, but just in case) needs quoting to stay valid
    body = "\n".join(f"  {cid}: '{name}'" if ":" in name else f"  {cid}: {name}" for name, cid in entries)
    new_block = HEADER + body + "\n"

    if "champion_ids:" in text:
        before = text[: text.index("champion_ids:")].rstrip("\n") + "\n\n"
    else:
        before = text.rstrip("\n") + "\n\n"

    LEAGUE_YAML.write_text(before + new_block, encoding="utf-8")
    print(f"wrote {len(entries)} champion_ids entries to {LEAGUE_YAML} (patch {latest})")


if __name__ == "__main__":
    main()
