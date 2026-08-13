import re
import unicodedata
from pathlib import Path

import discord


def slugify(name: str) -> str:
    """Filename-safe slug: keeps digits, folds accents to ASCII (Torbjörn -> Torbjorn)."""
    ascii_only = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]", "", ascii_only)

def get_character_image(game:str , character:str) -> Path | None:
    """Path to a character's image file, or None if it doesn't exist."""
    path = Path("assets/games") / game / "characters" / f"{slugify(character)}.webp"
    return path if path.exists() else None

def image_attachment(image_path: Path | None) -> discord.File:
    """Fresh discord.File for a character image (single-use, don't reuse across calls)."""
    if image_path is None:
        return discord.utils.MISSING
    return discord.File(image_path, filename=image_path.name)