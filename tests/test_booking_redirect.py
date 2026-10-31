import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cogs import pcs
from utils.booking_redirect import add_booking_routes, booking_redirect_url


@pytest.mark.asyncio
@pytest.mark.parametrize("reaction_fails", [False, True])
async def test_booking_link_acknowledges_and_redirects_without_a_second_click(
    monkeypatch, reaction_fails
):
    monkeypatch.setenv("BOOKING_REDIRECT_URL", "https://bot.example.edu")
    monkeypatch.setenv("BOOKING_REDIRECT_SECRET", "test-signing-key")
    target = f"{pcs.GGLEAP_BOOKING_GRID_URL}#nue=booking-payload"
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 123
    message = SimpleNamespace(
        id=456,
        channel=channel,
        author="bot",
        embeds=[discord.Embed(url=target)],
        add_reaction=AsyncMock(),
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def react(emoji):
        started.set()
        await release.wait()
        if reaction_fails:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "")

    message.add_reaction.side_effect = react
    view = pcs.booking_view(target, message)
    message.components = [
        discord.components.ActionRow(row) for row in view.to_components()
    ]
    assert view.children[0].url == booking_redirect_url(message)
    assert view.children[0].style == discord.ButtonStyle.link
    assert pcs.booking_link(message) == target
    channel.fetch_message = AsyncMock(return_value=message)
    pending = {message.id: {}, 789: {}}
    bot = SimpleNamespace(
        user="bot",
        get_channel=lambda channel_id: channel,
        get_cog=lambda name: SimpleNamespace(pending_acknowledgments=pending),
    )
    app = web.Application()
    add_booking_routes(app, bot)
    path = urlsplit(view.children[0].url).path
    async with TestClient(TestServer(app)) as client:
        assert (await client.post(path.replace("/456/", "/457/"))).status == 404
        assert (await client.head(path)).status == 405
        response = await client.get(path)
        assert response.status == 200
        assert '<form method="post">' in await response.text()
        message.add_reaction.assert_not_awaited()
        async with asyncio.timeout(2):
            response = await client.post(path, allow_redirects=False)
            await started.wait()
        assert response.status == 303
        assert response.headers["Location"] == target
        message.add_reaction.assert_awaited_once_with("✅")
        assert (
            message.id in pending
        )  # Redirect arrived while Discord was still waiting.
        release.set()
    assert (message.id in pending) == reaction_fails
    assert 789 in pending
