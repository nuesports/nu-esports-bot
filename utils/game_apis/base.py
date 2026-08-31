import contextlib
from dataclasses import dataclass
from typing import Protocol

from utils import db


class LinkError(Exception):
    """Raised when a submitted identifier can't be resolved via the game's API"""


class GameAPIError(Exception):
    """Raised when a game's API is unreachable, errors out, or sends back something we
    can't read. The counterpart to LinkError, which means the identifier itself was bad.

    status carries the HTTP status when there was one, so callers can still tell a 404
    apart from everything else without reaching for aiohttp's types.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@contextlib.contextmanager
def readable_payload(game: str):
    """Turn a response we can't read into a GameAPIError.

    Wraps parsing only. A KeyError in here means the provider changed the shape of its
    JSON, which is the API failing, not the caller -- so it shouldn't reach a caller as
    a bare KeyError they'd have to have predicted.
    """
    try:
        yield
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
        raise GameAPIError(f"{game} sent a response we couldn't read: {e!r}") from e


@dataclass
class LinkResult:
    external_id: str
    display_name: str
    region: str | None = None
    provider_account_id: str | None = None
    provider_secondary_id: str | None = None


class GameAPIClient(Protocol):
    game: str

    async def link(self, raw_identifier: str) -> LinkResult: ...
    async def fetch_and_store(self, discordid: int, account_row: tuple) -> None: ...


async def has_profile_mains(discordid: int, game: str) -> bool:
    """True if the player already has any mains on file for this game."""
    existing = await db.fetch_one(
        "SELECT 1 FROM profile_mains WHERE discordid = %s AND game = %s LIMIT 1;",
        (discordid, game),
    )
    return existing is not None


async def seed_mains(discordid: int, game: str, mains: list[str]) -> None:
    """Bulk-insert seeded mains; no-op if there aren't any."""
    if not mains:
        return
    await db.perform_many(
        "INSERT INTO profile_mains (discordid, game, main) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING;",
        [(discordid, game, m) for m in mains],
    )


async def has_profile_roles(discordid: int, game: str) -> bool:
    """True if the player already has any roles on file for this game."""
    existing = await db.fetch_one(
        "SELECT 1 FROM profile_roles WHERE discordid = %s AND game = %s LIMIT 1;",
        (discordid, game),
    )
    return existing is not None


async def seed_roles(discordid: int, game: str, roles) -> None:
    """Bulk-insert seeded roles; no-op if there aren't any."""
    if not roles:
        return
    await db.perform_many(
        "INSERT INTO profile_roles (discordid, game, role) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING;",
        [(discordid, game, r) for r in roles],
    )
