import datetime
import inspect
from types import SimpleNamespace

import discord
import pytest

from cogs import antiscam

NOW = datetime.datetime(2026, 8, 24, tzinfo=datetime.UTC)

RULES = {
    "threshold": 6,
    "account_age_bands": [
        {"max_days": 7, "weight": 4},
        {"max_days": 30, "weight": 3},
        {"max_days": 90, "weight": 2},
        {"max_days": 270, "weight": 1},
    ],
    "weights": {
        "giveaway_phrase": 3,
        "giveaway_phrase_each_extra": 1,
        "giveaway_phrase_max": 7,
        "offplatform_contact": 3,
        "dm_solicitation": 1,
        "mass_mention_text": 2,
        "attachment": 1,
    },
    "phrases": {
        "giveaway_phrase": ["giving away", "upgrading my", "first come first served"],
        "offplatform_contact": ["whatsapp", "imessage"],
        "dm_solicitation": ["dm me", "message me if"],
    },
}

# The two real reports from issue #66, trimmed to their text.
CAMERA_SCAM = (
    "@everyone Just upgraded! Giving away my old camera. It's still functional and in good "
    "shape. DM me if interested in picking it up dm me on WhatsApp..... +1 249 546 1998 "
    "iMessage ....Sophiaheart85@gmail.com"
)
PS5_SCAM = (
    "Giving away a PS5 to anyone who's interested! I'm upgrading my gaming set up and i want "
    "to pass on my old console to someone who'll enjoy it, first come first served. "
    "Message me if you're interested!"
)


def age(days):
    return days


# --- age_weight ---


def test_age_weight_uses_the_first_band_the_account_falls_under():
    assert antiscam.age_weight(3, RULES["account_age_bands"]) == 4
    assert antiscam.age_weight(20, RULES["account_age_bands"]) == 3
    assert antiscam.age_weight(60, RULES["account_age_bands"]) == 2
    assert antiscam.age_weight(200, RULES["account_age_bands"]) == 1


def test_age_weight_band_boundaries_are_exclusive():
    """A 7.0-day-old account is out of the 7-day band, not in it."""
    assert antiscam.age_weight(6.9, RULES["account_age_bands"]) == 4
    assert antiscam.age_weight(7.0, RULES["account_age_bands"]) == 3


def test_age_weight_is_zero_past_the_widest_band():
    """0 means age stops adding anything, not that the member stops being scored."""
    assert antiscam.age_weight(270, RULES["account_age_bands"]) == 0
    assert antiscam.age_weight(4000, RULES["account_age_bands"]) == 0


def test_account_age_days_measures_the_account_not_the_membership():
    created = NOW - datetime.timedelta(days=42)
    assert antiscam.account_age_days(created, NOW) == pytest.approx(42)


# --- score_message: the cases that must flag ---


def test_camera_scam_from_a_fresh_account_clears_the_threshold():
    score, reasons = antiscam.score_message(CAMERA_SCAM, True, age(3), RULES)
    assert (
        score == 14
    )  # age 4 + giveaway 3 + offplatform 3 + dm 1 + mass mention 2 + attachment 1
    assert score >= RULES["threshold"]
    assert "off-platform contact" in reasons
    assert "mass mention" in reasons


def test_ps5_scam_from_a_fresh_account_clears_the_threshold():
    """No WhatsApp or phone number in this one -- it clears on age, wording and the image."""
    score, reasons = antiscam.score_message(PS5_SCAM, True, age(3), RULES)
    assert (
        score == 11
    )  # age 4 + giveaway 3+2 for the extra phrases + dm 1 + attachment 1
    assert score >= RULES["threshold"]
    assert "off-platform contact" not in reasons


def test_the_same_ps5_text_from_an_established_account_still_clears_it():
    """Age barely helps here and it is meant not to. The post lays on five separate
    giveaway phrases, and stacked boilerplate is the tell that survives an older account."""
    score, _ = antiscam.score_message(PS5_SCAM, True, age(240), RULES)
    assert score >= RULES["threshold"]


# --- score_message: the cases that must NOT flag ---


def test_a_single_giveaway_phrase_from_an_old_account_stays_under():
    """The quiet floor. One phrase, no off-platform contact, no image, an account near the
    9-month line: that is as far as the detector leans back, and it is deliberately not far.
    Anything more than this is expected to flag."""
    score, _ = antiscam.score_message(
        "giving away my old textbook, DM me if you want it", False, age(240), RULES
    )
    assert score == 5
    assert score < RULES["threshold"]


def test_dm_solicitation_alone_cannot_carry_a_message_over():
    score, reasons = antiscam.score_message("dm me", False, age(3), RULES)
    assert reasons == ["new account (+4)", "asks you to DM"]
    assert score < RULES["threshold"]


# --- individual signals ---


def test_phone_number_counts_as_offplatform_contact():
    score, reasons = antiscam.score_message(
        "call +1 249 546 1998", False, age(400), RULES
    )
    assert "off-platform contact" in reasons
    assert score == RULES["weights"]["offplatform_contact"]


def test_email_counts_as_offplatform_contact():
    _, reasons = antiscam.score_message(
        "Sophiaheart85@gmail.com", False, age(400), RULES
    )
    assert "off-platform contact" in reasons


def test_a_bare_number_is_not_a_phone_number():
    """Prices and years shouldn't look like an off-platform handoff."""
    _, reasons = antiscam.score_message(
        "selling for 150 in 2026", False, age(400), RULES
    )
    assert reasons == []


def test_mass_mention_text_counts_even_though_they_cannot_really_ping():
    _, reasons = antiscam.score_message("@everyone hello", False, age(400), RULES)
    assert "mass mention" in reasons


def test_an_old_account_posting_nothing_suspicious_scores_zero():
    score, reasons = antiscam.score_message(
        "hey does anyone want to queue", False, age(400), RULES
    )
    assert (score, reasons) == (0, [])


# --- the alert embed ---


class FakeMember:
    def __init__(self, id=7, display_name="scammer", created_days_ago=3):
        self.id = id
        self.display_name = display_name
        self.mention = f"<@{id}>"
        self.created_at = NOW - datetime.timedelta(days=created_days_ago)
        self.display_avatar = SimpleNamespace(url="https://cdn.example/avatar.png")


def build_embed(
    member=None, channel=None, score=9, reasons=("giveaway wording",), **kwargs
):
    return antiscam.build_alert_embed(
        member or FakeMember(),
        channel or FakeSourceChannel(),
        score,
        list(reasons),
        **kwargs,
    )


def test_alert_embed_leads_with_the_score_and_reasons():
    embed = build_embed(score=9, reasons=["new account (+4)", "giveaway wording"])
    assert "Score 9" in embed.description
    assert "giveaway wording" in embed.description


def test_alert_embed_mentions_the_member_and_keeps_the_id():
    """The mention so staff can click through, the id because the buttons die on a restart
    and someone then has to act by hand."""
    fields = {f.name: f.value for f in build_embed().fields}
    assert "<@7>" in fields["Member"]
    assert "7" in fields["Member"]


def test_alert_embed_renders_the_account_age_as_a_discord_timestamp():
    """A stored "3.0 days" goes stale while the alert sits unread; <t:...> does not, and it
    renders in each reader's own locale."""
    created = int((NOW - datetime.timedelta(days=3)).timestamp())
    fields = {f.name: f.value for f in build_embed().fields}
    assert f"<t:{created}:R>" in fields["Account age"]
    assert f"<t:{created}:D>" in fields["Account age"]


def test_alert_embed_links_the_channel_rather_than_naming_it():
    fields = {f.name: f.value for f in build_embed().fields}
    assert fields["Posted in"] == "<#4242>"


def test_alert_embed_shows_the_offenders_avatar():
    assert build_embed().thumbnail.url == "https://cdn.example/avatar.png"


def test_alert_embed_does_not_repeat_the_message():
    """The forward under it is the message. Quoting it too made staff read the scam twice."""
    assert "Message" not in {f.name for f in build_embed().fields}


def test_alert_embed_quotes_the_message_only_when_the_forward_failed():
    fields = {f.name: f.value for f in build_embed(content=PS5_SCAM).fields}
    assert "Giving away a PS5" in fields["Message"]


def test_alert_embed_truncates_a_very_long_quoted_message():
    fields = {f.name: f.value for f in build_embed(content="x" * 4000).fields}
    assert len(fields["Message"]) < 1100
    assert fields["Message"].endswith("...")


# --- hold(): the order of operations ---


class FakeRole:
    def __init__(self, id=99):
        self.id = id
        self.mention = f"<@&{id}>"


DEFAULT_ROLE = FakeRole()


class FakeGuild:
    def __init__(self, role=None, text_channels=(), threads=()):
        self.role = role
        self.bans = []
        self.text_channels = list(text_channels)
        self.threads = list(threads)
        self.me = "bot-member"

    def get_role(self, role_id):
        return self.role

    async def ban(self, member, delete_message_seconds=None, reason=None):
        self.bans.append({"member": member, "seconds": delete_message_seconds})


class FakeAuthor(FakeMember):
    def __init__(self, guild, **kwargs):
        super().__init__(**kwargs)
        self.guild = guild
        self.bot = False
        self.timeouts = []

    # Two methods because py-cord has two: timeout() takes an absolute datetime and is what
    # Allow uses to clear a hold, timeout_for() takes the duration. Handing a timedelta to
    # the first is what crashed the first real flag, so the fakes mirror both signatures.
    async def timeout(self, until, reason=None):
        if isinstance(until, datetime.timedelta):
            # What py-cord does, one layer down: it calls until.isoformat() and blows up.
            # The fake used to accept this happily, which is how it reached production.
            raise TypeError("timeout() takes an absolute datetime; use timeout_for()")
        self.guild_events.append("timeout")
        self.timeouts.append({"until": until, "reason": reason})

    async def timeout_for(self, duration, reason=None):
        self.guild_events.append("timeout")
        self.timeouts.append({"duration": duration, "reason": reason})


class FakeAlertMessage:
    """What channel.send() hands back. hold() keeps it so the sweep result can be edited in
    after the fact, since the alert now goes out before the sweep has run."""

    def __init__(self, events):
        self.events = events
        self.edits = []

    async def edit(self, **kwargs):
        self.events.append("update")
        self.edits.append(kwargs)


class FakeAlertChannel:
    def __init__(self, events):
        self.events = events
        self.sent = []
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.events.append("forward" if "reference" in kwargs else "embed")
        self.sent.append({"content": content, **kwargs})
        message = FakeAlertMessage(self.events)
        self.messages.append(message)
        return message


class FakeSourceChannel:
    name = "general"
    mention = "<#4242>"


class FakeScamMessage:
    """The posted scam. Records into a shared event log, since the sequence -- forward while
    the original still exists, delete before the slower timeout call -- is the whole point."""

    def __init__(self, author, guild, events, content=PS5_SCAM, attachments=1):
        self.id = 1
        self.author = author
        self.guild = guild
        self.events = events
        self.content = content
        self.attachments = [object()] * attachments
        self.channel = FakeSourceChannel()

    def to_reference(self, type=None):
        self.reference_type = type
        return {"forwarded": True}

    async def delete(self):
        self.events.append("delete")


class FakeBot:
    def __init__(self, channel):
        self.channel = channel
        self.cached_messages = []

    def get_channel(self, channel_id):
        return self.channel


def build_cog(monkeypatch, events, role=DEFAULT_ROLE, guild=None):
    monkeypatch.setattr(
        antiscam.config,
        "config",
        {
            "antiscam": {
                "alert_channel": 5,
                "staff_role": 99,
                "timeout_days": 28,
                "ban_delete_message_days": 7,
                "purge_window_minutes": 60,
            }
        },
    )
    monkeypatch.setattr(antiscam.config, "has_leadership", lambda member: False)
    monkeypatch.setattr(antiscam.config, "is_bot_dev", lambda member: False)
    channel = FakeAlertChannel(events)
    cog = antiscam.AntiScam(bot=FakeBot(channel))
    guild = guild or FakeGuild(role)
    author = FakeAuthor(guild)
    author.guild_events = events
    message = FakeScamMessage(author, guild, events)
    return cog, message, channel


def final_embed(channel):
    """The alert goes out before the sweep runs, so the finished embed is the edited one."""
    return channel.messages[0].edits[-1]["embed"]


@pytest.mark.asyncio
async def test_hold_forwards_before_deleting_and_deletes_before_timing_out(monkeypatch):
    """The embed leads so the ping lands on the message staff act on. Then forward before
    the delete or there is nothing left to forward, and delete before the timeout because
    the message being visible is the actual harm."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert events[:4] == ["embed", "forward", "delete", "timeout"]


@pytest.mark.asyncio
async def test_the_pinged_embed_is_posted_above_the_forward(monkeypatch):
    """Reading order. The ping belongs on the message staff act on, with the forward beneath
    it as the evidence -- not a forwarded scam followed by a ping about it."""
    events = []
    cog, message, channel = build_cog(monkeypatch, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert [e for e in events if e in ("embed", "forward")] == ["embed", "forward"]
    assert "<@&99>" in channel.sent[0]["content"]
    assert "embed" in channel.sent[0]
    assert "reference" in channel.sent[1]


@pytest.mark.asyncio
async def test_hold_forwards_rather_than_reposting_the_text(monkeypatch):
    events = []
    cog, message, channel = build_cog(monkeypatch, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert message.reference_type is discord.MessageReferenceType.forward
    assert channel.sent[1]["reference"] == {"forwarded": True}


@pytest.mark.asyncio
async def test_the_forward_carries_no_content_of_its_own(monkeypatch):
    """Discord rejects a forward sent with content outright: 400, error code 160011. The
    staff ping rode on it until that 400 showed up in the container logs."""
    events = []
    cog, message, channel = build_cog(monkeypatch, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert channel.sent[1]["reference"] == {"forwarded": True}
    assert channel.sent[1]["content"] is None
    assert "embed" not in channel.sent[1]


@pytest.mark.asyncio
async def test_hold_pings_staff_without_letting_the_scam_ping_anyone(monkeypatch):
    """These posts carry a literal @everyone -- echoing one with default mentions would hand
    the scammer the mass ping they could not send themselves. The ping goes on the embed,
    which is the message staff can actually act on anyway."""
    events = []
    cog, message, channel = build_cog(monkeypatch, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    mentions = channel.sent[0]["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert "<@&99>" in channel.sent[0]["content"]


@pytest.mark.asyncio
async def test_a_refused_forward_still_reaches_staff(monkeypatch):
    """This is the failure that actually happened, and it aborted the whole hold: the scam
    stayed up, nobody was muted and no alert was posted."""
    events = []
    cog, message, channel = build_cog(monkeypatch, events)
    original_send = channel.send

    async def refuse_forward(content=None, **kwargs):
        if "reference" in kwargs:
            raise http_error()
        return await original_send(content, **kwargs)

    channel.send = refuse_forward

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert events[:3] == ["embed", "delete", "timeout"]
    fields = {f.name: f.value for f in final_embed(channel).fields}
    assert "Could not forward" in fields["⚠ Needs a human"]
    # The quoted copy is the fallback for exactly this case, and only this case.
    assert "Giving away a PS5" in fields["Message"]


@pytest.mark.asyncio
async def test_hold_times_out_for_the_configured_span(monkeypatch):
    events = []
    cog, message, _ = build_cog(monkeypatch, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert message.author.timeouts[0]["duration"] == datetime.timedelta(days=28)
    assert "score 9" in message.author.timeouts[0]["reason"]


def test_the_timeout_call_matches_pycords_real_signature():
    """The fakes accept whatever they are given, so a timedelta passed to timeout() -- which
    wants an absolute datetime -- sailed through every test and then blew up on the first
    real flag. This binds the arguments against the actual library instead."""
    inspect.signature(discord.Member.timeout_for).bind(
        None, datetime.timedelta(days=28), reason="scam"
    )
    inspect.signature(discord.Member.timeout).bind(None, None, reason="cleared")


@pytest.mark.asyncio
async def test_hold_does_nothing_when_the_alert_channel_is_missing(monkeypatch):
    """Better to leave the message up than delete it with nowhere to report it."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    cog.bot.channel = None

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert events == []


@pytest.mark.asyncio
async def test_a_refused_alert_leaves_the_member_flaggable(monkeypatch):
    """The alert is the one send that cannot degrade -- there is nowhere to report the case
    without it. Marking the member held before it lands would ignore every later scam from
    them for the life of the process, and silently."""
    events = []
    cog, message, channel = build_cog(monkeypatch, events)

    async def refuse(content=None, **kwargs):
        raise http_error()

    channel.send = refuse

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert message.author.id not in cog._held
    # Nothing deleted either: better to leave it up than delete it into silence.
    assert events == []


@pytest.mark.asyncio
async def test_hold_survives_a_missing_staff_role(monkeypatch):
    events = []
    cog, message, channel = build_cog(monkeypatch, events, role=None)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert events[:4] == ["embed", "forward", "delete", "timeout"]
    assert channel.sent[0]["allowed_mentions"].roles is False


# --- the review buttons ---


class FakeResponse:
    def __init__(self):
        self.edits = []
        self.messages = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)


class FakeClicker:
    def __init__(self, leadership=True, moderate_members=True, ban_members=True):
        self.leadership = leadership
        self.mention = "<@1>"
        self.response = FakeResponse()
        self.user = self
        # Discord reports every permission as granted for an administrator, so these two
        # standing separately is what a non-admin moderator actually looks like.
        self.guild_permissions = SimpleNamespace(
            moderate_members=moderate_members, ban_members=ban_members
        )


@pytest.mark.asyncio
async def test_allow_clears_the_timeout(monkeypatch):
    monkeypatch.setattr(antiscam.config, "has_leadership", lambda user: True)
    monkeypatch.setattr(antiscam.config, "is_bot_dev", lambda user: False)
    guild = FakeGuild(FakeRole())
    member = FakeAuthor(guild)
    member.guild_events = []
    view = antiscam.ScamReviewView(member, ban_delete_days=7)
    interaction = FakeClicker()

    await view.allow.callback(interaction)

    assert member.timeouts[0]["until"] is None
    assert all(child.disabled for child in view.children)


@pytest.mark.asyncio
async def test_ban_purges_recent_messages_too(monkeypatch):
    monkeypatch.setattr(antiscam.config, "has_leadership", lambda user: True)
    monkeypatch.setattr(antiscam.config, "is_bot_dev", lambda user: False)
    guild = FakeGuild(FakeRole())
    member = FakeAuthor(guild)
    member.guild_events = []
    view = antiscam.ScamReviewView(member, ban_delete_days=7)

    await view.ban.callback(FakeClicker())

    assert guild.bans[0]["seconds"] == 7 * 86400


@pytest.mark.asyncio
async def test_each_button_needs_the_permission_its_action_needs(monkeypatch):
    """Being leadership gets you as far as the buttons; it does not stand in for the
    permission. Someone who cannot ban by hand must not ban through the bot."""
    monkeypatch.setattr(antiscam.config, "has_leadership", lambda user: True)
    monkeypatch.setattr(antiscam.config, "is_bot_dev", lambda user: False)
    guild = FakeGuild(FakeRole())
    member = FakeAuthor(guild)
    member.guild_events = []
    view = antiscam.ScamReviewView(member, ban_delete_days=7)

    no_timeout = FakeClicker(moderate_members=False)
    await view.allow.callback(no_timeout)
    assert "Moderate Members" in no_timeout.response.messages[0]
    assert member.timeouts == []

    no_ban = FakeClicker(ban_members=False)
    await view.ban.callback(no_ban)
    assert "Ban Members" in no_ban.response.messages[0]
    assert guild.bans == []


@pytest.mark.asyncio
async def test_the_two_permissions_are_checked_independently(monkeypatch):
    """A moderator who can time out but not ban still gets to use Allow."""
    monkeypatch.setattr(antiscam.config, "has_leadership", lambda user: True)
    monkeypatch.setattr(antiscam.config, "is_bot_dev", lambda user: False)
    member = FakeAuthor(FakeGuild(FakeRole()))
    member.guild_events = []
    view = antiscam.ScamReviewView(member, ban_delete_days=7)

    await view.allow.callback(FakeClicker(ban_members=False))

    assert member.timeouts[0]["until"] is None


@pytest.mark.asyncio
async def test_a_button_that_cannot_act_says_so(monkeypatch):
    """Otherwise staff get a bare "interaction failed" and the buttons stay lit, with no
    way to tell whether the member was cleared or not."""
    monkeypatch.setattr(antiscam.config, "has_leadership", lambda user: True)
    monkeypatch.setattr(antiscam.config, "is_bot_dev", lambda user: False)
    member = FakeAuthor(FakeGuild(FakeRole()))
    member.guild_events = []

    async def refuse(until, reason=None):
        raise http_error()

    member.timeout = refuse
    view = antiscam.ScamReviewView(member, ban_delete_days=7)
    interaction = FakeClicker()

    await view.allow.callback(interaction)

    assert "Could not clear the timeout" in interaction.response.messages[0]
    # Still actionable: a transient failure must not disarm the case.
    assert not any(child.disabled for child in view.children)


@pytest.mark.asyncio
async def test_a_failed_ban_leaves_the_buttons_live(monkeypatch):
    monkeypatch.setattr(antiscam.config, "has_leadership", lambda user: True)
    monkeypatch.setattr(antiscam.config, "is_bot_dev", lambda user: False)
    guild = FakeGuild(FakeRole())

    async def refuse(member, delete_message_seconds=None, reason=None):
        raise http_error()

    guild.ban = refuse
    member = FakeAuthor(guild)
    member.guild_events = []
    view = antiscam.ScamReviewView(member, ban_delete_days=7)
    interaction = FakeClicker()

    await view.ban.callback(interaction)

    assert "Could not ban them" in interaction.response.messages[0]
    assert not any(child.disabled for child in view.children)


@pytest.mark.asyncio
async def test_a_non_staff_click_is_refused(monkeypatch):
    monkeypatch.setattr(antiscam.config, "has_leadership", lambda user: False)
    monkeypatch.setattr(antiscam.config, "is_bot_dev", lambda user: False)
    guild = FakeGuild(FakeRole())
    member = FakeAuthor(guild)
    member.guild_events = []
    view = antiscam.ScamReviewView(member, ban_delete_days=7)
    interaction = FakeClicker(leadership=False)

    assert await view.interaction_check(interaction) is False
    assert "Only leadership" in interaction.response.messages[0]


# --- the split post: a photo that arrived as its own message ---

CUTOFF = NOW - datetime.timedelta(minutes=60)


def cached(id, author_id=7, attachments=1, minutes_ago=5, guild="a-guild"):
    return SimpleNamespace(
        id=id,
        author=SimpleNamespace(id=author_id),
        attachments=[object()] * attachments,
        created_at=NOW - datetime.timedelta(minutes=minutes_ago),
        guild=guild,
    )


def test_recent_attachment_finds_a_file_the_author_posted_separately():
    assert antiscam.recent_attachment([cached(2)], 7, CUTOFF, exclude_id=1)


def test_recent_attachment_ignores_the_message_being_scored():
    """Otherwise a message with its own image would credit itself twice."""
    assert not antiscam.recent_attachment([cached(1)], 7, CUTOFF, exclude_id=1)


def test_recent_attachment_ignores_someone_elses_upload():
    assert not antiscam.recent_attachment(
        [cached(2, author_id=8)], 7, CUTOFF, exclude_id=1
    )


def test_recent_attachment_ignores_anything_older_than_the_window():
    assert not antiscam.recent_attachment(
        [cached(2, minutes_ago=90)], 7, CUTOFF, exclude_id=1
    )


def test_recent_attachment_ignores_dms():
    assert not antiscam.recent_attachment(
        [cached(2, guild=None)], 7, CUTOFF, exclude_id=1
    )


def test_recent_attachment_ignores_messages_carrying_no_file():
    assert not antiscam.recent_attachment(
        [cached(2, attachments=0)], 7, CUTOFF, exclude_id=1
    )


def test_the_split_post_costs_the_scammer_the_attachment_weight():
    """Why crediting a photo from another message matters: splitting the post is otherwise
    worth a free point to whoever does it."""
    with_image, _ = antiscam.score_message(PS5_SCAM, True, age(3), RULES)
    without, _ = antiscam.score_message(PS5_SCAM, False, age(3), RULES)
    assert with_image - without == RULES["weights"]["attachment"]


# --- sweep_recent: the last hour, everywhere the bot can reach ---


def http_error():
    """py-cord's HTTPException only needs a response carrying .status and .reason.
    Forbidden subclasses it, which is why sweep_recent catches the base."""
    return discord.HTTPException(
        SimpleNamespace(status=403, reason="Forbidden"), "nope"
    )


class FakePerms:
    def __init__(self, read=True, manage=True):
        self.read_message_history = read
        self.manage_messages = manage


def posted(id, author_id=7, attachments=0):
    return SimpleNamespace(
        id=id,
        author=SimpleNamespace(id=author_id),
        attachments=[object()] * attachments,
    )


class FakeSweepChannel:
    """history() is a plain method returning an async iterator, matching py-cord, so that
    the call arguments are recorded even for a channel that yields nothing."""

    def __init__(self, name, messages=(), perms=None, fails=None):
        self.name = name
        self.messages = list(messages)
        self.perms = perms or FakePerms()
        self.fails = fails  # "history", "delete", or None
        self.history_calls = []
        self.deleted_batches = []

    def permissions_for(self, member):
        return self.perms

    def history(self, after=None, limit=None):
        self.history_calls.append({"after": after, "limit": limit})
        return self._walk()

    async def _walk(self):
        if self.fails == "history":
            raise http_error()
        for message in self.messages:
            yield message

    async def delete_messages(self, messages):
        if self.fails == "delete":
            raise http_error()
        self.deleted_batches.append(list(messages))


AUTHOR = SimpleNamespace(id=7)


def sweep_cog(monkeypatch):
    cog, _, _ = build_cog(monkeypatch, [])
    return cog


@pytest.mark.asyncio
async def test_sweep_deletes_only_that_authors_messages(monkeypatch):
    channel = FakeSweepChannel(
        "general", [posted(2), posted(3, author_id=8), posted(4)]
    )

    result = await sweep_cog(monkeypatch).sweep_recent(
        FakeGuild(text_channels=[channel]), AUTHOR, CUTOFF
    )

    assert [m.id for m in channel.deleted_batches[0]] == [2, 4]
    assert result.deleted == 2


@pytest.mark.asyncio
async def test_sweep_hands_the_cutoff_to_discord_rather_than_filtering_by_hand(
    monkeypatch,
):
    channel = FakeSweepChannel("general", [posted(2)])

    await sweep_cog(monkeypatch).sweep_recent(
        FakeGuild(text_channels=[channel]), AUTHOR, CUTOFF
    )

    assert channel.history_calls[0] == {"after": CUTOFF, "limit": antiscam.SWEEP_LIMIT}


@pytest.mark.asyncio
async def test_sweep_never_reads_a_channel_it_could_not_clean(monkeypatch):
    """The permission check is local, so a channel the bot can't purge costs no request."""
    unreadable = FakeSweepChannel("secret", [posted(2)], perms=FakePerms(read=False))
    unmanageable = FakeSweepChannel(
        "locked", [posted(3)], perms=FakePerms(manage=False)
    )

    result = await sweep_cog(monkeypatch).sweep_recent(
        FakeGuild(text_channels=[unreadable, unmanageable]), AUTHOR, CUTOFF
    )

    assert unreadable.history_calls == []
    assert unmanageable.history_calls == []
    assert result == antiscam.SweepResult(0, [], 0)


@pytest.mark.asyncio
async def test_sweep_keeps_going_when_one_channel_fails(monkeypatch):
    """One locked channel must not leave the scam up everywhere else."""
    for failure in ("history", "delete"):
        broken = FakeSweepChannel("broken", [posted(2)], fails=failure)
        fine = FakeSweepChannel("general", [posted(3)])

        result = await sweep_cog(monkeypatch).sweep_recent(
            FakeGuild(text_channels=[broken, fine]), AUTHOR, CUTOFF
        )

        assert result == antiscam.SweepResult(1, ["general"], 0)


@pytest.mark.asyncio
async def test_sweep_reports_the_batches_that_did_land(monkeypatch):
    """A channel busy enough to need a second batch used to report zero for the whole
    channel if that second call failed -- telling staff the scam was still up when 100 of
    its messages had in fact gone."""
    channel = FakeSweepChannel("general", [posted(i) for i in range(150)])
    calls = []
    original = channel.delete_messages

    async def fail_on_the_second(messages):
        calls.append(messages)
        if len(calls) == 2:
            raise http_error()
        await original(messages)

    channel.delete_messages = fail_on_the_second

    result = await sweep_cog(monkeypatch).sweep_recent(
        FakeGuild(text_channels=[channel]), AUTHOR, CUTOFF
    )

    assert result == antiscam.SweepResult(100, ["general"], 0)


@pytest.mark.asyncio
async def test_sweep_chunks_past_discords_bulk_delete_ceiling(monkeypatch):
    channel = FakeSweepChannel("general", [posted(i) for i in range(150)])

    result = await sweep_cog(monkeypatch).sweep_recent(
        FakeGuild(text_channels=[channel]), AUTHOR, CUTOFF
    )

    assert [len(batch) for batch in channel.deleted_batches] == [100, 50]
    assert result.deleted == 150


@pytest.mark.asyncio
async def test_sweep_covers_threads_and_counts_where_the_files_were(monkeypatch):
    channel = FakeSweepChannel("general", [posted(2)])
    thread = FakeSweepChannel("pugs-thread", [posted(3, attachments=1)])

    result = await sweep_cog(monkeypatch).sweep_recent(
        FakeGuild(text_channels=[channel], threads=[thread]), AUTHOR, CUTOFF
    )

    assert result == antiscam.SweepResult(2, ["general", "pugs-thread"], 1)


# --- what the sweep reports back to staff ---


def test_alert_embed_reports_what_else_the_sweep_removed():
    """Swept messages aren't forwarded, so this line is the only thing telling staff a photo
    existed -- and the photo is the half of the scam that isn't in the text."""
    sweep = antiscam.SweepResult(2, ["general", "memes"], 1)

    fields = {f.name: f.value for f in build_embed(sweep=sweep).fields}
    assert (
        fields["Also removed"]
        == "2 more messages in #general, #memes (1 with attachments)"
    )


def test_alert_embed_omits_the_sweep_line_when_there_was_nothing_else():
    embed = build_embed(sweep=antiscam.SweepResult(0, [], 0))

    assert "Also removed" not in {f.name for f in embed.fields}


# --- hold(): where the sweep sits in the sequence ---


def record_sweep(monkeypatch, cog, events, result=None):
    result = result if result is not None else antiscam.SweepResult(1, ["memes"], 1)
    captured = {}

    async def sweep_recent(guild, author, cutoff):
        events.append("sweep")
        captured["cutoff"] = cutoff
        return result

    monkeypatch.setattr(cog, "sweep_recent", sweep_recent)
    return captured


@pytest.mark.asyncio
async def test_hold_edits_the_sweep_result_in_after_the_fact(monkeypatch):
    """The whole sequence. The alert leads so the ping lands on the message staff act on,
    which means it goes out before the sweep has run and the outcome is edited in at the
    end. The timeout still precedes the sweep: the sweep is many round-trips and an un-muted
    scammer can keep posting right through it."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    record_sweep(monkeypatch, cog, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert events == ["embed", "forward", "delete", "timeout", "sweep", "update"]


@pytest.mark.asyncio
async def test_hold_sweeps_back_the_configured_window(monkeypatch):
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    captured = record_sweep(monkeypatch, cog, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert captured["cutoff"] == NOW - datetime.timedelta(minutes=60)


@pytest.mark.asyncio
async def test_hold_puts_the_sweep_result_on_the_staff_embed(monkeypatch):
    events = []
    cog, message, channel = build_cog(monkeypatch, events)
    record_sweep(monkeypatch, cog, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    fields = {f.name: f.value for f in final_embed(channel).fields}
    assert fields["Also removed"] == "1 more message in #memes (1 with attachments)"


# --- one case per member, not one per message ---


@pytest.mark.asyncio
async def test_a_second_message_from_a_held_member_is_ignored(monkeypatch):
    """Without this a scammer who posts twice gets two forwards, two embeds and two sweeps."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    monkeypatch.setattr(antiscam.config, "antiscam_data", RULES)
    cog._held.add(message.author.id)

    await cog.on_message(message)

    assert events == []


@pytest.mark.asyncio
async def test_allowing_a_member_lets_them_be_flagged_again(monkeypatch):
    monkeypatch.setattr(antiscam.config, "has_leadership", lambda user: True)
    monkeypatch.setattr(antiscam.config, "is_bot_dev", lambda user: False)
    released = []
    member = FakeAuthor(FakeGuild(FakeRole()))
    member.guild_events = []
    view = antiscam.ScamReviewView(member, 7, on_resolved=lambda: released.append(True))

    await view.allow.callback(FakeClicker())

    assert released == [True]


@pytest.mark.asyncio
async def test_on_message_credits_a_photo_the_author_posted_in_another_message(
    monkeypatch,
):
    """The pitch and the photos arrive separately, so the photo has to count for the message
    that scored. Only matters for a message sitting one point below the line, which is why
    this one is deliberately thin -- anything blatant is over the threshold on wording alone
    and never reaches the cache at all."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    monkeypatch.setattr(antiscam.config, "antiscam_data", RULES)
    message.content = "dm me if you want it"
    message.attachments = []
    message.author.created_at = discord.utils.utcnow() - datetime.timedelta(days=3)
    cog.bot.cached_messages = [
        SimpleNamespace(
            id=2,
            author=SimpleNamespace(id=message.author.id),
            attachments=[object()],
            guild="a-guild",
            created_at=discord.utils.utcnow() - datetime.timedelta(minutes=5),
        )
    ]

    await cog.on_message(message)

    assert "attachment" in message.author.timeouts[0]["reason"]
    assert "score 6" in message.author.timeouts[0]["reason"]


# --- the two reports, scored against the real data/antiscam.yaml ---
#
# Everything above runs on RULES, a trimmed mirror, which proves the arithmetic but says
# nothing about what ships. These run on the file the bot actually loads, with the text
# exactly as it was pasted -- curly apostrophes, stray direction marks and all.

REAL_RULES = antiscam.config.antiscam_data
SIX_MONTHS = 182

REPORTED_CAMERA = (
    "@everyone\"Just upgraded! Giving away my old camera. It's still functional and in good "
    "shape. Perfect for photography enthusiasts or anyone wanting to start! DM me if "
    "interested in picking it up dm me on WhatsApp…..\n\n"
    "+1 249 546 1998\n\niMessage ….Sophiaheart85@gmail.com"
)
REPORTED_PS5 = (
    "Giving away a PS5 to anyone who's interested!\n"
    "I’m upgrading my gaming set up and i want to pass on my old console to someone "
    "who’ll enjoy it, first come first served. \n"
    "Message me if you're interested!"
)


def test_the_reported_camera_scam_flags_at_six_months():
    score, _ = antiscam.score_message(REPORTED_CAMERA, False, SIX_MONTHS, REAL_RULES)
    assert score >= REAL_RULES["threshold"]


def test_the_reported_ps5_scam_flags_at_six_months():
    """The harder of the two: no phone number, no email, no @everyone. It clears on the
    stacked giveaway wording alone -- five separate phrases in three sentences."""
    score, _ = antiscam.score_message(REPORTED_PS5, False, SIX_MONTHS, REAL_RULES)
    assert score >= REAL_RULES["threshold"]


def test_neither_report_needs_an_image_to_flag():
    """The photos arrive as their own message, so nothing may depend on them being here."""
    for text in (REPORTED_CAMERA, REPORTED_PS5):
        score, _ = antiscam.score_message(text, False, SIX_MONTHS, REAL_RULES)
        assert score >= REAL_RULES["threshold"]


def test_a_curly_apostrophe_still_matches_the_phrase_list():
    """Typed on a phone this arrives as U+2019, which matched nothing before normalise()."""
    curly, _ = antiscam.score_message(
        "to anyone who’s interested", False, 3, REAL_RULES
    )
    straight, _ = antiscam.score_message(
        "to anyone who's interested", False, 3, REAL_RULES
    )
    assert curly == straight
    assert curly > antiscam.age_weight(3, REAL_RULES["account_age_bands"])


# --- giveaway wording escalates with the number of distinct phrases ---


def test_giveaway_wording_escalates_with_each_extra_phrase():
    """One of these is something a person says. Several stacked is an advertisement."""
    weights, phrases = RULES["weights"], RULES["phrases"]["giveaway_phrase"]
    assert antiscam.giveaway_weight("nothing of interest here", phrases, weights) == 0
    assert antiscam.giveaway_weight("giving away my desk", phrases, weights) == 3
    assert (
        antiscam.giveaway_weight("giving away, upgrading my rig", phrases, weights) == 4
    )


def test_giveaway_wording_is_capped():
    weights = dict(RULES["weights"], giveaway_phrase_max=4)
    phrases = RULES["phrases"]["giveaway_phrase"]
    stacked = "giving away, upgrading my rig, first come first served"
    assert antiscam.giveaway_weight(stacked, phrases, weights) == 4


# --- the local-testing age override ---


def test_effective_age_uses_the_real_account_age_by_default():
    created = NOW - datetime.timedelta(days=42)
    assert antiscam.effective_age_days(7, created, NOW, {}) == pytest.approx(42)


def test_effective_age_honours_a_local_testing_override():
    """Nobody has a three-day-old account on demand, so without this the flow can't be
    exercised end to end against a real Discord guild."""
    created = NOW - datetime.timedelta(days=3700)
    assert antiscam.effective_age_days(7, created, NOW, {7: 180}) == 180


def test_the_override_only_touches_the_listed_account():
    created = NOW - datetime.timedelta(days=3700)
    assert antiscam.effective_age_days(8, created, NOW, {7: 180}) == pytest.approx(3700)


@pytest.mark.asyncio
async def test_a_faked_age_is_what_gets_scored(monkeypatch):
    """The account is really ten years old, so age would contribute 0. Pinned to 180 it
    contributes 1, and the score says so."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    monkeypatch.setattr(antiscam.config, "antiscam_data", RULES)
    cog.test_ages = {message.author.id: 180}
    message.author.created_at = discord.utils.utcnow() - datetime.timedelta(days=3700)

    await cog.on_message(message)

    assert "score 8" in message.author.timeouts[0]["reason"]
    assert "new account (+1)" in message.author.timeouts[0]["reason"]


# --- age is evidence, not a gate ---


def test_both_reports_flag_from_an_account_years_old():
    """This wording is a scam whoever posts it, so nothing is exempt on age alone."""
    assert antiscam.age_weight(4000, REAL_RULES["account_age_bands"]) == 0
    for text in (REPORTED_CAMERA, REPORTED_PS5):
        score, _ = antiscam.score_message(text, False, 4000, REAL_RULES)
        assert score >= REAL_RULES["threshold"]


@pytest.mark.asyncio
async def test_an_established_account_is_still_scored(monkeypatch):
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    monkeypatch.setattr(antiscam.config, "antiscam_data", RULES)
    message.author.created_at = discord.utils.utcnow() - datetime.timedelta(days=4000)

    await cog.on_message(message)

    assert events[:2] == ["embed", "forward"]


@pytest.mark.asyncio
async def test_staff_are_never_held(monkeypatch):
    """They are the ones who click Allow. An official club giveaway posted by leadership
    must not delete itself and mute whoever ran it."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    monkeypatch.setattr(antiscam.config, "antiscam_data", RULES)
    monkeypatch.setattr(antiscam.config, "has_leadership", lambda member: True)

    await cog.on_message(message)

    assert events == []


@pytest.mark.asyncio
async def test_the_staff_exemption_can_be_turned_off_for_testing(monkeypatch):
    """Otherwise there is no way to exercise this from a developer's own account, since a
    bot dev is exempt and every admin is treated as one."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    monkeypatch.setattr(antiscam.config, "antiscam_data", RULES)
    monkeypatch.setattr(antiscam.config, "has_leadership", lambda member: True)
    cog.exempt_staff = False

    await cog.on_message(message)

    assert events[:2] == ["embed", "forward"]


# --- a blocked timeout must not swallow the whole alert ---


@pytest.mark.asyncio
async def test_a_refused_timeout_still_reaches_staff(monkeypatch):
    """Discord will not time out an administrator. Deleting the message and then bailing
    before the alert is posted would leave staff with no idea anything happened."""
    events = []
    cog, message, channel = build_cog(monkeypatch, events)

    async def refuse(duration, reason=None):
        raise http_error()

    message.author.timeout_for = refuse

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    fields = {f.name: f.value for f in final_embed(channel).fields}
    assert "still post" in fields["⚠ Needs a human"]


@pytest.mark.asyncio
async def test_a_refused_delete_still_reaches_staff(monkeypatch):
    events = []
    cog, message, channel = build_cog(monkeypatch, events)

    async def refuse():
        raise http_error()

    message.delete = refuse

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    fields = {f.name: f.value for f in final_embed(channel).fields}
    assert "may still be up" in fields["⚠ Needs a human"]


def test_problems_are_surfaced_rather_than_left_for_staff_to_notice():
    """Silently half-acting is the worst outcome: staff would read the alert and assume the
    member was muted while they are still talking."""
    embed = build_embed(problems=["Could not time them out."])

    fields = {f.name: f.value for f in embed.fields}
    assert fields["⚠ Needs a human"] == "Could not time them out."


class ExplodingCache:
    def __iter__(self):
        raise AssertionError("the message cache should not be walked for this message")


@pytest.mark.asyncio
async def test_an_ordinary_message_never_walks_the_message_cache(monkeypatch):
    """Every message in the server reaches on_message now that age gates nothing, so the
    cache scan has to stay off the path for anything nowhere near the threshold."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    monkeypatch.setattr(antiscam.config, "antiscam_data", RULES)
    cog.bot.cached_messages = ExplodingCache()
    message.content = "hey does anyone want to queue"
    message.attachments = []

    await cog.on_message(message)

    assert events == []
