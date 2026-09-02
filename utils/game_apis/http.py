import asyncio
from typing import Any

import aiohttp

from .base import GameAPIError

type Json = Any


async def fetch_json_with_retries(
    url: str, headers: dict | None = None, params: dict | None = None
) -> Json:
    """GET a url and parse json, retrying transient failures up to 4 times.

    a 404 (doesn't exist) or 401/403 (bad/expired api key) raises immediately without
    retrying -- neither gets fixed by trying again.

    Every failure leaves here as a GameAPIError, so callers never have to name aiohttp's
    types or json's. The HTTP status rides along on the error when there was one."""

    timeout = aiohttp.ClientTimeout(total=10)
    max_attempts = 4
    retry_delay_seconds = 5
    NO_RETRY_STATUSES = (401, 403, 404)

    for attempt in range(max_attempts):
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(url, headers=headers, params=params) as resp,
            ):
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientResponseError as e:
            if e.status in NO_RETRY_STATUSES or attempt == max_attempts - 1:
                raise GameAPIError(f"{url} returned {e.status}", status=e.status) from e
            await asyncio.sleep(retry_delay_seconds)
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            # ValueError covers json.JSONDecodeError on a body that claimed to be JSON
            # and wasn't. CancelledError is a BaseException and passes straight through.
            if attempt == max_attempts - 1:
                raise GameAPIError(f"{url} was unreachable: {e!r}") from e
            await asyncio.sleep(retry_delay_seconds)
    raise AssertionError("unreachable: the final attempt always raises")
