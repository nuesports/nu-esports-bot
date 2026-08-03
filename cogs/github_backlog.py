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

SPECIAL_CHARS = re.compile(r"[\\`*_~|>\[\]()]")


def escape_markdown(text: str) -> str:
    """Neutralize Discord markdown in untrusted PR/issue titles (masked links,
    bold/code spans, blockquotes, etc.)."""
    collapsed = text.replace("\r\n", " ").replace("\n", " ")
    return SPECIAL_CHARS.sub(lambda m: "\\" + m.group(0), collapsed)


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Check GitHub's HMAC-SHA256 payload signature so nobody can spoof PR/issue events."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


class GithubBacklog(commands.Cog):
    """Posts and pins PR/issue notifications, unpins them on merge/close. No
    slash commands -- driven entirely by the aiohttp webhook route in setup()."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        cfg = config.config["github_backlog"]
        self.pr_channel_id = cfg["pr_channel"]
        self.issue_channel_id = cfg["issue_channel"]

    async def post_and_pin(self, channel_id: int, repo: str, number: int, kind: str, content: str) -> None:
        channel = await self.bot.fetch_channel(channel_id)
        message = await channel.send(
            content=content,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress=True,
        )
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

    async def send_plain(self, channel_id: int, content: str) -> None:
        channel = await self.bot.fetch_channel(channel_id)
        await channel.send(
            content=content,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress=True,
        )

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
            content = (
                f"pr [{number}]({pr['html_url']}), {escape_markdown(pr['title'])}. "
                f"author [{author['login']}]({author['html_url']})"
            )
            await self.post_and_pin(self.pr_channel_id, repo, number, "pr", content)
        elif action == "closed" and pr.get("merged"):
            await self.unpin(repo, number, "pr")
            merged_by = pr.get("merged_by")
            merged_by_text = (
                f"[{merged_by['login']}]({merged_by['html_url']})" if merged_by else "unknown"
            )
            content = (
                f"pr [{number}]({pr['html_url']}), {escape_markdown(pr['title'])} merged. "
                f"merged by {merged_by_text}"
            )
            await self.send_plain(self.pr_channel_id, content)

    async def handle_issue(self, payload: dict) -> None:
        issue = payload["issue"]
        repo = payload["repository"]["full_name"]
        number = issue["number"]
        action = payload["action"]

        if action == "opened":
            author = issue["user"]
            content = (
                f"issue [{number}]({issue['html_url']}), {escape_markdown(issue['title'])}. "
                f"author [{author['login']}]({author['html_url']})"
            )
            await self.post_and_pin(self.issue_channel_id, repo, number, "issue", content)
        elif action == "closed":
            await self.unpin(repo, number, "issue")


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
