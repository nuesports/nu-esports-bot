import datetime
from types import SimpleNamespace

import discord
import pytest

from cogs import antiscam

NOW = datetime.datetime(2026, 8, 24, tzinfo=datetime.timezone.utc)

RULES = {
    "threshold": 8,
    "account_age_bands": [
        {"max_days": 7, "weight": 4},
        {"max_days": 30, "weight": 3},
        {"max_days": 90, "weight": 2},
        {"max_days": 270, "weight": 1},
    ],
    "weights": {
        "giveaway_phrase": 3,
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
    """0 is what makes a member unscannable, so this doubles as the 9-month cutoff."""
    assert antiscam.age_weight(270, RULES["account_age_bands"]) == 0
    assert antiscam.age_weight(4000, RULES["account_age_bands"]) == 0


def test_account_age_days_measures_the_account_not_the_membership():
    created = NOW - datetime.timedelta(days=42)
    assert antiscam.account_age_days(created, NOW) == pytest.approx(42)


# --- score_message: the cases that must flag ---


def test_camera_scam_from_a_fresh_account_clears_the_threshold():
    score, reasons = antiscam.score_message(CAMERA_SCAM, True, age(3), RULES)
    assert score == 14  # age 4 + giveaway 3 + offplatform 3 + dm 1 + mass mention 2 + attachment 1
    assert score >= RULES["threshold"]
    assert "off-platform contact" in reasons
    assert "mass mention" in reasons


def test_ps5_scam_from_a_fresh_account_clears_the_threshold():
    """No WhatsApp or phone number in this one -- it clears on age, wording and the image."""
    score, reasons = antiscam.score_message(PS5_SCAM, True, age(3), RULES)
    assert score == 9  # age 4 + giveaway 3 + dm 1 + attachment 1
    assert score >= RULES["threshold"]
    assert "off-platform contact" not in reasons


# --- score_message: the cases that must NOT flag ---


def test_the_same_ps5_text_from_an_established_account_stays_under():
    """Pinned deliberately. A weight change that starts catching older members should fail
    here rather than surprise someone in production."""
    score, _ = antiscam.score_message(PS5_SCAM, True, age(240), RULES)
    assert score == 6
    assert score < RULES["threshold"]


def test_a_real_member_offering_a_textbook_stays_under():
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
    score, reasons = antiscam.score_message("call +1 249 546 1998", False, age(400), RULES)
    assert "off-platform contact" in reasons
    assert score == RULES["weights"]["offplatform_contact"]


def test_email_counts_as_offplatform_contact():
    _, reasons = antiscam.score_message("Sophiaheart85@gmail.com", False, age(400), RULES)
    assert "off-platform contact" in reasons


def test_a_bare_number_is_not_a_phone_number():
    """Prices and years shouldn't look like an off-platform handoff."""
    _, reasons = antiscam.score_message("selling for 150 in 2026", False, age(400), RULES)
    assert reasons == []


def test_mass_mention_text_counts_even_though_they_cannot_really_ping():
    _, reasons = antiscam.score_message("@everyone hello", False, age(400), RULES)
    assert "mass mention" in reasons


def test_an_old_account_posting_nothing_suspicious_scores_zero():
    score, reasons = antiscam.score_message("hey does anyone want to queue", False, age(400), RULES)
    assert (score, reasons) == (0, [])


# --- the alert embed ---


class FakeMember:
    def __init__(self, id=7, display_name="scammer", created_days_ago=3):
        self.id = id
        self.display_name = display_name
        self.mention = f"<@{id}>"
        self.created_at = NOW - datetime.timedelta(days=created_days_ago)


def test_alert_embed_reports_the_age_score_and_reasons():
    embed = antiscam.build_alert_embed(
        FakeMember(), "general", PS5_SCAM, 9, ["new account (+4)", "giveaway wording"], NOW
    )
    fields = {f.name: f.value for f in embed.fields}
    assert "3.0 days" in fields["Account age"]
    assert "**9**" in fields["Score"]
    assert "giveaway wording" in fields["Score"]
    assert "#general" in fields["Posted in"]
    assert "7" in fields["Member"]


def test_alert_embed_repeats_the_message_text():
    """Belt and braces: the forward above it should carry the content, but if it renders
    empty the staff copy is still readable."""
    embed = antiscam.build_alert_embed(FakeMember(), "general", PS5_SCAM, 9, ["x"], NOW)
    fields = {f.name: f.value for f in embed.fields}
    assert "Giving away a PS5" in fields["Message"]


def test_alert_embed_truncates_a_very_long_message():
    embed = antiscam.build_alert_embed(FakeMember(), "general", "x" * 4000, 9, ["x"], NOW)
    fields = {f.name: f.value for f in embed.fields}
    assert len(fields["Message"]) < 1100
    assert fields["Message"].endswith("...")


# --- hold(): the order of operations ---


class FakeRole:
    def __init__(self, id=99):
        self.id = id
        self.mention = f"<@&{id}>"


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

    async def timeout(self, until, reason=None):
        self.guild_events.append("timeout")
        self.timeouts.append({"until": until, "reason": reason})


class FakeAlertChannel:
    def __init__(self, events):
        self.events = events
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.events.append("forward" if "reference" in kwargs else "embed")
        self.sent.append({"content": content, **kwargs})
        return None


class FakeSourceChannel:
    name = "general"


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


def build_cog(monkeypatch, events, role=FakeRole(), guild=None):
    monkeypatch.setattr(
        antiscam.config,
        "config",
        {"antiscam": {"alert_channel": 5, "staff_role": 99, "timeout_days": 28,
                      "ban_delete_message_days": 7, "purge_window_minutes": 60}},
    )
    channel = FakeAlertChannel(events)
    cog = antiscam.AntiScam(bot=FakeBot(channel))
    guild = guild or FakeGuild(role)
    author = FakeAuthor(guild)
    author.guild_events = events
    message = FakeScamMessage(author, guild, events)
    return cog, message, channel


@pytest.mark.asyncio
async def test_hold_forwards_before_deleting_and_deletes_before_timing_out(monkeypatch):
    """Forward first or there is nothing left to forward; delete before the timeout because
    the timeout is the slower call and the message being visible is the actual harm."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert events[:3] == ["forward", "delete", "timeout"]


@pytest.mark.asyncio
async def test_hold_forwards_rather_than_reposting_the_text(monkeypatch):
    events = []
    cog, message, channel = build_cog(monkeypatch, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert message.reference_type is discord.MessageReferenceType.forward
    assert channel.sent[0]["reference"] == {"forwarded": True}


@pytest.mark.asyncio
async def test_hold_pings_staff_without_letting_the_scam_ping_anyone(monkeypatch):
    """These posts carry a literal @everyone -- echoing one with default mentions would hand
    the scammer the mass ping they could not send themselves."""
    events = []
    cog, message, channel = build_cog(monkeypatch, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    mentions = channel.sent[0]["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert "<@&99>" in channel.sent[0]["content"]


@pytest.mark.asyncio
async def test_hold_times_out_for_the_configured_span(monkeypatch):
    events = []
    cog, message, _ = build_cog(monkeypatch, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert message.author.timeouts[0]["until"] == datetime.timedelta(days=28)
    assert "score 9" in message.author.timeouts[0]["reason"]


@pytest.mark.asyncio
async def test_hold_does_nothing_when_the_alert_channel_is_missing(monkeypatch):
    """Better to leave the message up than delete it with nowhere to report it."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    cog.bot.channel = None

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert events == []


@pytest.mark.asyncio
async def test_hold_survives_a_missing_staff_role(monkeypatch):
    events = []
    cog, message, channel = build_cog(monkeypatch, events, role=None)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert events[:3] == ["forward", "delete", "timeout"]
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
    def __init__(self, leadership=True):
        self.leadership = leadership
        self.mention = "<@1>"
        self.response = FakeResponse()
        self.user = self


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
    assert not antiscam.recent_attachment([cached(2, author_id=8)], 7, CUTOFF, exclude_id=1)


def test_recent_attachment_ignores_anything_older_than_the_window():
    assert not antiscam.recent_attachment([cached(2, minutes_ago=90)], 7, CUTOFF, exclude_id=1)


def test_recent_attachment_ignores_dms():
    assert not antiscam.recent_attachment([cached(2, guild=None)], 7, CUTOFF, exclude_id=1)


def test_recent_attachment_ignores_messages_carrying_no_file():
    assert not antiscam.recent_attachment([cached(2, attachments=0)], 7, CUTOFF, exclude_id=1)


def test_the_ps5_text_alone_sits_exactly_on_the_threshold():
    """Why the split matters. Without the image the text is at 8 exactly, so the same post
    without "message me if" drops to 7 and slips through -- which is what crediting a photo
    from another recent message is there to prevent."""
    score, _ = antiscam.score_message(PS5_SCAM, False, age(3), RULES)
    assert score == RULES["threshold"]


# --- sweep_recent: the last hour, everywhere the bot can reach ---


def http_error():
    """py-cord's HTTPException only needs a response carrying .status and .reason.
    Forbidden subclasses it, which is why sweep_recent catches the base."""
    return discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "nope")


class FakePerms:
    def __init__(self, read=True, manage=True):
        self.read_message_history = read
        self.manage_messages = manage


def posted(id, author_id=7, attachments=0):
    return SimpleNamespace(
        id=id, author=SimpleNamespace(id=author_id), attachments=[object()] * attachments
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
    channel = FakeSweepChannel("general", [posted(2), posted(3, author_id=8), posted(4)])

    result = await sweep_cog(monkeypatch).sweep_recent(
        FakeGuild(text_channels=[channel]), AUTHOR, CUTOFF
    )

    assert [m.id for m in channel.deleted_batches[0]] == [2, 4]
    assert result.deleted == 2


@pytest.mark.asyncio
async def test_sweep_hands_the_cutoff_to_discord_rather_than_filtering_by_hand(monkeypatch):
    channel = FakeSweepChannel("general", [posted(2)])

    await sweep_cog(monkeypatch).sweep_recent(
        FakeGuild(text_channels=[channel]), AUTHOR, CUTOFF
    )

    assert channel.history_calls[0] == {"after": CUTOFF, "limit": antiscam.SWEEP_LIMIT}


@pytest.mark.asyncio
async def test_sweep_never_reads_a_channel_it_could_not_clean(monkeypatch):
    """The permission check is local, so a channel the bot can't purge costs no request."""
    unreadable = FakeSweepChannel("secret", [posted(2)], perms=FakePerms(read=False))
    unmanageable = FakeSweepChannel("locked", [posted(3)], perms=FakePerms(manage=False))

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

    embed = antiscam.build_alert_embed(FakeMember(), "general", PS5_SCAM, 9, ["x"], NOW, sweep)

    fields = {f.name: f.value for f in embed.fields}
    assert fields["Also removed"] == "2 more messages in #general, #memes (1 with attachments)"


def test_alert_embed_omits_the_sweep_line_when_there_was_nothing_else():
    embed = antiscam.build_alert_embed(
        FakeMember(), "general", PS5_SCAM, 9, ["x"], NOW, antiscam.SweepResult(0, [], 0)
    )

    assert "Also removed" not in {f.name for f in embed.fields}


# --- hold(): where the sweep sits in the sequence ---


def record_sweep(monkeypatch, cog, events, result=antiscam.SweepResult(1, ["memes"], 1)):
    captured = {}

    async def sweep_recent(guild, author, cutoff):
        events.append("sweep")
        captured["cutoff"] = cutoff
        return result

    monkeypatch.setattr(cog, "sweep_recent", sweep_recent)
    return captured


@pytest.mark.asyncio
async def test_hold_sweeps_after_the_timeout_and_before_the_embed(monkeypatch):
    """The timeout goes first because the sweep is many round-trips, and an un-muted
    scammer can keep posting right through it."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    record_sweep(monkeypatch, cog, events)

    await cog.hold(message, 9, ["giveaway wording"], NOW)

    assert events == ["forward", "delete", "timeout", "sweep", "embed"]


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

    fields = {f.name: f.value for f in channel.sent[1]["embed"].fields}
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
async def test_on_message_credits_a_photo_the_author_posted_in_another_message(monkeypatch):
    """The case that prompted all of this: the pitch and the photos arrived separately, so
    the text on its own would have scored 8 rather than 9."""
    events = []
    cog, message, _ = build_cog(monkeypatch, events)
    monkeypatch.setattr(antiscam.config, "antiscam_data", RULES)
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
    assert "score 9" in message.author.timeouts[0]["reason"]
