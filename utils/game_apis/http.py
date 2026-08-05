import asyncio
import aiohttp

async def fetch_json_with_retries(url: str, headers: dict | None = None, params: dict | None = None) -> dict:
    """GET a url and parse json, retrying transient failures up to 4 times.
    
    a 404 raises immediately without retrying."""

    timeout = aiohttp.ClientTimeout(total=10)
    max_attempts = 4
    retry_delay_seconds = 5

    for attempt in range(max_attempts):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except aiohttp.ClientResponseError as e:
            if e.status == 404 or attempt == max_attempts - 1:
                raise
            await asyncio.sleep(retry_delay_seconds)
        except Exception:
            if attempt == max_attempts - 1:
                raise
            await asyncio.sleep(retry_delay_seconds)