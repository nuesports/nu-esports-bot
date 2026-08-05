import asyncio
import aiohttp

async def fetch_json_with_retries(url: str, headers: dict | None = None, params: dict | None = None) -> dict:
    """GET a url and parse json, retrying transient failures up to 4 times.

    a 404 (doesn't exist) or 401/403 (bad/expired api key) raises immediately without
    retrying -- neither gets fixed by trying again."""

    timeout = aiohttp.ClientTimeout(total=10)
    max_attempts = 4
    retry_delay_seconds = 5
    NO_RETRY_STATUSES = (401, 403, 404)

    for attempt in range(max_attempts):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except aiohttp.ClientResponseError as e:
            if e.status in NO_RETRY_STATUSES or attempt == max_attempts - 1:
                raise
            await asyncio.sleep(retry_delay_seconds)
        except Exception:
            if attempt == max_attempts - 1:
                raise
            await asyncio.sleep(retry_delay_seconds)