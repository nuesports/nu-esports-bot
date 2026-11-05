import random
import traceback

import discord
from discord.ext import commands, tasks

from utils import config
from utils import version as build_info
from utils.statuses import load_statuses

GUILD_ID = config.secrets["discord"]["guild_id"]
STREAM_LINK = "https://twitch.tv/NorthwesternEsports"
CYCLE_MINS = 3


class Presence(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot: discord.Bot = bot
        self.index: int = 0
        self.lastindex: int = -1
        self.statuses: list[discord.Activity] = load_statuses()
        # worked out once here, so the command never waits on git
        self.started_at = discord.utils.utcnow()
        self.commit: str = build_info.git_commit()
        self.commit_date: str | None = build_info.commit_date()
        self.project_version: str = build_info.project_version()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.cycle_status.is_running():
            self.cycle_status.start()

    @tasks.loop(minutes=CYCLE_MINS)
    async def cycle_status(self) -> None:
        while (
            self.index == self.lastindex
        ):  # basic shuffle, won't do something twice in a row.
            self.index = random.randint(0, len(self.statuses) - 1)
        await self.bot.change_presence(activity=self.statuses[self.index])
        self.lastindex = self.index

    @discord.slash_command(
        name="status",
        description="Changes status of the bot",
        guild_ids=[GUILD_ID],
    )
    async def status(
        self,
        ctx: discord.ApplicationContext,
        type: str = discord.Option(
            str, "What kind of status?", choices=["streaming", "default", "custom"]
        ),
        status: str = "",
    ) -> None:
        if not (config.is_bot_dev(ctx.author) or config.is_stream_team(ctx.author)):
            await ctx.respond(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if type == "streaming":
            self.cycle_status.stop()
            if status == "":
                status = "NU Esports is live!"
            await self.bot.change_presence(
                activity=discord.Streaming(name=status, url=STREAM_LINK)
            )
            await ctx.respond("🚨 Showing stream in status!", ephemeral=True)

        elif type == "custom":
            self.cycle_status.stop()
            await self.bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.playing, name=status
                )
            )
            if status == "":
                await ctx.respond("🔮 Cleared status!", ephemeral=True)
            else:
                await ctx.respond(f'📝 Changed status to "{status}"!', ephemeral=True)

        else:  # default
            try:
                self.cycle_status.start()
            except RuntimeError:
                traceback.print_exc()
                await ctx.respond("❌ Something went wrong. Try again!", ephemeral=True)
                return
            await ctx.respond("🤖 Resumed cycling status!", ephemeral=True)

    @discord.slash_command(
        name="version",
        description="Shows which build of the bot is running",
        guild_ids=[GUILD_ID],
    )
    async def version(self, ctx: discord.ApplicationContext) -> None:
        await ctx.respond(
            build_info.format_version(
                self.project_version, self.commit, self.commit_date, self.started_at
            ),
            ephemeral=True,
        )


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Presence(bot))
