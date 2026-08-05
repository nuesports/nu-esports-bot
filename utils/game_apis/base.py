from dataclasses import dataclass
from typing import Protocol

class LinkError(Exception):
    """Raised when a submitted identifier can't be resolved via the game's API"""

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
