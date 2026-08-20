from types import SimpleNamespace

import aiohttp
import pytest

from utils.game_apis import http
from utils.game_apis.base import GameAPIError


class FakeResponse:
    def __init__(self, status, json_data=None):
        self.status = status
        self._json_data = json_data

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url=""), history=(), status=self.status,
            )

    async def json(self):
        return self._json_data


class FakeGetCtx:
    def __init__(self, outcome):
        self.outcome = outcome

    async def __aenter__(self):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, outcome):
        self.outcome = outcome

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers=None, params=None):
        return FakeGetCtx(self.outcome)


@pytest.fixture
def fake_sessions(monkeypatch):
    """Queues one outcome (FakeResponse or exception instance) per aiohttp.ClientSession(...)
    call -- http.py opens a fresh session each retry attempt."""
    queue = []

    def factory(*args, **kwargs):
        return FakeSession(queue.pop(0))

    monkeypatch.setattr(http.aiohttp, "ClientSession", factory)

    async def no_delay(seconds):
        return None

    monkeypatch.setattr(http.asyncio, "sleep", no_delay)
    return queue


@pytest.mark.asyncio
async def test_success_first_attempt(fake_sessions):
    fake_sessions.append(FakeResponse(200, {"a": 1}))
    result = await http.fetch_json_with_retries("https://example.com")
    assert result == {"a": 1}


@pytest.mark.asyncio
async def test_404_raises_immediately_without_retry(fake_sessions):
    fake_sessions.append(FakeResponse(404))
    with pytest.raises(GameAPIError) as exc_info:
        await http.fetch_json_with_retries("https://example.com")
    assert exc_info.value.status == 404   # clients key their "not found" message off this


@pytest.mark.asyncio
async def test_401_raises_immediately_without_retry(fake_sessions):
    fake_sessions.append(FakeResponse(401))
    with pytest.raises(GameAPIError) as exc_info:
        await http.fetch_json_with_retries("https://example.com")
    assert exc_info.value.status == 401


@pytest.mark.asyncio
async def test_transient_failure_then_success_retries(fake_sessions):
    fake_sessions.append(TimeoutError())
    fake_sessions.append(FakeResponse(200, {"ok": True}))
    result = await http.fetch_json_with_retries("https://example.com")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_exhausts_all_attempts_then_raises(fake_sessions):
    for _ in range(4):
        fake_sessions.append(TimeoutError())
    with pytest.raises(GameAPIError) as exc_info:
        await http.fetch_json_with_retries("https://example.com")
    assert exc_info.value.status is None   # never got far enough to have one
