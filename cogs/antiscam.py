"""
Scam message filter;

Catches common scam messages via a holistic scoring system.
Flagged messages are sent to staff for review while
the suspected account is timed out.
"""

import datetime
import re
import traceback
from collections.abc import Callable
from typing import NamedTuple

import discord
from discord.ext import commands

from utils import config

PHONE_RE = re.compile(r"(\+\d[\d\s().-]{8,}\d)|(\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b)")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
MASS_MENTION_RE = re.compile(r"@(everyone|here)")

# Discord's hard ceiling on a timeout, and the ceiling on how much history a ban can purge.
MAX_TIMEOUT_DAYS = 28
MAX_BAN_DELETE_DAYS = 7

# How far back the sweep reads in each channel, and Discord's bulk-delete ceiling per call.
SWEEP_LIMIT = 200
BULK_DELETE_MAX = 100


class SweepResult(NamedTuple):
    """What the post-flag cleanup removed, for the staff embed to report."""

    deleted: int
    channels: list[str]
    with_files: int
    forwarded: bool = False


def account_age_days(created_at: datetime.datetime, now: datetime.datetime) -> float:
    """Age of the Discord account"""
    return (now - created_at).total_seconds() / 86400


def effective_age_days(
    member_id: int,
    created_at: datetime.datetime,
    now: datetime.datetime,
    overrides: dict,
) -> float:
    """Account age, unless a local-testing override pins it to something else"""
    if member_id in overrides:
        return overrides[member_id]
    return account_age_days(created_at, now)


def age_weight(age_days: float, bands: list[dict]) -> int:
    """Score contribution from account age alone"""
    for band in bands:
        if age_days < band["max_days"]:
            return band["weight"]
    return 0


def recent_attachment(
    messages: list[discord.Message],
    author_id: int,
    cutoff: datetime.datetime,
    exclude_id: int,
) -> bool:
    """True if the author posted a file in some *other* message since `cutoff`"""
    return any(
        message.attachments
        and message.id != exclude_id
        and message.guild is not None
        and message.author.id == author_id
        and message.created_at >= cutoff
        for message in messages
    )


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def normalize(content: str) -> str:
    """Lowercase, and fold the quote characters phones typcically insert down to plain ASCII"""
    return content.lower().replace("’", "'").replace("‘", "'")


def _phrase_hits(lowered: str, phrases: list[str]) -> bool:
    return any(phrase in lowered for phrase in phrases)


def _phrase_count(lowered: str, phrases: list[str]) -> int:
    return sum(1 for phrase in phrases if phrase in lowered)


def giveaway_weight(lowered: str, phrases: list[str], weights: dict) -> int:
    """Giveaway wording escalates with how many distinct phrases fire, capped"""
    count = _phrase_count(lowered, phrases)
    if not count:
        return 0
    escalated = (
        weights["giveaway_phrase"] + (count - 1) * weights["giveaway_phrase_each_extra"]
    )
    return min(escalated, weights["giveaway_phrase_max"])


def score_message(
    content: str, has_attachment: bool, age_days: float, rules: dict
) -> tuple[int, list[str]]:
    """Total weight of every signal that fires, plus the names of the ones that did.

    The reasons come back too so that staff can say *why* something was flagged"""
    lowered = normalize(content)
    weights = rules["weights"]
    phrases = rules["phrases"]

    score = 0
    reasons = []

    age = age_weight(age_days, rules["account_age_bands"])
    if age:
        score += age
        reasons.append(f"new account (+{age})")

    giveaway = giveaway_weight(lowered, phrases["giveaway_phrase"], weights)
    if giveaway:
        score += giveaway
        reasons.append(f"giveaway wording (+{giveaway})")

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


async def forward_newest_attachment(
    messages: list[discord.Message], alert_channel: discord.TextChannel | None
) -> bool:
    """Forward the newest of `messages` carrying a file, so one photo outlives the sweep."""
    if alert_channel is None:
        return False
    carrying = [message for message in messages if message.attachments]
    if not carrying:
        return False

    newest = max(carrying, key=lambda message: message.created_at)
    try:
        await alert_channel.send(
            reference=newest.to_reference(type=discord.MessageReferenceType.forward)
        )
    except discord.HTTPException:
        traceback.print_exc()
        return False
    return True


def build_alert_embed(
    member: discord.Member,
    channel: discord.TextChannel | discord.Thread,
    score: int,
    reasons: list[str],
    sweep: SweepResult | None = None,
    problems: list[str] | None = None,
    content: str | None = None,
) -> discord.Embed:
    """The member-info embed that carries the Allow/Ban buttons."""
    created = int(member.created_at.timestamp())
    embed = discord.Embed(
        title="Possible scam held for review",
        description=f"**Score {score}** — {', '.join(reasons)}",
        color=discord.Color.from_rgb(78, 42, 132),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="Member", value=f"{member.mention}\n`{member.id}`", inline=True
    )
    embed.add_field(
        name="Account age", value=f"<t:{created}:R>\n<t:{created}:D>", inline=True
    )
    embed.add_field(name="Posted in", value=channel.mention, inline=True)
    if content:
        excerpt = content if len(content) <= 1000 else content[:997] + "..."
        embed.add_field(name="Message", value=f">>> {excerpt}", inline=False)
    if sweep and (sweep.deleted or sweep.forwarded):
        # Only the newest photo comes across, so the count of messages that carried one is
        # still worth saying -- otherwise "1 more message" reads as nothing visual at all.
        lines = []
        if sweep.deleted:
            where = ", ".join(f"#{name}" for name in sweep.channels)
            files = (
                f" ({sweep.with_files} with attachments)" if sweep.with_files else ""
            )
            lines.append(f"{_plural(sweep.deleted, 'more message')} in {where}{files}")
        if sweep.forwarded:
            lines.append("Newest attachment forwarded below.")
        embed.add_field(name="Also removed", value="\n".join(lines), inline=False)
    if problems:
        embed.add_field(name="⚠ Needs a human", value="\n".join(problems), inline=False)
    return embed


class ScamReviewView(discord.ui.View):
    """Summary embed with allow/ban buttons"""

    def __init__(
        self,
        member: discord.Member,
        ban_delete_days: int,
        on_resolved: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.member = member
        self.ban_delete_days = ban_delete_days
        self.on_resolved = on_resolved

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not (
            config.has_leadership(interaction.user)
            or config.is_bot_dev(interaction.user)
        ):
            await interaction.response.send_message(
                "Only leadership can clear or ban a held member.", ephemeral=True
            )
            return False
        return True

    def _finish(self) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()
        if self.on_resolved:
            self.on_resolved()

    async def _may(
        self, interaction: discord.Interaction, permission: str, label: str
    ) -> bool:
        """Checks if the clicker has the implied prerequisite permissions"""
        if getattr(interaction.user.guild_permissions, permission, False):
            return True
        await interaction.response.send_message(
            f"You need **{label}** to do that.", ephemeral=True
        )
        return False

    async def _report_failure(
        self, interaction: discord.Interaction, what: str
    ) -> None:
        """Say what went wrong instead of leaving a bare "interaction failed"."""
        await interaction.response.send_message(
            f"Could not {what} — they may have left, or the bot may sit below them in the "
            f"role list. Nothing was changed.",
            ephemeral=True,
        )

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.success)
    async def allow(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        """Clear the timeout and let them talk again."""
        if not await self._may(interaction, "moderate_members", "Moderate Members"):
            return
        try:
            await self.member.timeout(
                None, reason=f"Scam hold cleared by {interaction.user}"
            )
        except discord.HTTPException as exc:
            traceback.print_exception(exc)
            await self._report_failure(interaction, "clear the timeout")
            return

        self._finish()
        await interaction.response.edit_message(
            content=f"✅ Allowed by {interaction.user.mention} — timeout cleared.",
            view=self,
        )

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger)
    async def ban(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        """Ban and purge recent history"""
        if not await self._may(interaction, "ban_members", "Ban Members"):
            return

        try:
            await self.member.timeout(
                None, reason=f"Timeout cleared before ban by {interaction.user}"
            )
        except discord.HTTPException as exc:
            traceback.print_exception(exc)
            await self._report_failure(interaction, "clear the timeout")
            return

        try:
            await self.member.guild.ban(
                self.member,
                delete_message_seconds=self.ban_delete_days * 86400,
                reason=f"Scam confirmed by {interaction.user}",
            )
        except discord.HTTPException as exc:
            traceback.print_exception(exc)
            await self._report_failure(interaction, "ban them")
            return

        self._finish()
        await interaction.response.edit_message(
            content=f"🔨 Banned by {interaction.user.mention}.", view=self
        )


class AntiScam(commands.Cog):
    """Watches messages from young accounts and holds likely giveaway scams for staff."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot: discord.Bot = bot
        cfg = config.config["antiscam"]
        self.alert_channel_id = cfg["alert_channel"]
        self.staff_role_id = cfg["staff_role"]
        self.timeout_days = min(cfg["timeout_days"], MAX_TIMEOUT_DAYS)
        self.ban_delete_days = min(cfg["ban_delete_message_days"], MAX_BAN_DELETE_DAYS)
        self.purge_window_minutes = cfg["purge_window_minutes"]
        self.exempt_staff = cfg.get("exempt_staff", True)
        # Local testing only, and .get() so it can be absent everywhere else.
        self.test_ages = cfg.get("test_account_ages") or {}
        if self.test_ages:
            print(
                f"[antiscam] TESTING: {len(self.test_ages)} account(s) have a faked age"
            )
        # Members with a case already open.
        self._held: set[int] = set()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if message.author.id in self._held:
            return

        if self.exempt_staff and (
            config.has_leadership(message.author) or config.is_bot_dev(message.author)
        ):
            return

        rules = config.antiscam_data
        now = discord.utils.utcnow()
        age = effective_age_days(
            message.author.id, message.author.created_at, now, self.test_ages
        )

        score, reasons = score_message(
            message.content, bool(message.attachments), age, rules
        )

        # Only walk the message cache when the point a separately-posted photo would add is
        # the difference between flagging and not. Every message in the server comes through
        # here, and there is no reason to scan the cache for the ones nowhere near the line.
        attachment_weight = rules["weights"]["attachment"]
        if (
            not message.attachments
            and score < rules["threshold"] <= score + attachment_weight
            and recent_attachment(
                self.bot.cached_messages,
                message.author.id,
                now - datetime.timedelta(minutes=self.purge_window_minutes),
                message.id,
            )
        ):
            score, reasons = score_message(message.content, True, age, rules)

        if score < rules["threshold"]:
            return

        await self.hold(message, score, reasons, now)

    async def hold(
        self,
        message: discord.Message,
        score: int,
        reasons: list[str],
        now: datetime.datetime,
    ) -> None:
        """Post the review embed, forward the message under it, delete it, time the poster
        out, sweep the rest of their last hour, then edit the outcome back into the embed.
        """
        channel = self.bot.get_channel(self.alert_channel_id)
        if not channel:
            return

        staff_role = message.guild.get_role(self.staff_role_id)
        mentions = discord.AllowedMentions(
            everyone=False, users=False, roles=[staff_role] if staff_role else False
        )

        ping = staff_role.mention if staff_role else ""
        try:
            alert = await channel.send(
                content=f"{ping} held a possible scam from {message.author.display_name}",
                embed=build_alert_embed(
                    message.author, message.channel, score, reasons
                ),
                view=ScamReviewView(
                    message.author,
                    self.ban_delete_days,
                    on_resolved=lambda: self._held.discard(message.author.id),
                ),
                allowed_mentions=mentions,
            )
        except discord.HTTPException as exc:
            traceback.print_exception(exc)
            return

        self._held.add(message.author.id)

        problems = []

        try:
            await channel.send(
                reference=message.to_reference(
                    type=discord.MessageReferenceType.forward
                )
            )
        except discord.HTTPException as exc:
            traceback.print_exception(exc)
            problems.append("Could not forward the message; the text is quoted below.")

        try:
            await message.delete()
        except discord.HTTPException as exc:
            traceback.print_exception(exc)
            problems.append("Could not delete the message; it may still be up.")

        try:
            await message.author.timeout_for(
                datetime.timedelta(days=self.timeout_days),
                reason=f"Possible giveaway scam (score {score}: {', '.join(reasons)})",
            )
        except discord.HTTPException as exc:
            traceback.print_exception(exc)
            problems.append(
                "**Could not time them out** — they can still post. Check the "
                "bot's role position, and note admins cannot be timed out."
            )

        cutoff = now - datetime.timedelta(minutes=self.purge_window_minutes)
        sweep = await self.sweep_recent(message.guild, message.author, cutoff, channel)

        # Only quote the text when the forward that was meant to carry it did not go out.
        fallback = message.content if any("forward" in p for p in problems) else None
        try:
            await alert.edit(
                embed=build_alert_embed(
                    message.author,
                    message.channel,
                    score,
                    reasons,
                    sweep,
                    problems,
                    fallback,
                )
            )
        except discord.HTTPException as exc:
            traceback.print_exception(exc)

    async def sweep_recent(
        self,
        guild: discord.Guild,
        author: discord.User | discord.Member,
        cutoff: datetime.datetime,
        alert_channel: discord.TextChannel | None = None,
    ) -> SweepResult:
        """Delete everything `author` posted since `cutoff`"""
        found = []
        for channel in [*guild.text_channels, *guild.threads]:
            perms = channel.permissions_for(guild.me)
            if not (perms.read_message_history and perms.manage_messages):
                continue
            try:
                stale = [
                    m
                    async for m in channel.history(after=cutoff, limit=SWEEP_LIMIT)
                    if m.author.id == author.id
                ]
            except discord.HTTPException:
                traceback.print_exc()
                continue
            if stale:
                found.append((channel, stale))

        forwarded = await forward_newest_attachment(
            [m for _, stale in found for m in stale], alert_channel
        )

        deleted = 0
        with_files = 0
        channels: list[str] = []

        for channel, stale in found:
            # Counted per batch as it lands, not once at the end: a channel busy enough to
            # need a second batch would otherwise report zero for everything it did delete
            # if that second call failed, and staff would be told the scam is still up.
            removed = []
            try:
                for start in range(0, len(stale), BULK_DELETE_MAX):
                    batch = stale[start : start + BULK_DELETE_MAX]
                    await channel.delete_messages(batch)
                    removed.extend(batch)
            except discord.HTTPException:
                traceback.print_exc()

            if not removed:
                continue
            deleted += len(removed)
            with_files += sum(1 for m in removed if m.attachments)
            channels.append(channel.name)

        return SweepResult(deleted, channels, with_files, forwarded)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(AntiScam(bot))
