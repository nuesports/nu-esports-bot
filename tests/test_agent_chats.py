"""@Miku conversations: who's talking to her, the answer landing right where it was
asked (a Discord reply only when the channel moved on), and the "Make the PR"
button, against a fake worker on the real database."""

import asyncio
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from conftest import FakeInteraction
from test_agent_jobs import (
    BOT,
    DEV,
    DEV_TWO,
    FAN,
    OTHER,
    OWNER,
    FakeBot,
    FakeChannel,
    FakeDiscordMessage,
    FakeWorker,
    agent_settings,  # noqa: F401 - a fixture, used by name
    make_cog,
    person,
)

from cogs import agent_jobs
from utils import db


class ChatChannel(FakeChannel):
    def __init__(self, channel_id, kind=discord.ChannelType.text):
        super().__init__(channel_id, kind)
        self.views = []
        self.references = []
        self.typing = 0
        self.last_message_id = None

    async def send(self, content=None, **kwargs):
        message = await super().send(content, **kwargs)
        message.author = SimpleNamespace(id=BOT)
        self.views.append(kwargs.get("view"))
        self.references.append(kwargs.get("reference"))
        self.last_message_id = message.id
        return message

    async def trigger_typing(self):
        self.typing += 1


class ChatMessage(FakeDiscordMessage):
    def __init__(self, content, replying_to=None, **kwargs):
        kwargs.setdefault("channel", ChatChannel(8000 + FakeDiscordMessage._next + 1))
        super().__init__(content, **kwargs)
        self.author = person(self.author.id, bot=self.author.bot)
        self.author.display_name = "alex" if self.author.id == OWNER else "dev"
        # a Discord reply; resolved is left empty, as it is when the message isn't cached
        self.reference = (
            SimpleNamespace(message_id=replying_to.id, resolved=None)
            if replying_to is not None
            else None
        )

    async def create_thread(self, name, **kwargs):
        raise AssertionError("a conversation never opens a thread")


class ChatWorker(FakeWorker):
    """The worker's chat API: takes each message, answers when the test says so."""

    def __init__(self):
        super().__init__()
        self.said = []
        self.answers = {}
        self.say_status = 202
        self.can_pr = []
        self.contexts = []
        self.admins = []

    async def say(
        self,
        channel_id,
        message,
        author,
        message_id,
        can_pr=True,
        context="",
        admin=False,
    ):
        self.check()
        self.can_pr.append(can_pr)
        self.contexts.append(context)
        self.admins.append(admin)
        if self.say_status != 202:
            return self.say_status, {"error": "still answering the last message"}
        n = sum(1 for s in self.said if s[0] == channel_id) + 1
        self.said.append((channel_id, message, author, message_id))
        self.answers.setdefault((channel_id, n), {"state": "thinking"})
        return 202, {"chat_id": channel_id, "n": n, "state": "thinking"}

    async def answer(self, channel_id, n):
        self.check()
        if (channel_id, n) not in self.answers:
            return 404, {"error": "no such turn"}
        return 200, {"chat_id": channel_id, "n": n, **self.answers[(channel_id, n)]}


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(agent_jobs, "CHAT_POLL_SECONDS", 0.01)


@pytest_asyncio.fixture
async def clean_chats(migrated_db, agent_settings):  # noqa: F811
    for table in ("agent_chat_turns", "agent_chats", "agent_jobs"):
        await db.perform_one(f"DELETE FROM {table}")
    yield
    for table in ("agent_chat_turns", "agent_chats", "agent_jobs"):
        await db.perform_one(f"DELETE FROM {table}")


def chat_cog(worker=None):
    return make_cog(bot=FakeBot(), worker=worker or ChatWorker())


async def say(cog, message):
    """The message lands in its channel, then the bot hears it."""
    cog.bot.remember(message.channel)
    message.bot = cog.bot
    message.channel.last_message_id = message.id
    await cog.on_message(message)


async def settle(cog):
    """Lets every turn being followed finish."""
    for _ in range(200):
        tasks = [t for t in cog.following.values() if not t.done()]
        if not tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("a turn was still being followed")


async def turns():
    return await db.fetch_all(
        "SELECT id, channel_id, message_id, n, effort, status, pr_request, job_id, error FROM agent_chat_turns ORDER BY id"
    )


def done(reply, pr_request=None):
    return {"state": "done", "reply": reply, "pr_request": pr_request}


# --- who's talking to her, and where she answers ------------------------------------


@pytest.mark.asyncio
async def test_a_mention_is_answered_right_there_as_a_plain_message(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    message = ChatMessage(f"<@{BOT}> how do points work?")
    channel = message.channel
    await say(cog, message)

    assert worker.said == [(channel.id, "how do points work?", "alex", message.id)]
    ((_, channel_id, _, n, effort, status, *_),) = await turns()
    assert (channel_id, n, effort, status) == (channel.id, 1, "high", "thinking")

    worker.answers[(channel.id, 1)] = done("Points live in `cogs/points.py:12`.")
    await settle(cog)
    assert channel.sent == ["Points live in `cogs/points.py:12`."]
    # nothing came in after the question, so it isn't a Discord reply
    assert channel.references == [None]
    assert channel.typing >= 1
    assert (await turns())[0][5] == "done"


@pytest.mark.asyncio
async def test_when_the_channel_moved_on_the_answer_is_a_reply(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    message = ChatMessage(f"<@{BOT}> how do points work?")
    channel = message.channel
    await say(cog, message)
    channel.last_message_id = message.id + 50  # someone else said something meanwhile
    worker.answers[(channel.id, 1)] = done("Like this.\n\n" + "x" * 2100)
    await settle(cog)
    assert len(channel.sent) >= 2
    first, *rest = channel.references
    assert first.message_id == message.id and first.fail_if_not_exists is False
    # only the first piece is the reply; the rest follow it
    assert rest and all(r is None for r in rest)


@pytest.mark.asyncio
async def test_replying_to_her_needs_no_mention_and_nothing_else_does(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    first = ChatMessage(f"<@{BOT}> how do points work?")
    channel = first.channel
    await say(cog, first)
    worker.answers[(channel.id, 1)] = done("Like this.")
    await settle(cog)
    hers = channel.messages[channel.last_message_id]

    await say(
        cog,
        ChatMessage("and where are they stored?", channel=channel, replying_to=hers),
    )
    # a plain message in the same channel, a reply to someone else, and a stranger
    await say(cog, ChatMessage("brb", channel=channel))
    await say(cog, ChatMessage("lol", channel=channel, replying_to=first))
    await say(
        cog,
        ChatMessage("me too, miku", channel=channel, author=OTHER, replying_to=hers),
    )
    assert [s[1] for s in worker.said] == [
        "how do points work?",
        "and where are they stored?",
    ]
    assert {s[0] for s in worker.said} == {channel.id}
    assert [t[3] for t in await turns()] == [1, 2]


@pytest.mark.asyncio
async def test_short_messages_are_conversation_too(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    await say(cog, ChatMessage(f"<@{BOT}> hi"))
    assert [s[1] for s in worker.said] == ["hi"]


@pytest.mark.asyncio
async def test_threads_and_voice_chats_are_answered_in_place_too(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    for kind in (discord.ChannelType.voice, discord.ChannelType.public_thread):
        place = ChatChannel(5150 + len(worker.said), kind)
        await say(cog, ChatMessage(f"<@{BOT}> you there?", channel=place))
        assert worker.said[-1][0] == place.id


@pytest.mark.asyncio
async def test_the_same_message_twice_is_one_turn(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    message = ChatMessage(f"<@{BOT}> how do points work?")
    await say(cog, message)
    again = ChatMessage(f"<@{BOT}> dup", channel=message.channel)
    again.id = message.id
    await say(cog, again)
    assert len(worker.said) == 1 and len(await turns()) == 1


# --- when it can't answer -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_down_or_busy_worker_says_so_in_one_line(clean_chats):
    worker = ChatWorker()
    worker.down = True
    cog = chat_cog(worker)
    message = ChatMessage(f"<@{BOT}> how do points work?")
    channel = message.channel
    await say(cog, message)
    assert channel.sent == ["I can't think right now: the worker isn't reachable."]
    assert (await turns())[0][5] == "failed"

    worker.down = False
    worker.say_status = 409
    await say(cog, ChatMessage(f"<@{BOT}> and now?", channel=channel))
    assert channel.sent[-1].startswith("I'm still answering the last message here")


@pytest.mark.asyncio
async def test_a_limit_or_failure_is_posted_not_swallowed(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    message = ChatMessage(f"<@{BOT}> how do points work?")
    channel = message.channel
    await say(cog, message)
    worker.answers[(channel.id, 1)] = {
        "state": "failed",
        "error": "Claude session limit hit, resets 3am (UTC)",
        "limit": "session",
    }
    await settle(cog)
    # in her voice, with nothing about Claude or limits in it
    assert len(channel.sent) == 1 and channel.sent[0] in agent_jobs.OUT_OF_VOICE
    assert all(
        "Claude" not in line and "limit" not in line for line in agent_jobs.OUT_OF_VOICE
    )
    assert (await turns())[0][5] == "failed"


def test_a_limit_says_so_when_a_login_expired_too_and_other_failures_say_why():
    expired = agent_jobs.chat_failure(
        {
            "error": "Claude session limit hit; and the login for claude_oauth_token has expired",
            "limit": "session",
        }
    )
    line, note = expired.split("\n")
    assert line in agent_jobs.OUT_OF_VOICE and "claude setup-token" in note
    broke = agent_jobs.chat_failure(
        {"error": "the reply came back empty", "limit": None}
    )
    assert broke == "I couldn't answer that: the reply came back empty."


@pytest.mark.asyncio
async def test_a_restart_follows_turns_again_and_owns_up_to_lost_ones(clean_chats):
    worker = ChatWorker()
    first = chat_cog(worker)
    message = ChatMessage(f"<@{BOT}> how do points work?")
    channel = message.channel
    await say(first, message)
    for task in first.following.values():
        task.cancel()
    # one that was mid-POST when the bot died
    await db.perform_one(
        "INSERT INTO agent_chat_turns (channel_id, message_id, requester_id, effort, status) VALUES (%s, 1, %s, 'high', 'sending')",
        (channel.id, OWNER),
    )

    second = make_cog(bot=first.bot, worker=worker)
    worker.answers[(channel.id, 1)] = done("Like this.")
    await second.resume_chats()
    await settle(second)
    assert sorted(channel.sent) == sorted(
        ["Like this.", "I restarted before I got your last message. Send it again?"]
    )
    assert sorted(t[5] for t in await turns()) == ["done", "failed"]


# --- the answer ---------------------------------------------------------------------


def test_long_replies_split_cleanly():
    code = "```py\n" + "\n".join(f"x = {i}" for i in range(400)) + "\n```"
    pieces = agent_jobs.chunks("Here:\n" + code)
    assert len(pieces) > 1
    assert all(len(p) <= agent_jobs.DISCORD_LIMIT for p in pieces)
    assert all(p.count("```") % 2 == 0 for p in pieces)
    assert agent_jobs.chunks("short") == ["short"]


@pytest.mark.asyncio
async def test_a_change_comes_with_a_button_that_makes_one_job(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    message = ChatMessage(f"<@{BOT}> medium effort, can we add a coinflip command?")
    channel = message.channel
    await say(cog, message)
    worker.answers[(channel.id, 1)] = done(
        "Sure: a /coinflip that costs 10 points. Press the button below.",
        "Add a /coinflip command that costs 10 points.\nAdd a test.",
    )
    await settle(cog)
    assert channel.sent[0].startswith("Sure:")
    assert channel.sent[1] == (
        "The PR I'd ask for (medium effort):\n> Add a /coinflip command that costs 10 points.\n> Add a test."
    )
    button = channel.views[1].children[0]
    assert button.label == "Make the PR"
    turn_id = (await turns())[0][0]
    assert button.custom_id == f"{agent_jobs.PR_PREFIX}{turn_id}"

    def press(user=OWNER):
        interaction = FakeInteraction(person(user))
        interaction.data = {"custom_id": button.custom_id}
        interaction.message = channel.messages[max(channel.messages)]
        return interaction

    stranger = press(OTHER)
    await cog.on_interaction(stranger)
    assert stranger.response.messages[0]["ephemeral"] is True
    assert worker.started == []

    owner = press()
    await cog.on_interaction(owner)
    (job_id, prompt, thread_url, effort) = worker.started[0]
    assert prompt == "Add a /coinflip command that costs 10 points.\nAdd a test."
    assert effort == "medium"
    assert thread_url.endswith(f"/{channel.id}")
    assert owner.followup.send_calls[0]["content"] == f"Started job #{job_id}."
    assert (await turns())[0][7] == job_id
    # its status is in the conversation's channel
    assert any(s.startswith(f"Job #{job_id}") for s in channel.sent)

    again = press()
    await cog.on_interaction(again)
    assert again.followup.send_calls[0]["content"] == f"Already started: job #{job_id}."
    assert len(worker.started) == 1


@pytest.mark.asyncio
async def test_no_change_means_no_button(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    message = ChatMessage(f"<@{BOT}> what's the coinflip command?")
    channel = message.channel
    await say(cog, message)
    worker.answers[(channel.id, 1)] = done("There isn't one yet.")
    await settle(cog)
    assert channel.sent == ["There isn't one yet."] and channel.views == [None]


# --- bot devs ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_devs_talk_to_her_and_strangers_dont(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    first = ChatMessage(f"<@{BOT}> how do points work?", author=DEV)
    channel = first.channel
    await say(cog, first)
    worker.answers[(channel.id, 1)] = done("Like this.")
    await settle(cog)
    hers = channel.messages[channel.last_message_id]
    # another dev joins the same conversation by replying; a stranger can't
    await say(
        cog,
        ChatMessage("and the shop?", channel=channel, author=DEV_TWO, replying_to=hers),
    )
    await say(cog, ChatMessage(f"<@{BOT}> hi", channel=channel, author=OTHER))
    assert [(s[1], s[2]) for s in worker.said] == [
        ("how do points work?", "dev"),
        ("and the shop?", "dev"),
    ]


async def offered_by(cog, worker, author):
    message = ChatMessage(f"<@{BOT}> add a coinflip command", author=author)
    channel = message.channel
    await say(cog, message)
    worker.answers[(channel.id, 1)] = done("Sure.", "Add a /coinflip command.")
    await settle(cog)

    def press(user):
        interaction = FakeInteraction(person(user))
        interaction.data = {"custom_id": channel.views[1].children[0].custom_id}
        interaction.message = channel.messages[max(channel.messages)]
        return interaction

    return press


@pytest.mark.asyncio
async def test_the_pr_button_is_for_who_asked(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    press = await offered_by(cog, worker, DEV)
    other_dev = press(DEV_TWO)
    await cog.on_interaction(other_dev)
    assert other_dev.response.messages[0]["content"] == (
        "Only the bot dev who asked for it can start it."
    )
    assert worker.started == []
    await cog.on_interaction(press(DEV))
    assert len(worker.started) == 1
    requester = await db.fetch_one(
        "SELECT requester_id FROM agent_jobs WHERE id = %s", (worker.started[0][0],)
    )
    assert requester == (DEV,)


@pytest.mark.asyncio
async def test_the_owner_can_press_anyones_button(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    press = await offered_by(cog, worker, DEV)
    await cog.on_interaction(press(OWNER))
    assert len(worker.started) == 1


@pytest.mark.asyncio
async def test_miku_certified_chat_but_never_get_a_pr(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    first = ChatMessage(f"<@{BOT}> sing me something", author=FAN)
    channel = first.channel
    await say(cog, first)
    worker.answers[(channel.id, 1)] = done("La la la ♪")
    await settle(cog)
    hers = channel.messages[channel.last_message_id]
    # replying to her works for them too, still without PRs
    await say(cog, ChatMessage("again!", channel=channel, author=FAN, replying_to=hers))
    await say(cog, ChatMessage(f"<@{BOT}> add a coinflip", channel=channel, author=DEV))
    assert worker.can_pr == [False, False, True]


# --- what the bot hands her -------------------------------------------------------


class Guild:
    """Enough of a guild for names and lookups."""

    def __init__(self, *people):
        self.id = 77
        self.people = {p.id: p for p in people}
        self.members = list(people)

    def get_member(self, user_id):
        return self.people.get(user_id)


async def until(condition):
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


def named(user_id, name):
    member = person(user_id)
    member.name = name
    member.global_name = None
    member.display_name = name
    return member


@pytest.mark.asyncio
async def test_every_message_tells_her_this_channels_jobs(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    message = ChatMessage(f"<@{BOT}> add a coinflip command", author=DEV)
    channel = message.channel
    channel.guild = Guild(named(DEV, "caviar"))
    await say(cog, message)
    assert worker.contexts[0] == "No PR jobs from this channel yet."
    worker.answers[(channel.id, 1)] = done("Sure.", "Add a /coinflip command.")
    await settle(cog)
    # an offer nobody pressed is in the next message's note
    await say(cog, ChatMessage(f"<@{BOT}> well?", channel=channel, author=DEV))
    assert (
        '- Offered to caviar, "Make the PR" not pressed yet: Add a /coinflip command.'
        in (worker.contexts[1])
    )
    worker.answers[(channel.id, 2)] = done("Waiting on you!")
    await settle(cog)
    press = FakeInteraction(person(DEV))
    press.data = {"custom_id": channel.views[1].children[0].custom_id}
    press.message = channel.messages[max(channel.messages)]
    await cog.on_interaction(press)
    job_id = worker.started[0][0]
    await say(cog, ChatMessage(f"<@{BOT}> how's it going", channel=channel, author=DEV))
    note = worker.contexts[2]
    assert note.startswith("This channel's PR jobs, newest first:")
    assert f"- Job #{job_id}, asked by caviar: " in note
    assert "not pressed yet" not in note


@pytest.mark.asyncio
async def test_a_lookup_reads_the_server_then_answers_the_message(
    clean_chats, monkeypatch
):
    worker = ChatWorker()
    cog = chat_cog(worker)
    sid = named(9191, "siddharth7373")
    asker = named(DEV, "aiden")
    read = []

    async def fake_read(guild, who_asks, target, contains):
        read.append((who_asks.id, target.id, contains))
        return "siddharth7373's recent messages: [2026-11-04 #general] anyone wanna duo"

    monkeypatch.setattr(agent_jobs, "read_messages", fake_read)
    message = ChatMessage(f"<@{BOT}> will sid find love", author=DEV)
    channel = message.channel
    channel.guild = Guild(asker, sid)
    await say(cog, message)
    worker.answers[(channel.id, 1)] = {
        **done("one sec"),
        "lookup": {"user": "siddharth7373", "contains": None},
    }
    await until(lambda: len(worker.said) == 2)
    # the first reply isn't posted; the bot read the server and asked again
    assert channel.sent == []
    assert read == [(DEV, 9191, None)]
    assert worker.said[1][3] == -message.id
    assert "anyone wanna duo" in worker.contexts[1]
    assert worker.can_pr[1] is True
    # a lookup on the follow-up is ignored: that answer is posted, as a reply to the original
    channel.last_message_id = 1
    worker.answers[(channel.id, 2)] = {
        **done("Running the numbers... six months is ambitious, but I believe ♪"),
        "lookup": {"user": "siddharth7373", "contains": None},
    }
    await settle(cog)
    assert channel.sent == [
        "Running the numbers... six months is ambitious, but I believe ♪"
    ]
    assert channel.references[0].message_id == message.id
    assert len(read) == 1


@pytest.mark.asyncio
async def test_a_lookup_for_nobody_says_so(clean_chats, monkeypatch):
    worker = ChatWorker()
    cog = chat_cog(worker)

    async def never(*args):
        raise AssertionError("read messages for nobody")

    monkeypatch.setattr(agent_jobs, "read_messages", never)
    message = ChatMessage(f"<@{BOT}> what does ghost say", author=DEV)
    channel = message.channel
    channel.guild = Guild(named(DEV, "aiden"))
    await say(cog, message)
    worker.answers[(channel.id, 1)] = {
        **done("hm"),
        "lookup": {"user": "ghost", "contains": None},
    }
    await until(lambda: len(worker.said) == 2)
    assert "Nobody in the server matched 'ghost'." in worker.contexts[1]


def test_find_member_by_mention_id_or_name():
    sid = named(9191, "siddharth7373")
    sid.display_name = "Sid"
    guild = Guild(named(DEV, "aiden"), sid)
    for text in (
        "<@9191>",
        "<@!9191>",
        "9191",
        "siddharth7373",
        "@Siddharth7373",
        "sid",
    ):
        assert agent_jobs.find_member(guild, text) is sid, text
    assert agent_jobs.find_member(guild, "nobody") is None
    assert agent_jobs.find_member(guild, "@") is None


class Perms:
    def __init__(self, ok):
        self.view_channel = ok
        self.read_message_history = ok


class TextChannel:
    def __init__(self, name, last, messages, hidden_from=()):
        self.name = name
        self.last_message_id = last
        self.messages = messages
        self.hidden_from = set(hidden_from)

    def permissions_for(self, member):
        return Perms(member.id not in self.hidden_from)

    async def history(self, limit):
        for m in self.messages[:limit]:
            yield m


def said(author_id, text, day):
    from datetime import UTC, datetime

    return SimpleNamespace(
        author=SimpleNamespace(id=author_id),
        content=text,
        created_at=datetime(2026, 11, day, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_read_messages_only_from_channels_the_asker_can_see():
    sid = named(9191, "siddharth7373")
    asker = named(DEV, "aiden")
    me = named(BOT, "miku")
    general = TextChannel(
        "general",
        50,
        [said(9191, "anyone wanna duo", 4), said(DEV, "no", 4), said(9191, "", 3)],
    )
    secret = TextChannel(
        "officers", 99, [said(9191, "officer gossip", 4)], hidden_from=[DEV]
    )
    games = TextChannel("games", 10, [said(9191, "gg ez", 2)])
    guild = SimpleNamespace(me=me, text_channels=[games, secret, general])
    text = await agent_jobs.read_messages(guild, asker, sid, None)
    lines = text.splitlines()
    assert lines[0].startswith(
        "siddharth7373 (@siddharth7373)'s recent messages, newest first"
    )
    assert lines[1:] == [
        "[2026-11-04 #general] anyone wanna duo",
        "[2026-11-02 #games] gg ez",
    ]
    assert "officer gossip" not in text
    filtered = await agent_jobs.read_messages(guild, asker, sid, "DUO")
    assert filtered.splitlines()[1:] == ["[2026-11-04 #general] anyone wanna duo"]
    nothing = await agent_jobs.read_messages(guild, asker, sid, "leeks")
    assert nothing.endswith("(none found)")


@pytest.mark.asyncio
async def test_she_knows_when_an_admin_bot_dev_is_talking(clean_chats):
    worker = ChatWorker()
    cog = chat_cog(worker)
    boss = ChatMessage(f"<@{BOT}> nah its fine, do it", author=DEV)
    boss.author.guild_permissions.administrator = True
    await say(cog, boss)
    await say(cog, ChatMessage(f"<@{BOT}> do it", author=DEV_TWO))
    await say(cog, ChatMessage(f"<@{BOT}> do it"))
    assert worker.admins == [True, False, True]
