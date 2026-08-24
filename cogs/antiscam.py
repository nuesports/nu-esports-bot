"""Catches giveaway scams -- "giving away my old PS5, message me on WhatsApp" -- posted by
throwaway accounts, and holds the poster until a human looks at it.

Scores each signal rather than tripping on any one keyword, because a real member offering a
textbook says some of the same words a scammer does. Only the account age and the combination
separate them, so both feed the score. Rules and weights live in data/antiscam.yaml.

The listener stays thin and the judgement lives in plain functions below it, which is what
makes any of this testable without a Discord connection.
"""

import datetime
import re

import discord
from discord.ext import commands

from utils import config


# Counted as offplatform_contact: a scam's whole purpose is moving you somewhere Discord
# can't see. Deliberately conservative -- PHONE_RE wants separators or a leading +, so a
# bare year like "2026" or a price like "150" doesn't match.
PHONE_RE = re.compile(r"(\+\d[\d\s().-]{8,}\d)|(\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b)")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
MASS_MENTION_RE = re.compile(r"@(everyone|here)")

# Discord's hard ceiling on a timeout, and the ceiling on how much history a ban can purge.
MAX_TIMEOUT_DAYS = 28
MAX_BAN_DELETE_DAYS = 7


def account_age_days(created_at: datetime.datetime, now: datetime.datetime) -> float:
    """Age of the Discord account itself, not of their membership here.

    `now` is passed in rather than read from the clock so the scoring stays testable."""
    return (now - created_at).total_seconds() / 86400


def age_weight(age_days: float, bands: list[dict]) -> int:
    """Score contribution from account age alone: the weight of the first band the age falls
    under, or 0 if it is older than every band.

    That 0 is also what makes a member unscannable, so the widest band doubles as the overall
    cutoff instead of the same threshold being configured in two places."""
    for band in bands:
        if age_days < band["max_days"]:
            return band["weight"]
    return 0


def _phrase_hits(lowered: str, phrases: list[str]) -> bool:
    return any(phrase in lowered for phrase in phrases)


def score_message(content: str, has_attachment: bool, age_days: float, rules: dict) -> tuple[int, list[str]]:
    """Total weight of every signal that fires, plus the names of the ones that did.

    The reasons come back alongside the number so the staff alert can say *why* something was
    flagged, which is what makes a false positive diagnosable without re-running anything."""
    lowered = content.lower()
    weights = rules["weights"]
    phrases = rules["phrases"]

    score = 0
    reasons = []

    age = age_weight(age_days, rules["account_age_bands"])
    if age:
        score += age
        reasons.append(f"new account (+{age})")

    if _phrase_hits(lowered, phrases["giveaway_phrase"]):
        score += weights["giveaway_phrase"]
        reasons.append("giveaway wording")

    offplatform = _phrase_hits(lowered, phrases["offplatform_contact"]) or bool(
        PHONE_RE.search(content) or EMAIL_RE.search(content)
    )
    if offplatform:
        score += weights["offplatform_contact"]
        reasons.append("off-platform contact")

    if _phrase_hits(lowered, phrases["dm_solicitation"]):
        score += weights["dm_solicitation"]
        reasons.append("asks you to DM")

    if MASS_MENTION_RE.search(content):
        score += weights["mass_mention_text"]
        reasons.append("mass mention")

    if has_attachment:
        score += weights["attachment"]
        reasons.append("attachment")

    return score, reasons


def build_alert_embed(member: discord.Member, channel_name: str, content: str, score: int,
                      reasons: list[str], now: datetime.datetime) -> discord.Embed:
    """The member-info embed that carries the Allow/Ban buttons.

    Also repeats the message text, so staff still have something to judge if the forward above
    it renders empty for any reason."""
    age = account_age_days(member.created_at, now)
    embed = discord.Embed(
        title="Possible scam held for review",
        description=f"{member.mention} was timed out and their message deleted.",
        color=discord.Color.from_rgb(78, 42, 132),
    )
    embed.add_field(name="Member", value=f"{member.display_name}\n`{member.id}`", inline=True)
    embed.add_field(name="Account age", value=f"{age:.1f} days", inline=True)
    embed.add_field(name="Posted in", value=f"#{channel_name}", inline=True)
    embed.add_field(name="Score", value=f"**{score}** — {', '.join(reasons)}", inline=False)
    if content:
        excerpt = content if len(content) <= 1000 else content[:997] + "..."
        embed.add_field(name="Message", value=f">>> {excerpt}", inline=False)
    embed.set_footer(text="Allow clears the timeout. Ban also deletes their recent messages.")
    return embed


class ScamReviewView(discord.ui.View):
    """Allow/Ban on a held member. `timeout=None` so it stays usable for as long as the
    process lives -- a restart drops the buttons, and the embed carries the member id so
    staff can act by hand in that case."""

    def __init__(self, member: discord.Member, ban_delete_days: int):
        super().__init__(timeout=None)
        self.member = member
        self.ban_delete_days = ban_delete_days

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not (config.has_leadership(interaction.user) or config.is_bot_dev(interaction.user)):
            await interaction.response.send_message(
                "Only leadership can clear or ban a held member.", ephemeral=True
            )
            return False
        return True

    def _finish(self) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.success)
    async def allow(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        """Clear the timeout and let them talk again."""
        await self.member.timeout(None, reason=f"Scam hold cleared by {interaction.user}")
        self._finish()
        await interaction.response.edit_message(
            content=f"✅ Allowed by {interaction.user.mention} — timeout cleared.", view=self
        )

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger)
    async def ban(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        """Ban, and purge their recent history in the same call -- that is what cleans up
        anything they posted in other channels before being caught."""
        await self.member.guild.ban(
            self.member,
            delete_message_seconds=self.ban_delete_days * 86400,
            reason=f"Scam confirmed by {interaction.user}",
        )
        self._finish()
        await interaction.response.edit_message(
            content=f"🔨 Banned by {interaction.user.mention}.", view=self
        )


class AntiScam(commands.Cog):
    """Watches messages from young accounts and holds likely giveaway scams for staff."""

    def __init__(self, bot):
        self.bot = bot
        cfg = config.config["antiscam"]
        self.alert_channel_id = cfg["alert_channel"]
        self.staff_role_id = cfg["staff_role"]
        self.timeout_days = min(cfg["timeout_days"], MAX_TIMEOUT_DAYS)
        self.ban_delete_days = min(cfg["ban_delete_message_days"], MAX_BAN_DELETE_DAYS)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return

        rules = config.antiscam_data
        now = discord.utils.utcnow()
        age = account_age_days(message.author.created_at, now)

        # Older accounts are never scanned, so the regexes never run for the vast majority
        # of traffic.
        if not age_weight(age, rules["account_age_bands"]):
            return

        score, reasons = score_message(
            message.content, bool(message.attachments), age, rules
        )
        if score < rules["threshold"]:
            return

        await self.hold(message, score, reasons, now)

    async def hold(self, message, score: int, reasons: list[str], now) -> None:
        """Forward the message to staff, delete it, time the poster out, then post the
        review embed.

        Order matters. The forward has to happen while the original still exists, and the
        delete has to beat the timeout because the timeout is the slower call and the message
        being visible is the actual harm."""
        channel = self.bot.get_channel(self.alert_channel_id)
        if not channel:
            return

        staff_role = message.guild.get_role(self.staff_role_id)
        mentions = discord.AllowedMentions(
            everyone=False, users=False, roles=[staff_role] if staff_role else False
        )

        ping = staff_role.mention if staff_role else ""
        await channel.send(
            content=f"{ping} held a possible scam from {message.author.display_name}",
            reference=message.to_reference(type=discord.MessageReferenceType.forward),
            allowed_mentions=mentions,
        )

        await message.delete()

        await message.author.timeout(
            datetime.timedelta(days=self.timeout_days),
            reason=f"Possible giveaway scam (score {score}: {', '.join(reasons)})",
        )

        embed = build_alert_embed(
            message.author, message.channel.name, message.content, score, reasons, now
        )
        await channel.send(
            embed=embed,
            view=ScamReviewView(message.author, self.ban_delete_days),
            allowed_mentions=discord.AllowedMentions.none(),
        )


def setup(bot):
    bot.add_cog(AntiScam(bot))
