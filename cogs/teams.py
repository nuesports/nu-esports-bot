import discord
from discord.ext import commands

from utils import config

GUILD_ID = config.secrets["discord"]["guild_id"]


class Teams(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot: discord.Bot = bot


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Teams(bot))
