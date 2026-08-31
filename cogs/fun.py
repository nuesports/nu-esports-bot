import asyncio
import random
from typing import Any

import discord
from discord.ext import commands

from utils import config

GUILD_ID = config.secrets["discord"]["guild_id"]
TYST_STICKER_ID = config.config["fun"]["stickers"]["TYST"]


class Fun(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot: discord.Bot = bot
        # Track active mute tasks and original permissions for Hannah
        self.hannah_mute_state: dict[str, Any] = {
            "text_unmute_task": None,
            "voice_unmute_task": None,
            "original_text_permissions": {},  # {channel_id: send_messages_value}
        }

    @discord.slash_command(
        name="mutehannah",
        description="Mutes Hannah for 3 minutes in text and voice",
        guild_ids=[GUILD_ID],
    )
    async def mutehannah(self, ctx: discord.ApplicationContext) -> None:
        # Check if the user has the required role
        required_role_id = config.config["fun"]["hannah-haters"]
        if not ctx.author.get_role(required_role_id):
            await ctx.respond(
                "You don't have permission to use this command!", ephemeral=True
            )
            return

        # Defer the response to avoid timeout (processing takes time)
        await ctx.defer()

        # Cancel any existing mute tasks to prevent conflicts
        if self.hannah_mute_state["text_unmute_task"]:
            self.hannah_mute_state["text_unmute_task"].cancel()
            self.hannah_mute_state["text_unmute_task"] = None
        if self.hannah_mute_state["voice_unmute_task"]:
            self.hannah_mute_state["voice_unmute_task"].cancel()
            self.hannah_mute_state["voice_unmute_task"] = None

        # Get the target user ID from config
        target_user_id = config.config["fun"]["hannah"]

        # Fetch the member
        try:
            member = await ctx.guild.fetch_member(target_user_id)
        except discord.NotFound:
            await ctx.respond("Hannah is not in this server!")
            return
        except discord.HTTPException:
            await ctx.respond("Failed to fetch Hannah from the server!")
            return

        # Deny send message permissions in all text channels
        text_channels = [
            channel
            for channel in ctx.guild.channels
            if isinstance(channel, discord.TextChannel)
        ]

        # Clear and store original permissions
        self.hannah_mute_state["original_text_permissions"] = {}
        for channel in text_channels:
            try:
                overwrite = channel.overwrites_for(member)
                # Store the original value (None, True, or False)
                self.hannah_mute_state["original_text_permissions"][channel.id] = (
                    overwrite.send_messages
                )
                overwrite.send_messages = False
                await channel.set_permissions(
                    member, overwrite=overwrite, reason="Muted by /mutehannah command"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass  # Skip channels where we don't have permission

        # Schedule permission restore after 3 minutes
        async def restore_text_permissions() -> None:
            try:
                await asyncio.sleep(180)
                for channel in text_channels:
                    try:
                        # Get the original permission value
                        original_perm = self.hannah_mute_state[
                            "original_text_permissions"
                        ].get(channel.id)
                        overwrite = channel.overwrites_for(member)
                        overwrite.send_messages = original_perm

                        # If the overwrite is now empty, remove it entirely
                        if overwrite.is_empty():
                            await channel.set_permissions(
                                member,
                                overwrite=None,
                                reason="Auto-unmute after 3 minutes",
                            )
                        else:
                            await channel.set_permissions(
                                member,
                                overwrite=overwrite,
                                reason="Auto-unmute after 3 minutes",
                            )
                    except (discord.Forbidden, discord.HTTPException):
                        pass  # Silently fail if we can't restore

                # Clear the stored permissions and task reference
                self.hannah_mute_state["original_text_permissions"] = {}
                self.hannah_mute_state["text_unmute_task"] = None
            except asyncio.CancelledError:
                # Task was cancelled, don't restore permissions
                pass

        # Run the restore task in the background and store reference
        self.hannah_mute_state["text_unmute_task"] = asyncio.create_task(
            restore_text_permissions()
        )

        # Mute in voice if they're in a voice channel
        voice_muted = False
        if member.voice and member.voice.channel:
            try:
                await member.edit(mute=True, reason="Muted by /mutehannah command")
                voice_muted = True

                # Schedule unmute after 3 minutes
                async def unmute_after_delay() -> None:
                    try:
                        await asyncio.sleep(180)
                        try:
                            # Check if member is still in voice
                            if member.voice and member.voice.channel:
                                await member.edit(
                                    mute=False, reason="Auto-unmute after 3 minutes"
                                )
                        except (discord.Forbidden, discord.HTTPException):
                            pass  # Silently fail if we can't unmute

                        # Clear task reference
                        self.hannah_mute_state["voice_unmute_task"] = None
                    except asyncio.CancelledError:
                        # Task was cancelled, don't unmute
                        pass

                # Run the unmute task in the background and store reference
                self.hannah_mute_state["voice_unmute_task"] = asyncio.create_task(
                    unmute_after_delay()
                )
            except discord.Forbidden:
                await ctx.respond(
                    "Hannah has been muted in text for 3 minutes, but I don't have permission to voice mute!"
                )
                return
            except discord.HTTPException:
                pass  # Voice mute failed, but timeout succeeded

        # Send confirmation message
        if voice_muted:
            await ctx.respond(
                "Hannah has been muted for 3 minutes in both text and voice! 🤫"
            )
        else:
            await ctx.respond(
                "Hannah has been muted for 3 minutes in text channels! 🤫"
            )

    @discord.slash_command(
        name="unmutehannah",
        description="Immediately unmutes Hannah (removes all restrictions)",
        guild_ids=[GUILD_ID],
    )
    async def unmutehannah(self, ctx: discord.ApplicationContext) -> None:
        # Check if the user has the required role
        required_role_id = config.config["fun"]["hannah-haters"]
        if not ctx.author.get_role(required_role_id):
            await ctx.respond(
                "You don't have permission to use this command!", ephemeral=True
            )
            return

        # Defer the response to avoid timeout
        await ctx.defer()

        # Cancel any existing mute tasks
        if self.hannah_mute_state["text_unmute_task"]:
            self.hannah_mute_state["text_unmute_task"].cancel()
            self.hannah_mute_state["text_unmute_task"] = None
        if self.hannah_mute_state["voice_unmute_task"]:
            self.hannah_mute_state["voice_unmute_task"].cancel()
            self.hannah_mute_state["voice_unmute_task"] = None

        # Get the target user ID from config
        target_user_id = config.config["fun"]["hannah"]

        # Fetch the member
        try:
            member = await ctx.guild.fetch_member(target_user_id)
        except discord.NotFound:
            await ctx.respond("Hannah is not in this server!")
            return
        except discord.HTTPException:
            await ctx.respond("Failed to fetch Hannah from the server!")
            return

        # Restore original send message permissions in all text channels
        text_channels = [
            channel
            for channel in ctx.guild.channels
            if isinstance(channel, discord.TextChannel)
        ]

        text_unmuted = False
        for channel in text_channels:
            try:
                # Get the original permission value if we stored it
                original_perm = self.hannah_mute_state["original_text_permissions"].get(
                    channel.id
                )
                overwrite = channel.overwrites_for(member)

                # Restore to original value (could be None, True, or False)
                overwrite.send_messages = original_perm

                # If the overwrite is now empty, remove it entirely
                if overwrite.is_empty():
                    await channel.set_permissions(
                        member,
                        overwrite=None,
                        reason="Unmuted by /unmutehannah command",
                    )
                else:
                    await channel.set_permissions(
                        member,
                        overwrite=overwrite,
                        reason="Unmuted by /unmutehannah command",
                    )
                text_unmuted = True
            except (discord.Forbidden, discord.HTTPException):
                pass  # Skip channels where we don't have permission

        # Clear the stored permissions
        self.hannah_mute_state["original_text_permissions"] = {}

        # Unmute in voice if they're in a voice channel
        voice_unmuted = False
        if member.voice and member.voice.channel:
            try:
                await member.edit(mute=False, reason="Unmuted by /unmutehannah command")
                voice_unmuted = True
            except (discord.Forbidden, discord.HTTPException):
                pass  # Silently fail if we can't unmute

        # Send confirmation message
        if text_unmuted and voice_unmuted:
            await ctx.respond("Hannah has been fully unmuted! 🔊")
        elif text_unmuted:
            await ctx.respond("Hannah has been unmuted in text channels! 🔊")
        elif voice_unmuted:
            await ctx.respond("Hannah has been unmuted in voice! 🔊")
        else:
            await ctx.respond("Hannah was not muted or I don't have permissions!")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.bot.user:
            return

        if chess_emoji := chess(self, message):
            await message.add_reaction(chess_emoji)

        # ty_stan also returns False when nothing matched, so narrow to the
        # reply-worthy case rather than testing truthiness.
        tyst_reply = await ty_stan(message)
        if isinstance(tyst_reply, str):
            await message.reply(tyst_reply)

        if osu_reply := i_love_osu(message):
            await message.reply(osu_reply)

        if lord_reply := oh_lord(message):
            await message.reply(lord_reply)

        if special_emojis := special_interactions(message):
            for emoji in special_emojis:
                await message.add_reaction(emoji)


def chess(cog: Fun, message: discord.Message) -> str | None:
    """Reacts with a random chess-piece emoji when the bot is @mentioned.

    Plain function, not a cog method -- takes `cog` explicitly just to reach
    `cog.bot.user`. Called as chess(self, message) from on_message."""
    if cog.bot.user.mentioned_in(message):
        if message.mention_everyone:
            return None

        chess_emojis = config.config["fun"]["chess_emojis"]
        emoji, id = random.choice(list(chess_emojis.items()))
        output = f"<:{emoji}:{id}>"
        return output
    return None


async def ty_stan(message: discord.Message) -> str | bool | None:
    """Returns a string for on_message to reply with, or None/False if there's
    nothing more to do -- including when a sticker was found, since that branch
    already sends its own reply and shouldn't also get echoed by the caller."""
    lower_content = message.content.lower()
    if random.randint(1, 100) <= 10 and (
        "thank you shannon tan" in lower_content or "tyst" in lower_content
    ):
        sticker = discord.utils.get(message.guild.stickers, id=TYST_STICKER_ID)
        if sticker is not None:
            await message.reply(
                "THANK YOU SHANNON TAN THANK YOU SHANNON TAN", stickers=[sticker]
            )
            return None
        return "THANK YOU SHANNON TAN"
    return False


def i_love_osu(message: discord.Message) -> str | None:
    lower_content = message.content.lower()
    if "i love osu" in lower_content:
        output = "Osu 😻"
        return output
    return None


def oh_lord(message: discord.Message) -> str | None:
    lower_content = message.content.lower()
    if random.randint(1, 100) <= 10 and "oh lord" in lower_content:
        output = "https://www.youtube.com/watch?v=YsoP6bjADic"
        return output
    return None


def special_interactions(message: discord.Message) -> list[str] | None:
    special_users = config.config["fun"]["special_users"]
    if (
        special_users
        and random.randint(1, 100) <= 15
        and message.author.id in special_users
    ):
        output = [random.choice(special_users[message.author.id])]
        return output
    return None


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Fun(bot))
