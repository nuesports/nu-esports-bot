"""GitHub webhook receiver: posts PR/issue notifications and pins/unpins them.

Runs a small aiohttp server alongside the bot's own gateway connection, started
from setup() via bot.loop.create_task() -- not from on_ready, since on_ready can
fire more than once on reconnect and would try to bind the port twice.
"""
import hashlib
import hmac
import re

import discord
from discord.ext import commands
from aiohttp import web

from utils import config, db

MASKED_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
LEADING_HEADING = re.compile(r"^#{1,3}\s*")
FORMATTING_CHARS = re.compile(r"[\\`*_~|>\[\]#]")

COLOR_OPEN_ISSUE = discord.Color.from_rgb(237, 66, 69)
COLOR_OPEN_PR = discord.Color.from_rgb(59, 130, 246)
COLOR_DONE = discord.Color.from_rgb(48, 199, 107)


def strip_markdown(text: str) -> str:
    """Strip Discord/GitHub markdown syntax down to plain text -- untrusted PR/issue
    titles/bodies otherwise render as headings, bold blocks, masked links, etc.
    (Discord embeds render '#'/'##' as actual large headings, not just message content.)"""
    collapsed = text.replace("\r\n", " ").replace("\n", " ").strip()
    collapsed = LEADING_HEADING.sub("", collapsed)
    collapsed = MASKED_LINK.sub(r"\1", collapsed)
    return FORMATTING_CHARS.sub("", collapsed).strip()


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Check GitHub's HMAC-SHA256 payload signature so nobody can spoof PR/issue events."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def opengraph_image_url(repo: str, kind: str, number: int) -> str:
    """Same social-preview image a plain pasted GitHub link would show -- the
    path-prefix segment GitHub normally signs isn't actually validated, any
    value works, confirmed against the real endpoint."""
    path_kind = "pull" if kind == "pr" else "issues"
    return f"https://opengraph.githubassets.com/1/{repo}/{path_kind}/{number}"


def first_body_line(body: str | None) -> str:
    """First non-blank line of a PR/issue body, stripped to plain text."""
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped:
            return strip_markdown(stripped)
    return "No description provided."


def build_notification_embed(
    kind: str, number: int, repo: str, title: str, body: str | None, url: str,
    footer_verb: str, username: str, color: discord.Color,
) -> discord.Embed:
    type_word = "PR" if kind == "pr" else "Issue"
    embed = discord.Embed(
        title=f"{type_word} {number} - {strip_markdown(title)}",
        description=first_body_line(body),
        url=url,
        color=color,
    )
    embed.set_image(url=opengraph_image_url(repo, kind, number))
    embed.set_footer(text=f"{footer_verb} {username}")
    return embed


class GithubBacklog(commands.Cog):
    """Posts and pins PR/issue notifications, unpins them on merge/close. No
    slash commands -- driven entirely by the aiohttp webhook route in setup()."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        cfg = config.config["github_backlog"]
        self.pr_channel_id = cfg["pr_channel"]
        self.issue_channel_id = cfg["issue_channel"]

    async def post_and_pin(self, channel_id: int, repo: str, number: int, kind: str, embed: discord.Embed) -> None:
        channel = await self.bot.fetch_channel(channel_id)
        message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        try:
            await message.pin()
        except discord.HTTPException as e:
            print(f"[github_backlog] failed to pin {repo}#{number}: {e}")

        await db.perform_one(
            """
            INSERT INTO github_backlog_messages (repo, number, kind, channel_id, message_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (repo, number, kind) DO UPDATE SET
                channel_id = EXCLUDED.channel_id,
                message_id = EXCLUDED.message_id;
            """,
            (repo, number, kind, channel_id, message.id),
        )

    async def send_embed(self, channel_id: int, embed: discord.Embed) -> None:
        channel = await self.bot.fetch_channel(channel_id)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def unpin(self, repo: str, number: int, kind: str) -> None:
        row = await db.fetch_one(
            "SELECT channel_id, message_id FROM github_backlog_messages WHERE repo = %s AND number = %s AND kind = %s;",
            (repo, number, kind),
        )
        if row is None:
            return
        channel_id, message_id = row
        channel = await self.bot.fetch_channel(channel_id)
        try:
            message = await channel.fetch_message(message_id)
            await message.unpin()
        except discord.HTTPException as e:
            print(f"[github_backlog] failed to unpin {repo}#{number}: {e}")

    async def handle_pull_request(self, payload: dict) -> None:
        pr = payload["pull_request"]
        repo = payload["repository"]["full_name"]
        number = pr["number"]
        action = payload["action"]

        if action == "opened":
            author = pr["user"]
            embed = build_notification_embed(
                "pr", number, repo, pr["title"], pr.get("body"), pr["html_url"],
                footer_verb="Authored by", username=author["login"], color=COLOR_OPEN_PR,
            )
            await self.post_and_pin(self.pr_channel_id, repo, number, "pr", embed)
        elif action == "closed" and pr.get("merged"):
            await self.unpin(repo, number, "pr")
            merged_by = pr.get("merged_by")
            username = merged_by["login"] if merged_by else "unknown"
            embed = build_notification_embed(
                "pr", number, repo, pr["title"], pr.get("body"), pr["html_url"],
                footer_verb="Merged by", username=username, color=COLOR_DONE,
            )
            await self.send_embed(self.pr_channel_id, embed)

    async def handle_issue(self, payload: dict) -> None:
        issue = payload["issue"]
        repo = payload["repository"]["full_name"]
        number = issue["number"]
        action = payload["action"]

        if action == "opened":
            author = issue["user"]
            embed = build_notification_embed(
                "issue", number, repo, issue["title"], issue.get("body"), issue["html_url"],
                footer_verb="Authored by", username=author["login"], color=COLOR_OPEN_ISSUE,
            )
            await self.post_and_pin(self.issue_channel_id, repo, number, "issue", embed)
        elif action == "closed":
            await self.unpin(repo, number, "issue")
            closer = payload.get("sender") or issue["user"]
            embed = build_notification_embed(
                "issue", number, repo, issue["title"], issue.get("body"), issue["html_url"],
                footer_verb="Closed by", username=closer["login"], color=COLOR_DONE,
            )
            await self.send_embed(self.issue_channel_id, embed)


def create_app(backlog: GithubBacklog) -> web.Application:
    secret = config.secrets["github"]["webhook_secret"]

    async def webhook_handler(request: web.Request) -> web.Response:
        body = await request.read()
        if not verify_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
            return web.Response(status=401, text="bad signature")

        event = request.headers.get("X-GitHub-Event")
        payload = await request.json()

        if event == "pull_request":
            await backlog.handle_pull_request(payload)
        elif event == "issues":
            await backlog.handle_issue(payload)

        return web.Response(status=204)

    app = web.Application()
    app.router.add_post("/github/webhook", webhook_handler)
    return app


async def start_webhook_server(backlog: GithubBacklog, port: int = 8001) -> None:
    app = create_app(backlog)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[github_backlog] webhook server listening on :{port}")


def setup(bot: discord.Bot) -> None:
    backlog = GithubBacklog(bot)
    bot.add_cog(backlog)
    bot.loop.create_task(start_webhook_server(backlog))
