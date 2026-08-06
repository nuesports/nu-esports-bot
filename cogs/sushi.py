import discord
from discord.ext import commands

from utils import config

GUILD_ID = config.secrets["discord"]["guild_id"]


class Sushi(commands.Cog):
    """AYCE sushi counter -- in-memory, resets on restart, that's fine for this."""
    def __init__(self, bot):
        self.bot = bot
        self.count = 0

    def build_embed(self) -> discord.Embed:
        return discord.Embed(
            title="Sushi!",
            description=f"🍣 count: {self.count}",
            color=discord.Color.from_rgb(78, 42, 132),
        )

    @discord.slash_command(name="sushi", description="Show the sushi counter", guild_ids=[GUILD_ID])
    async def sushi(self, ctx: discord.ApplicationContext) -> None:
        await ctx.respond(embed=self.build_embed(), view=SushiView(self))

    @discord.slash_command(name="sushi-add", description="Add to the sushi counter", guild_ids=[GUILD_ID])
    async def sushi_add(
        self,
        ctx: discord.ApplicationContext,
        amount: discord.Option(int, description="How many to add (default 1)", default=1),
    ) -> None:
        self.count = max(0, self.count + amount)
        await ctx.respond(embed=self.build_embed(), view=SushiView(self))


class SushiView(discord.ui.View):
    """Add/Remove buttons for the sushi counter. No timeout."""
    def __init__(self, cog: Sushi) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Add", style=discord.ButtonStyle.success)
    async def add(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.cog.count += 1
        await interaction.response.edit_message(embed=self.cog.build_embed())

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger)
    async def remove(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.cog.count = max(0, self.cog.count - 1)
        await interaction.response.edit_message(embed=self.cog.build_embed())


def setup(bot):
    bot.add_cog(Sushi(bot))
