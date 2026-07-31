import re
import unicodedata
from pathlib import Path

def slugify(name: str) -> str:
    """
    Simplifies a name, going from
    `Aurelion Sol` and `Bel'Veth` to `AurelionSol` and `BelVeth`.
    Digits are kept (`Soldier: 76` -> `Soldier76`) and accented letters
    are folded to their plain ASCII form (`Torbjörn` -> `Torbjorn`)
    rather than dropped.
    """
    ascii_only = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]", "", ascii_only)

def get_character_image(game:str , character:str) -> Path | None:
    """
    Gets the path to the filename of the character from a game
    """
    path = Path("assets/games") / game / "characters" / f"{slugify(character)}.webp"
    return path if path.exists() else None