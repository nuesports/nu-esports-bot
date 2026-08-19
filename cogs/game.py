import asyncio
import contextlib

import discord
from discord.ext import commands

from utils import config

GUILD_ID = config.secrets["discord"]["guild_id"]


class Game(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    game = discord.SlashCommandGroup("game", "game-related utils")

    @game.command(name="stack", description="any stackas", guild_ids=[GUILD_ID])
    async def stack(
        self,
        ctx,
        name: discord.Option(
            str,
            name="name",
            description="Name of the stack",
            default="",
        ),
        size: discord.Option(
            int,
            name="size",
            description="Number of stackas (default 5)",
            default=5,
        ),
    ):
        # We don't need 1 or less people in a stack
        size = max(size, 2)
        # We don't need more than 10 people in a stack. If we do, jump me
        size = min(size, 10)

        if name == "":
            name = f"{ctx.author.display_name}'s stack"
        name += f" [{size}]"

        embed = discord.Embed(
            title=name,
            color=discord.Color.from_rgb(78, 42, 132),
        )
        embed.add_field(
            name="".join([":white_medium_square:" for _ in range(size)]),
            value="empty :/",
        )
        view = GameStackView(embed, size)
        await ctx.respond(embed=embed, view=view)
        # Re-fetch as a normal message so later edits and deletes go out on the bot's
        # token, not the interaction webhook, which expires 15 minutes in -- well short
        # of the 20 minutes the view stays alive for.
        sent = await ctx.interaction.original_response()
        view.current_message = await ctx.channel.fetch_message(sent.id)


class GameStackView(discord.ui.View):
    def __init__(self, embed, size):
        super().__init__(timeout=1200)
        self.embed = embed
        self.joined = {}
        self.pinged = False
        self.stack_size = size
        # The one live copy of the stack, tracked here rather than on discord.ui.View's
        # own .message -- pycord reassigns that to interaction.message on every click of
        # every button, so a bump sitting on an await reads whichever copy was clicked
        # last and deletes that one instead of its own.
        self.current_message: discord.Message | None = None
        self.bump_lock = asyncio.Lock()

    def update_embed(self):
        # Title:
        # - Green square: Person joined under limit
        # - Yellow square: Person joined over stack size
        # - White square: Empty slot
        num_joined = len(self.joined)
        name = "".join(
            [
                ":green_square:" if i < self.stack_size else ":yellow_square:"
                for i in range(num_joined)
            ]
        )
        if num_joined < self.stack_size:
            name += "".join(
                [":white_medium_square:" for _ in range(self.stack_size - num_joined)]
            )

        # Value: display name of every user
        value = (
            "\n".join(user.mention for user in self.joined.values())
            if self.joined
            else "empty :/"
        )

        self.embed.remove_field(0)
        self.embed.add_field(name=name, value=value)

    async def on_timeout(self):
        self.disable_all_items()
        await self.message.edit(view=self)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.green)
    async def join_callback(self, button, interaction):
        self.joined[interaction.user.id] = interaction.user
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed)

        if not self.pinged and len(self.joined) >= self.stack_size:
            self.pinged = True
            await interaction.followup.send(
                " ".join(user.mention for user in self.joined.values())
            )

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.red)
    async def leave_callback(self, button, interaction):
        if interaction.user.id in self.joined:
            self.joined.pop(interaction.user.id)
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed)

    @discord.ui.button(label="Bump!", style=discord.ButtonStyle.grey)
    async def refresh_callback(self, button, interaction):
        """Repost the stack at the bottom of the channel.

        Answers the interaction before deleting anything: a delete is a round trip on
        Discord's per-channel delete bucket, and spamming the button makes it sleep there
        past the 3 second response deadline, which left the old stack deleted and the
        replacement unsendable. Sending first also means a failure leaves the stack
        standing instead of vanishing.
        """
        if (
            self.current_message is not None
            and interaction.message.id != self.current_message.id
        ):
            await interaction.response.send_message(
                "That stack's already been bumped -- scroll down!", ephemeral=True
            )
            return

        # Turned away rather than queued: waiting on the bump in flight would burn this
        # interaction's own 3 seconds. Nothing awaits between the check and the acquire,
        # so a second click can't slip past it.
        if self.bump_lock.locked():
            await interaction.response.send_message(
                "Already bumping that stack, hang on!", ephemeral=True
            )
            return

        async with self.bump_lock:
            old_message = self.current_message
            await interaction.response.send_message(embed=self.embed, view=self)
            sent = await interaction.original_response()
            self.current_message = await interaction.channel.fetch_message(sent.id)

            if old_message is None:
                return
            try:
                await old_message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                # Couldn't take the old copy down, so strip its buttons instead -- an
                # orphan that still dispatches would bump a stack that isn't there.
                with contextlib.suppress(discord.HTTPException):
                    await old_message.edit(view=None)


def setup(bot):
    bot.add_cog(Game(bot))
