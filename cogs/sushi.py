import discord
from discord.ext import commands

from utils import config

GUILD_ID = config.secrets["discord"]["guild_id"]


class Sushi(commands.Cog):
    """AYCE sushi leaderboard, per channel -- in-memory, resets on restart, that's fine for this."""
    def __init__(self, bot: discord.Bot) -> None:
        self.bot: discord.Bot = bot
        self.boards: dict[int, dict[int, int]] = {}  # channel_id -> {user_id: count}
        self.board_messages: dict[int, discord.Message] = {}  # channel_id -> the live leaderboard message

    def build_embed(self, channel_id: int) -> discord.Embed:
        board = self.boards.get(channel_id, {})
        ranked = sorted(board.items(), key=lambda item: item[1], reverse=True)
        description = "\n".join(
            f"{i}. <@{user_id}> — {count}" for i, (user_id, count) in enumerate(ranked, start=1)
        ) if ranked else "nobody's eaten any sushi yet"
        return discord.Embed(title="🍣 Sushi Leaderboard", description=description, color=discord.Color.from_rgb(78, 42, 132))

    async def repost_board(self, channel: discord.abc.Messageable) -> None:
        """Deletes the channel's current leaderboard message (if any) and sends a fresh
        one -- keeps it pinned to the bottom of the channel instead of getting buried."""
        old_message = self.board_messages.get(channel.id)
        if old_message:
            try:
                await old_message.delete()
            except discord.NotFound:
                pass
        message = await channel.send(embed=self.build_embed(channel.id), view=SushiView(self))
        self.board_messages[channel.id] = message

    @discord.slash_command(name="sushi", description="Show the sushi leaderboard", guild_ids=[GUILD_ID])
    async def sushi(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        await self.repost_board(ctx.channel)
        await ctx.followup.send("🍣", ephemeral=True)


class SushiView(discord.ui.View):
    """Add/Remove buttons for the sushi leaderboard. No timeout -- AYCE dinners run long."""
    def __init__(self, cog: Sushi) -> None:
        super().__init__(timeout=None)
        self.cog: Sushi = cog

    @discord.ui.button(label="Add", style=discord.ButtonStyle.success, emoji="🍣")
    async def add(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        board = self.cog.boards.setdefault(interaction.channel_id, {})
        board[interaction.user.id] = board.get(interaction.user.id, 0) + 1
        await self.cog.repost_board(interaction.channel)

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger)
    async def remove(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        board = self.cog.boards.setdefault(interaction.channel_id, {})
        board[interaction.user.id] = max(0, board.get(interaction.user.id, 0) - 1)
        await self.cog.repost_board(interaction.channel)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Sushi(bot))
