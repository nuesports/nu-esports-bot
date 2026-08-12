#!/usr/bin/env python3
"""Regenerates deadlock.yaml's hero_ids block from deadlock-api.com.

New hero dropped? Same order as League's champions:
  1. drop the image in assets/games/deadlock/characters/<name>.webp
  2. add the name to the characters list in deadlock.yaml
  3. run this (python scripts/update_deadlock_heroes.py) to pick up the new id

Only pulls heroes that are player_selectable and not disabled -- deadlock-api lists
a bunch of in-development/test heroes too that nobody can actually pick yet.
Only touches the hero_ids block, nothing else in the file gets touched.
"""
import json
import urllib.request
from pathlib import Path

import yaml

DEADLOCK_YAML = Path(__file__).resolve().parent.parent / "data" / "games" / "deadlock.yaml"
HEROES_URL = "https://api.deadlock-api.com/v1/assets/heroes"

HEADER = (
    "hero_ids: # heroId -> name, for mapping /v1/players/hero-stats results. Regenerate with\n"
    "          # scripts/update_deadlock_heroes.py when a new hero is added to the\n"
    "          # characters list above.\n"
)


def fetch_json(url: str) -> list:
    # deadlock-api.com 403s the default urllib User-Agent, so pretend to be a browser
    req = urllib.request.Request(url, headers={"User-Agent": "nu-esports-bot/update-script"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def main() -> None:
    heroes = fetch_json(HEROES_URL)
    selectable = [h for h in heroes if h.get("player_selectable") and not h.get("disabled")]

    entries = sorted(((h["name"], h["id"]) for h in selectable), key=lambda item: item[0].lower())

    text = DEADLOCK_YAML.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    yaml_characters = set(parsed.get("characters") or [])
    api_names = {name for name, _ in entries}

    missing_from_yaml = api_names - yaml_characters
    extra_in_yaml = yaml_characters - api_names
    if missing_from_yaml:
        print(f"heads up: {len(missing_from_yaml)} hero(es) selectable in the API but not in `characters:` yet, "
              "add them there too + drop an image in assets/games/deadlock/characters/:")
        for name in sorted(missing_from_yaml):
            print(f"  - {name}")
    if extra_in_yaml:
        print(f"heads up: {len(extra_in_yaml)} name(s) in `characters:` the API doesn't know about "
              "(typo? hero renamed?):")
        for name in sorted(extra_in_yaml):
            print(f"  - {name}")

    # yaml treats ": " inside an unquoted scalar as another mapping, so a name with
    # a colon in it needs quoting to stay valid
    body = "\n".join(f"  {hid}: '{name}'" if ":" in name else f"  {hid}: {name}" for name, hid in entries)
    new_block = HEADER + body + "\n"

    if "hero_ids:" in text:
        before = text[: text.index("hero_ids:")].rstrip("\n") + "\n\n"
    else:
        before = text.rstrip("\n") + "\n\n"

    DEADLOCK_YAML.write_text(before + new_block, encoding="utf-8")
    print(f"wrote {len(entries)} hero_ids entries to {DEADLOCK_YAML}")


if __name__ == "__main__":
    main()
