"""Catches giveaway scams -- "giving away my old PS5, message me on WhatsApp" -- posted by
throwaway accounts, and holds the poster until a human looks at it.

Scores each signal rather than tripping on any one keyword, because a real member offering a
textbook says some of the same words a scammer does. Only the account age and the combination
separate them, so both feed the score. Rules and weights live in data/antiscam.yaml.

Scams routinely split the pitch and the photos across two messages, so a flag sweeps
everything the poster put up in the last hour rather than only the message that scored, and
an attachment sitting in one of those other messages still counts toward the score.

The listener stays thin and the judgement lives in plain functions below it, which is what
makes any of this testable without a Discord connection.
"""

import datetime
import re
import traceback
from typing import NamedTuple

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

# How far back the sweep reads in each channel, and Discord's bulk-delete ceiling per call.
SWEEP_LIMIT = 200
BULK_DELETE_MAX = 100


class SweepResult(NamedTuple):
    """What the post-flag cleanup removed, for the staff embed to report."""

    deleted: int
    channels: list[str]
    with_files: int


def account_age_days(created_at: datetime.datetime, now: datetime.datetime) -> float:
    """Age of the Discord account itself, not of their membership here.

    `now` is passed in rather than read from the clock so the scoring stays testable."""
    return (now - created_at).total_seconds() / 86400


def effective_age_days(member_id: int, created_at: datetime.datetime,
                       now: datetime.datetime, overrides: dict) -> float:
    """Account age, unless a local-testing override pins it to something else.

    Nobody's real account is three days old on demand, so there is no way to exercise this
    end to end without lying about an age somewhere. `test_account_ages` only ever exists in
    a developer's own config.yaml, which is gitignored -- it cannot follow the bot to
    production, and the cog prints a warning at load if it is set."""
    if member_id in overrides:
        return overrides[member_id]
    return account_age_days(created_at, now)


def age_weight(age_days: float, bands: list[dict]) -> int:
    """Score contribution from account age alone: the weight of the first band the age falls
    under, or 0 if it is older than every band.

    0 means age adds nothing, not that the member is exempt. A giveaway advertisement is a
    scam whoever posts it, so every message is scored and age is only ever evidence."""
    for band in bands:
        if age_days < band["max_days"]:
            return band["weight"]
    return 0


def recent_attachment(messages, author_id: int, cutoff, exclude_id: int) -> bool:
    """True if the author posted a file in some *other* guild message since `cutoff`.

    Splitting the pitch and the photos across two posts would otherwise cost a scammer the
    attachment weight, and the text on its own lands right on the threshold. Reads the
    message cache the bot already keeps, so this costs no API call in the message path."""
    return any(
        message.attachments
        and message.id != exclude_id
        and message.guild is not None
        and message.author.id == author_id
        and message.created_at >= cutoff
        for message in messages
    )


def normalise(content: str) -> str:
    """Lowercase, and fold the quote characters phones insert down to plain ASCII.

    These posts are written on a phone or pasted out of one, so "who's" arrives as
    "who’s" and never matches a phrase list spelled with a straight apostrophe. Silent
    misses, and the sort that only show up once a scam has already gone unflagged."""
    return content.lower().replace("’", "'").replace("‘", "'")


def _phrase_hits(lowered: str, phrases: list[str]) -> bool:
    return any(phrase in lowered for phrase in phrases)


def _phrase_count(lowered: str, phrases: list[str]) -> int:
    return sum(1 for phrase in phrases if phrase in lowered)


def giveaway_weight(lowered: str, phrases: list[str], weights: dict) -> int:
    """Giveaway wording escalates with how many distinct phrases fire, capped.

    One of these phrases is something a real member says. Five of them stacked in one post
    is boilerplate, and counting the category once threw that difference away -- an obvious
    scam from an older account came out level with an honest offer from the same account."""
    count = _phrase_count(lowered, phrases)
    if not count:
        return 0
    escalated = weights["giveaway_phrase"] + (count - 1) * weights["giveaway_phrase_each_extra"]
    return min(escalated, weights["giveaway_phrase_max"])


def score_message(content: str, has_attachment: bool, age_days: float, rules: dict) -> tuple[int, list[str]]:
    """Total weight of every signal that fires, plus the names of the ones that did.

    The reasons come back alongside the number so the staff alert can say *why* something was
    flagged, which is what makes a false positive diagnosable without re-running anything."""
    lowered = normalise(content)
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


def build_alert_embed(member: discord.Member, channel, score: int, reasons: list[str],
                      sweep: SweepResult | None = None,
                      problems: list[str] | None = None,
                      content: str | None = None) -> discord.Embed:
    """The member-info embed that carries the Allow/Ban buttons.

    Carries no copy of the message: the forward under it is the message, and repeating the
    text just made staff read the scam twice. `content` is the exception, passed only when
    the forward failed and there would otherwise be nothing to judge.

    Account age is a Discord timestamp rather than a computed number of days, so it renders
    in the reader's own locale and stays right however long the alert sits unread."""
    created = int(member.created_at.timestamp())
    embed = discord.Embed(
        title="Possible scam held for review",
        description=f"**Score {score}** — {', '.join(reasons)}",
        color=discord.Color.from_rgb(78, 42, 132),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Member", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="Account age", value=f"<t:{created}:R>\n<t:{created}:D>", inline=True)
    embed.add_field(name="Posted in", value=channel.mention, inline=True)
    if content:
        excerpt = content if len(content) <= 1000 else content[:997] + "..."
        embed.add_field(name="Message", value=f">>> {excerpt}", inline=False)
    if sweep and sweep.deleted:
        # The swept messages aren't forwarded, so this line is the only thing telling staff
        # a photo existed at all -- and the photo is the half of the scam that isn't text.
        where = ", ".join(f"#{name}" for name in sweep.channels)
        files = f" ({sweep.with_files} with attachments)" if sweep.with_files else ""
        plural = "" if sweep.deleted == 1 else "s"
        embed.add_field(
            name="Also removed",
            value=f"{sweep.deleted} more message{plural} in {where}{files}",
            inline=False,
        )
    if problems:
        # Discord refuses to time out an administrator, and role hierarchy blocks plenty of
        # other cases. Silently half-acting is the worst outcome: staff would assume the
        # member was muted when they are still talking.
        embed.add_field(name="⚠ Needs a human", value="\n".join(problems), inline=False)
    return embed


class ScamReviewView(discord.ui.View):
    """Allow/Ban on a held member. `timeout=None` so it stays usable for as long as the
    process lives -- a restart drops the buttons, and the embed carries the member id so
    staff can act by hand in that case."""

    def __init__(self, member: discord.Member, ban_delete_days: int, on_resolved=None):
        super().__init__(timeout=None)
        self.member = member
        self.ban_delete_days = ban_delete_days
        self.on_resolved = on_resolved

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
        if self.on_resolved:
            self.on_resolved()

    async def _may(self, interaction: discord.Interaction, permission: str, label: str) -> bool:
        """Whether the clicker holds the Discord permission the action actually needs.

        Being leadership gets you as far as the buttons; it does not stand in for the
        permission itself. Someone who cannot ban by hand should not ban through the bot,
        and the two are different permissions, so this is per button rather than on
        interaction_check. Administrators pass automatically -- Discord reports every
        permission as granted for them."""
        if getattr(interaction.user.guild_permissions, permission, False):
            return True
        await interaction.response.send_message(
            f"You need **{label}** to do that.", ephemeral=True
        )
        return False

    async def _report_failure(self, interaction: discord.Interaction, what: str) -> None:
        """Say what went wrong instead of leaving a bare "interaction failed".

        The member may have left, or the hierarchy may have moved since the hold. Without
        this the buttons stay lit and staff have no idea whether anything happened."""
        await interaction.response.send_message(
            f"Could not {what} — they may have left, or the bot may sit below them in the "
            f"role list. Nothing was changed.",
            ephemeral=True,
        )

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.success)
    async def allow(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        """Clear the timeout and let them talk again."""
        if not await self._may(interaction, "moderate_members", "Moderate Members"):
            return
        try:
            await self.member.timeout(None, reason=f"Scam hold cleared by {interaction.user}")
        except Exception as exc:
            traceback.print_exception(exc)
            await self._report_failure(interaction, "clear the timeout")
            return

        self._finish()
        await interaction.response.edit_message(
            content=f"✅ Allowed by {interaction.user.mention} — timeout cleared.", view=self
        )

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger)
    async def ban(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        """Ban, and purge their recent history in the same call -- that is what cleans up
        anything they posted in other channels before being caught."""
        if not await self._may(interaction, "ban_members", "Ban Members"):
            return
        try:
            await self.member.guild.ban(
                self.member,
                delete_message_seconds=self.ban_delete_days * 86400,
                reason=f"Scam confirmed by {interaction.user}",
            )
        except Exception as exc:
            traceback.print_exception(exc)
            await self._report_failure(interaction, "ban them")
            return

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
        self.purge_window_minutes = cfg["purge_window_minutes"]
        # Defaults on: staff run real giveaways. Off is for testing from your own account.
        self.exempt_staff = cfg.get("exempt_staff", True)
        # Local testing only, and .get() so it can be absent everywhere else.
        self.test_ages = cfg.get("test_account_ages") or {}
        if self.test_ages:
            print(f"[antiscam] TESTING: {len(self.test_ages)} account(s) have a faked age")
        # Members with a case already open. Without this a scammer who posts twice gets two
        # forwards, two embeds and two whole sweeps. Process-local, like the buttons.
        self._held: set[int] = set()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        if message.author.id in self._held:
            return
        # The people who can clear a hold are not held. Otherwise an official club giveaway
        # posted by leadership would delete itself and mute whoever ran it. Turn this off in
        # a dev guild to test the flow from your own account.
        if self.exempt_staff and (
            config.has_leadership(message.author) or config.is_bot_dev(message.author)
        ):
            return

        rules = config.antiscam_data
        now = discord.utils.utcnow()
        age = effective_age_days(
            message.author.id, message.author.created_at, now, self.test_ages
        )

        score, reasons = score_message(message.content, bool(message.attachments), age, rules)

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

    async def hold(self, message, score: int, reasons: list[str], now) -> None:
        """Post the review embed, forward the message under it, delete it, time the poster
        out, sweep the rest of their last hour, then edit the outcome back into the embed.

        The embed leads so the ping arrives on the message staff act on, with the forward
        reading as its evidence rather than the other way round. That means the alert is
        posted before the sweep has run, so what the sweep found is edited in afterwards.

        The forward still has to happen while the original exists, and the timeout goes
        ahead of the sweep: the sweep is many round-trips and an un-muted scammer can keep
        posting right through it."""
        channel = self.bot.get_channel(self.alert_channel_id)
        if not channel:
            return

        staff_role = message.guild.get_role(self.staff_role_id)
        mentions = discord.AllowedMentions(
            everyone=False, users=False, roles=[staff_role] if staff_role else False
        )

        # If this fails there is nowhere to report the case, which is the same position as
        # having no alert channel at all: leave the message up rather than delete it into
        # silence. Crucially the member is only marked held once it has landed -- marking
        # first and then raising would ignore every later scam from them, forever.
        ping = staff_role.mention if staff_role else ""
        try:
            alert = await channel.send(
                content=f"{ping} held a possible scam from {message.author.display_name}",
                embed=build_alert_embed(message.author, message.channel, score, reasons),
                view=ScamReviewView(
                    message.author,
                    self.ban_delete_days,
                    on_resolved=lambda: self._held.discard(message.author.id),
                ),
                allowed_mentions=mentions,
            )
        except Exception as exc:
            traceback.print_exception(exc)
            return

        self._held.add(message.author.id)

        # None of these three is allowed to abort the rest. Half-acting -- deleting the
        # message and then bailing before staff are told -- is worse than any one failure.
        problems = []

        # A forward carries no content of its own: Discord rejects the pair outright with
        # 400 error 160011, which is why the ping rides on the embed above.
        try:
            await channel.send(
                reference=message.to_reference(type=discord.MessageReferenceType.forward)
            )
        except Exception as exc:
            traceback.print_exception(exc)
            problems.append("Could not forward the message; the text is quoted below.")

        try:
            await message.delete()
        except Exception as exc:
            traceback.print_exception(exc)
            problems.append("Could not delete the message; it may still be up.")

        try:
            # timeout_for() takes the duration; timeout() wants an absolute datetime.
            await message.author.timeout_for(
                datetime.timedelta(days=self.timeout_days),
                reason=f"Possible giveaway scam (score {score}: {', '.join(reasons)})",
            )
        except Exception as exc:
            # Discord will not time out an administrator, and role hierarchy blocks the rest.
            # Deliberately broad: whatever goes wrong here, staff still need the alert, and
            # the traceback still reaches the log rather than being swallowed.
            traceback.print_exception(exc)
            problems.append("**Could not time them out** — they can still post. Check the "
                            "bot's role position, and note admins cannot be timed out.")

        cutoff = now - datetime.timedelta(minutes=self.purge_window_minutes)
        sweep = await self.sweep_recent(message.guild, message.author, cutoff)

        # Only quote the text when the forward that was meant to carry it did not go out.
        fallback = message.content if any("forward" in p for p in problems) else None
        try:
            await alert.edit(embed=build_alert_embed(
                message.author, message.channel, score, reasons, sweep, problems, fallback
            ))
        except Exception as exc:
            # The buttons still work, so this costs the sweep summary rather than the case.
            traceback.print_exception(exc)

    async def sweep_recent(self, guild, author, cutoff) -> SweepResult:
        """Delete everything `author` posted since `cutoff`, in every channel the bot can act
        in -- the scam's photos are routinely a second message, sometimes somewhere else.

        The permission check is local, so channels the bot could not clean anyway cost no
        request. A channel that fails is skipped rather than aborting the rest of the sweep."""
        deleted = 0
        with_files = 0
        channels: list[str] = []

        for channel in [*guild.text_channels, *guild.threads]:
            perms = channel.permissions_for(guild.me)
            if not (perms.read_message_history and perms.manage_messages):
                continue
            # Counted per batch as it lands, not once at the end: a channel busy enough to
            # need a second batch would otherwise report zero for everything it did delete
            # if that second call failed, and staff would be told the scam is still up.
            removed = []
            try:
                stale = [
                    m
                    async for m in channel.history(after=cutoff, limit=SWEEP_LIMIT)
                    if m.author.id == author.id
                ]
                for start in range(0, len(stale), BULK_DELETE_MAX):
                    batch = stale[start:start + BULK_DELETE_MAX]
                    await channel.delete_messages(batch)
                    removed.extend(batch)
            except discord.HTTPException:
                # Forbidden included. One locked channel must not stop the others.
                traceback.print_exc()

            if not removed:
                continue
            deleted += len(removed)
            with_files += sum(1 for m in removed if m.attachments)
            channels.append(channel.name)

        return SweepResult(deleted, channels, with_files)


def setup(bot):
    bot.add_cog(AntiScam(bot))
