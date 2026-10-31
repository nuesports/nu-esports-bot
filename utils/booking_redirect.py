"""One-click booking links served by the bot's existing HTTP server."""

import asyncio
import hashlib
import hmac
import html
import logging
import os
from urllib.parse import urlsplit

import discord
from aiohttp import web

logger = logging.getLogger(__name__)


def signature(channel_id: int, message_id: int) -> str:
    key = os.environ["BOOKING_REDIRECT_SECRET"].encode()
    return hmac.new(
        key, f"booking:{channel_id}:{message_id}".encode(), hashlib.sha256
    ).hexdigest()


def booking_redirect_url(message: discord.Message) -> str | None:
    """Without public hosting configured, keep the ordinary direct ggLeap link."""
    base = os.environ.get("BOOKING_REDIRECT_URL", "").rstrip("/")
    if not base or not os.environ.get("BOOKING_REDIRECT_SECRET"):
        return None
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("BOOKING_REDIRECT_URL must be an HTTPS base URL")
    return f"{base}/booking/{message.channel.id}/{message.id}/{signature(message.channel.id, message.id)}"


def add_booking_routes(app: web.Application, bot: discord.Bot) -> None:
    async def booking(request: web.Request) -> web.Response:
        from cogs.pcs import GGLEAP_BOOKING_GRID_URL, booking_link

        channel_id = int(request.match_info["channel"])
        message_id = int(request.match_info["message"])
        if not os.environ.get("BOOKING_REDIRECT_SECRET") or not hmac.compare_digest(
            signature(channel_id, message_id), request.match_info["signature"]
        ):
            raise web.HTTPNotFound()
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise web.HTTPNotFound()
        try:
            async with asyncio.timeout(5):
                message = await channel.fetch_message(message_id)
            if message.author != bot.user:
                raise web.HTTPNotFound()
            _, marker, payload = booking_link(message).partition("#nue=")
            if not marker:
                raise ValueError("No booking payload")
        except (discord.HTTPException, TimeoutError, ValueError) as error:
            raise web.HTTPNotFound(
                text="This booking link is no longer available."
            ) from error
        target = f"{GGLEAP_BOOKING_GRID_URL}#nue={payload}"
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if request.method == "GET":
            # GET/HEAD and ordinary link previews never acknowledge a reservation.
            # A real browser submits automatically, with no second user action.
            return web.Response(
                text="<!doctype html><title>Opening ggLeap</title>"
                '<form method="post"></form><script>'
                "const go = () => document.forms[0].submit();"
                "if (document.prerendering) "
                'document.addEventListener("prerenderingchange", go, {once:true});'
                "else go();</script><noscript>"
                f'<a href="{html.escape(target, quote=True)}">Open ggLeap</a>'
                "</noscript>",
                content_type="text/html",
                headers=headers,
            )
        try:
            async with asyncio.timeout(3):
                await message.add_reaction("✅")
        except discord.HTTPException, TimeoutError:
            # Booking remains accessible even if Discord cannot add the reaction.
            logger.warning(
                "Could not acknowledge booking message %s", message_id, exc_info=True
            )
        else:
            if cog := bot.get_cog("PCs"):
                cog.pending_acknowledgments.pop(message_id, None)
        raise web.HTTPSeeOther(target, headers=headers)

    path = r"/booking/{channel:\d+}/{message:\d+}/{signature:[a-f0-9]{64}}"
    app.router.add_get(path, booking, allow_head=False)
    app.router.add_post(path, booking)
