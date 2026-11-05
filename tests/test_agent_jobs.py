"""@Miku <request> makes a PR: the trigger, the guards, where it answers, and the
job's whole life against a fake worker on the real database."""

from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from conftest import FakeInteraction, FakeMessage

from cogs import agent_jobs
from cogs.agent_jobs import AgentRequest, parse_request
from utils import config, config_schema, db

BOT = 1111
ROLE = 2222
OWNER = 3333
OTHER = 4444


# --- the trigger --------------------------------------------------------------


@pytest.mark.parametrize(
    "mention", [f"<@{BOT}>", f"<@!{BOT}>", f"<@&{ROLE}>", f"  <@{BOT}>"]
)
def test_every_mention_form_at_the_start(mention):
    request = parse_request(f"{mention} add a coinflip command", BOT, ROLE)
    assert request == AgentRequest("add a coinflip command")


def test_role_mention_needs_the_role():
    assert parse_request(f"<@&{ROLE}> add a coinflip command", BOT, None) is None


@pytest.mark.parametrize(
    "content",
    [
        f"hey <@{BOT}> add a coinflip command",
        "add a coinflip command",
        f"<@{OTHER}> add a coinflip command",
        f"<@&{OTHER}> add a coinflip command",
    ],
)
def test_only_the_start_counts(content):
    assert parse_request(content, BOT, ROLE) is None


@pytest.mark.parametrize(
    ("content", "effort", "prompt"),
    [
        ("medium effort, add a coinflip command", "medium", "add a coinflip command"),
        ("effort:max rewrite the job queue", "max", "rewrite the job queue"),
        ("effort: low add a coinflip command", "low", "add a coinflip command"),
        ("xhigh effort: add a coinflip command", "xhigh", "add a coinflip command"),
        ("Medium Effort add a coinflip command", "medium", "add a coinflip command"),
        ("max the volume slider on /music", "high", "max the volume slider on /music"),
        ("low the volume slider please", "high", "low the volume slider please"),
        (
            "add a thing, use sonnet for the parsing",
            "high",
            "add a thing, use sonnet for the parsing",
        ),
    ],
)
def test_effort_forms_and_near_misses(content, effort, prompt):
    request = parse_request(f"<@{BOT}> {content}", BOT, ROLE)
    assert request == AgentRequest(prompt, effort)


@pytest.mark.parametrize(
    "content",
    [
        "effort:huge add a thing please",
        "use sonnet, add a coinflip",
        "model:opus add a coinflip",
        "model: opus add a coinflip",
        "using opus, add a coinflip",
    ],
)
def test_model_options_and_bad_efforts_get_one_line(content):
    request = parse_request(f"<@{BOT}> {content}", BOT, ROLE)
    assert request is not None and request.error == agent_jobs.MODELS_FIXED


@pytest.mark.parametrize("content", ["", "   ", "medium effort", "effort:low"])
def test_nothing_after_the_mention_gets_usage(content):
    request = parse_request(f"<@{BOT}> {content}", BOT, ROLE)
    assert request is not None and request.error == agent_jobs.USAGE


def test_owner_id_is_not_editable_from_discord():
    assert config_schema.find("agent.enabled").kind == config_schema.Kind.BOOL
    assert config_schema.find("agent.owner_id") is None


# --- fakes ----------------------------------------------------------------------


class FakeStatus(FakeMessage):
    _next = 500

    def __init__(self, content):
        super().__init__()
        FakeStatus._next += 1
        self.id = FakeStatus._next
        self.content = content
        self.view = None

    async def edit(self, **kwargs):
        await super().edit(**kwargs)
        self.content = kwargs.get("content", self.content)


class FakeChannel:
    def __init__(self, channel_id, kind=discord.ChannelType.text):
        self.id = channel_id
        self.type = kind
        self.guild = SimpleNamespace(id=77)
        self.sent = []
        self.messages = {}

    async def send(self, content=None, **kwargs):
        message = FakeStatus(content)
        self.sent.append(content)
        self.messages[message.id] = message
        assert kwargs.get("allowed_mentions") == agent_jobs.QUIET
        return message

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")
        return self.messages[message_id]


class FakeDiscordMessage:
    _next = 9000

    def __init__(
        self,
        content,
        author=OWNER,
        channel=None,
        guild=True,
        bot=False,
        thread_fails=False,
    ):
        FakeDiscordMessage._next += 1
        self.id = FakeDiscordMessage._next
        self.content = content
        self.author = SimpleNamespace(id=author, bot=bot)
        self.channel = channel or FakeChannel(8000 + self.id)
        self.guild = (
            SimpleNamespace(id=77, self_role=SimpleNamespace(id=ROLE))
            if guild
            else None
        )
        self.replies = []
        self.threads = []
        self.thread_fails = thread_fails
        self.bot = None

    async def reply(self, content=None, **kwargs):
        assert kwargs.get("allowed_mentions") == agent_jobs.QUIET
        message = FakeStatus(content)
        self.replies.append(content)
        self.channel.messages[message.id] = message
        return message

    async def create_thread(self, name, **kwargs):
        if self.thread_fails:
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="no"), "missing permissions"
            )
        thread = FakeChannel(
            self.channel.id + 100000, discord.ChannelType.public_thread
        )
        thread.name = name
        self.threads.append(thread)
        # Discord's cache knows a thread the moment it's made
        if self.bot is not None:
            self.bot.remember(thread)
        return thread


class FakeWorker:
    """Answers like the worker's API, with a script of states per job."""

    def __init__(self):
        self.jobs = {}
        self.started = []
        self.cancelled = []
        self.down = False
        self.start_status = 201
        self.script = {}

    def check(self):
        if self.down:
            raise agent_jobs.WorkerUnavailable("connection refused")

    async def start(self, job_id, prompt, thread_url, effort):
        self.check()
        if self.start_status != 201:
            return self.start_status, {"error": "nope"}
        self.started.append((job_id, prompt, thread_url, effort))
        self.jobs[job_id] = {
            "job_id": job_id,
            "state": "running",
            "step": "planning",
            "review_round": 0,
            "elapsed": 5,
        }
        return 201, self.jobs[job_id]

    async def status(self, job_id):
        self.check()
        if job_id not in self.jobs:
            return 404, {"error": "no such job"}
        steps = self.script.get(job_id)
        if steps:
            self.jobs[job_id] = {**self.jobs[job_id], **steps.pop(0)}
        return 200, self.jobs[job_id]

    async def cancel(self, job_id):
        self.check()
        self.cancelled.append(job_id)
        self.jobs[job_id] = {**self.jobs.get(job_id, {}), "state": "cancelled"}
        return 200, self.jobs[job_id]


class FakeBot:
    def __init__(self):
        self.user = SimpleNamespace(id=BOT)
        self.channels = {}

    def remember(self, *channels):
        for channel in channels:
            self.channels[channel.id] = channel

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")


def make_cog(bot=None, worker=None):
    """A cog without its loop running, the way the other cogs' tests build theirs."""
    cog = agent_jobs.AgentJobs.__new__(agent_jobs.AgentJobs)
    cog.bot = bot or FakeBot()
    cog.worker = worker or FakeWorker()
    cog.lock = agent_jobs.asyncio.Lock()
    cog.last_status = {}
    cog.following = {}
    cog.pr_lock = agent_jobs.asyncio.Lock()
    cog.chats_resumed = False
    return cog


@pytest.fixture
def agent_settings(monkeypatch):
    monkeypatch.setitem(config.config, "agent", {"enabled": True})
    monkeypatch.setitem(
        config.secrets,
        "agent",
        {"worker_url": "http://w", "worker_token": "t", "owner_id": OWNER},
    )


# --- the guards: these never touch the database ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        lambda: FakeDiscordMessage(f"<@{BOT}> add a coinflip command", author=OTHER),
        lambda: FakeDiscordMessage(f"<@{BOT}> add a coinflip command", bot=True),
        lambda: FakeDiscordMessage(f"<@{BOT}> add a coinflip command", guild=False),
        lambda: FakeDiscordMessage(f"what does <@{BOT}> think of a coinflip command"),
        lambda: FakeDiscordMessage("add a coinflip command"),
    ],
)
async def test_the_right_nothing(agent_settings, message):
    cog = make_cog()
    msg = message()

    async def no_chat(*args):
        raise AssertionError("started a chat")

    cog.chat = no_chat
    cog.start_job = no_chat
    await cog.on_message(msg)
    assert msg.replies == []


@pytest.mark.asyncio
async def test_off_or_no_owner_means_nothing(agent_settings, monkeypatch):
    cog = make_cog()

    async def no_chat(*args):
        raise AssertionError("started a chat")

    cog.chat = no_chat
    cog.start_job = no_chat
    monkeypatch.setitem(config.config, "agent", {"enabled": False})
    await cog.on_message(FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))
    monkeypatch.delitem(config.config, "agent")
    await cog.on_message(FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))
    monkeypatch.setitem(config.config, "agent", {"enabled": True})
    monkeypatch.setitem(config.secrets, "agent", {"worker_url": "http://w"})
    await cog.on_message(FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))


@pytest.mark.asyncio
async def test_usage_and_option_errors_answer_one_line(agent_settings):
    cog = make_cog()
    short = FakeDiscordMessage(f"<@{BOT}>")
    model = FakeDiscordMessage(f"<@{BOT}> use sonnet, add a coinflip command")
    await cog.on_message(short)
    await cog.on_message(model)
    assert short.replies == [agent_jobs.USAGE]
    assert model.replies == [agent_jobs.MODELS_FIXED]


def test_missing_settings_mean_off(monkeypatch):
    monkeypatch.delitem(config.config, "agent", raising=False)
    monkeypatch.delitem(config.secrets, "agent", raising=False)
    assert agent_jobs.enabled() is False
    assert agent_jobs.owner_id() is None
    with pytest.raises(agent_jobs.WorkerUnavailable):
        agent_jobs.WorkerClient().settings()


def test_owner_id_must_be_a_number(monkeypatch):
    monkeypatch.setitem(config.secrets, "agent", {"owner_id": "3333"})
    assert agent_jobs.owner_id() is None
    monkeypatch.setitem(config.secrets, "agent", {"owner_id": True})
    assert agent_jobs.owner_id() is None


def test_messages_read_like_the_plan():
    assert agent_jobs.status_line(12, "running", "review 2", 2, "high", 1260) == (
        "Job #12: review round 2 of up to 4 (opus), high effort. 21 min."
    )
    assert (
        agent_jobs.status_line(12, "queued", None, 0, "medium", None)
        == "Job #12: queued (medium effort)."
    )
    assert (
        agent_jobs.final_message(
            {
                "state": "done",
                "pr_url": "https://github.com/x/pull/101",
                "summary": "ruff, mypy and 718 tests pass. Review approved after 2 rounds.",
                "elapsed": 1560,
            }
        )
        == "Done: https://github.com/x/pull/101\nruff, mypy and 718 tests pass. Review approved after 2 rounds. 26 min. Not run on a live bot."
    )
    draft = agent_jobs.final_message(
        {
            "state": "draft",
            "pr_url": "u",
            "stop_reason": "max_rounds",
            "open_findings": [{}],
            "elapsed": 60,
        }
    )
    assert "it used all 3 fix rounds" in draft and "1 finding still open" in draft
    assert agent_jobs.thread_name(
        12, "add a `coinflip` command that costs 10 points to everyone here"
    ) == ("add a coinflip command that costs 10 points (#12)")


# --- jobs on the real database ----------------------------------------------------


@pytest_asyncio.fixture
async def clean_jobs(migrated_db, agent_settings):
    await db.perform_one("DELETE FROM agent_jobs")
    yield
    await db.perform_one("DELETE FROM agent_jobs")


async def row(job_id):
    return await db.fetch_one(
        "SELECT status, step, review_rounds, stop_reason, pr_url, error, limit_hit, plan_summary, channel_id, status_message_id FROM agent_jobs WHERE id = %s",
        (job_id,),
    )


async def start(cog, message):
    cog.bot.remember(message.channel)
    message.bot = cog.bot
    return await cog.start_job(message, parse_request(message.content, BOT, ROLE))


@pytest.mark.asyncio
async def test_a_request_reaches_done_and_records_the_pr(clean_jobs):
    worker = FakeWorker()
    cog = make_cog(worker=worker)
    message = FakeDiscordMessage(f"<@{BOT}> medium effort add a coinflip command")
    job_id = await start(cog, message)

    # it opened a thread, started the job there, and said so in one status message
    thread = message.threads[0]
    assert thread.name == f"add a coinflip command (#{job_id})"
    assert worker.started == [
        (
            job_id,
            "add a coinflip command",
            f"https://discord.com/channels/77/{thread.id}",
            "medium",
        )
    ]
    status, _, _, _, _, _, _, _, channel_id, status_id = await row(job_id)
    assert (status, channel_id) == ("running", thread.id)
    status_message = thread.messages[status_id]

    worker.script[job_id] = [
        {"step": "coding", "plan_summary": "Add /coinflip to cogs/fun.py, 4 tests."},
        {"step": "review 2", "review_round": 2, "elapsed": 1260},
        {
            "state": "done",
            "step": "done",
            "review_round": 2,
            "pr_url": "https://github.com/nuesports/nu-esports-bot/pull/101",
            "stop_reason": "approved",
            "summary": "ruff, mypy and 718 tests pass. Review approved after 2 rounds.",
            "elapsed": 1560,
        },
    ]
    await cog.tick()
    assert thread.sent[-1] == "Plan: Add /coinflip to cogs/fun.py, 4 tests."
    assert "coding (sonnet)" in status_message.content
    await cog.tick()
    assert (
        status_message.content
        == f"Job #{job_id}: review round 2 of up to 4 (opus), medium effort. 21 min."
    )
    edits = len(status_message.edit_calls)
    await cog.tick()
    status, _, rounds, stop, pr_url, *_ = await row(job_id)
    assert (status, rounds, stop, pr_url) == (
        "done",
        2,
        "approved",
        "https://github.com/nuesports/nu-esports-bot/pull/101",
    )
    assert thread.sent[-1].startswith(
        "Done: https://github.com/nuesports/nu-esports-bot/pull/101"
    )
    assert status_message.edit_calls[-1]["view"] is None
    assert len(status_message.edit_calls) == edits + 1
    # the plan line went out once
    assert sum(1 for s in thread.sent if s.startswith("Plan:")) == 1


@pytest.mark.asyncio
async def test_status_is_only_edited_when_it_changes(clean_jobs):
    worker = FakeWorker()
    cog = make_cog(worker=worker)
    message = FakeDiscordMessage(f"<@{BOT}> add a coinflip command")
    job_id = await start(cog, message)
    *_, status_id = await row(job_id)
    status_message = message.threads[0].messages[status_id]
    await cog.tick()
    edits = len(status_message.edit_calls)
    await cog.tick()
    await cog.tick()
    assert len(status_message.edit_calls) == edits


@pytest.mark.asyncio
async def test_a_restart_loses_nothing(clean_jobs):
    worker = FakeWorker()
    first = make_cog(worker=worker)
    message = FakeDiscordMessage(f"<@{BOT}> add a coinflip command")
    job_id = await start(first, message)
    thread = message.threads[0]

    # a fresh cog, as after a deploy: same database, same worker, nothing in memory
    second = make_cog(bot=first.bot, worker=worker)
    worker.script[job_id] = [
        {"step": "checks"},
        {"state": "draft", "pr_url": "u", "stop_reason": "checks_red"},
    ]
    await second.tick()
    *_, status_id = await row(job_id)
    assert "running ruff, mypy and the tests" in thread.messages[status_id].content
    await second.tick()
    assert (await row(job_id))[0] == "draft"
    assert thread.sent[-1].startswith("Draft PR: u")


@pytest.mark.asyncio
async def test_one_at_a_time_and_queued_jobs_wait(clean_jobs):
    worker = FakeWorker()
    cog = make_cog(worker=worker)
    first = await start(cog, FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))
    second = await start(cog, FakeDiscordMessage(f"<@{BOT}> add a dice command too"))
    assert [s[0] for s in worker.started] == [first]
    assert (await row(second))[0] == "queued"
    worker.script[first] = [{"state": "no_change", "summary": "already there"}]
    await cog.tick()
    await cog.tick()
    assert [s[0] for s in worker.started] == [first, second]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "note"),
    [
        (lambda w: setattr(w, "down", True), "Waiting: the worker isn't reachable."),
        (
            lambda w: setattr(w, "start_status", 409),
            "Waiting: the worker is busy with another job.",
        ),
        (
            lambda w: setattr(w, "start_status", 503),
            "Waiting: the worker's secrets aren't set up.",
        ),
    ],
)
async def test_a_down_or_busy_worker_leaves_the_job_queued(clean_jobs, setup, note):
    worker = FakeWorker()
    setup(worker)
    cog = make_cog(worker=worker)
    message = FakeDiscordMessage(f"<@{BOT}> add a coinflip command")
    job_id = await start(cog, message)
    status, *_, status_id = await row(job_id)
    assert status == "queued"
    assert message.threads[0].messages[status_id].content.endswith(note)


@pytest.mark.asyncio
async def test_turning_it_off_stops_new_jobs_at_once(clean_jobs, monkeypatch):
    worker = FakeWorker()
    worker.down = True
    cog = make_cog(worker=worker)
    job_id = await start(cog, FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))
    worker.down = False
    # /config saves into this same dict and dispatches config_changed; nothing to reload
    monkeypatch.setitem(config.config, "agent", {"enabled": False})
    await cog.tick()
    assert worker.started == []
    assert (await row(job_id))[0] == "queued"
    monkeypatch.setitem(config.config, "agent", {"enabled": True})
    await cog.tick()
    assert [s[0] for s in worker.started] == [job_id]


@pytest.mark.asyncio
async def test_a_usage_limit_holds_the_queue(clean_jobs):
    worker = FakeWorker()
    cog = make_cog(worker=worker)
    first = await start(cog, FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))
    worker.script[first] = [
        {
            "state": "failed",
            "error": "Claude session limit hit, resets 3am (UTC)",
            "limit": "session",
        }
    ]
    await cog.tick()
    assert (await row(first))[6] == "session"
    second = await start(cog, FakeDiscordMessage(f"<@{BOT}> add a dice command too"))
    await cog.tick()
    assert (await row(second))[0] == "queued"
    await db.perform_one(
        "UPDATE agent_jobs SET finished_at = finished_at - interval '31 minutes' WHERE id = %s",
        (first,),
    )
    await cog.tick()
    assert (await row(second))[0] == "running"


@pytest.mark.asyncio
async def test_a_job_the_worker_lost_fails(clean_jobs):
    worker = FakeWorker()
    cog = make_cog(worker=worker)
    job_id = await start(cog, FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))
    worker.jobs.clear()
    await cog.tick()
    assert (await row(job_id))[:1] == ("failed",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "threaded"),
    [
        (discord.ChannelType.text, True),
        (discord.ChannelType.public_thread, False),  # a thread, or a forum post
        (discord.ChannelType.private_thread, False),
        (discord.ChannelType.voice, False),  # a voice channel's chat
    ],
)
async def test_where_it_answers(clean_jobs, kind, threaded):
    cog = make_cog()
    channel = FakeChannel(424242, kind)
    message = FakeDiscordMessage(f"<@{BOT}> add a coinflip command", channel=channel)
    job_id = await start(cog, message)
    *_, channel_id, _ = await row(job_id)
    assert bool(message.threads) == threaded
    assert channel_id == (message.threads[0].id if threaded else channel.id)
    if not threaded:
        assert message.replies and message.replies[0].startswith(f"Job #{job_id}")


@pytest.mark.asyncio
async def test_no_thread_permission_answers_in_place(clean_jobs):
    cog = make_cog()
    message = FakeDiscordMessage(f"<@{BOT}> add a coinflip command", thread_fails=True)
    job_id = await start(cog, message)
    assert (await row(job_id))[8] == message.channel.id
    assert message.replies[0].startswith(f"Job #{job_id}")


# --- Cancel -------------------------------------------------------------------------


def press(job_id, user=OWNER):
    interaction = FakeInteraction(SimpleNamespace(id=user))
    interaction.data = {"custom_id": f"{agent_jobs.CANCEL_PREFIX}{job_id}"}
    return interaction


@pytest.mark.asyncio
async def test_cancel_a_running_job(clean_jobs):
    worker = FakeWorker()
    cog = make_cog(worker=worker)
    message = FakeDiscordMessage(f"<@{BOT}> add a coinflip command")
    job_id = await start(cog, message)
    interaction = press(job_id)
    await cog.on_interaction(interaction)
    assert worker.cancelled == [job_id]
    assert (await row(job_id))[0] == "cancelled"
    assert interaction.followup.send_calls[0]["content"] == f"Cancelled job #{job_id}."
    assert message.threads[0].sent[-1] == "Cancelled."


@pytest.mark.asyncio
async def test_cancel_a_queued_job_after_a_restart(clean_jobs):
    worker = FakeWorker()
    worker.down = True
    first = make_cog(worker=worker)
    job_id = await start(first, FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))
    second = make_cog(bot=first.bot, worker=worker)
    await second.on_interaction(press(job_id))
    assert (await row(job_id))[0] == "cancelled"
    assert worker.cancelled == []


@pytest.mark.asyncio
async def test_only_the_owner_can_cancel(clean_jobs):
    worker = FakeWorker()
    cog = make_cog(worker=worker)
    job_id = await start(cog, FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))
    interaction = press(job_id, user=OTHER)
    await cog.on_interaction(interaction)
    assert interaction.response.messages[0]["ephemeral"] is True
    assert worker.cancelled == []
    assert (await row(job_id))[0] == "running"


@pytest.mark.asyncio
async def test_other_buttons_are_left_alone(clean_jobs):
    cog = make_cog()
    interaction = FakeInteraction(SimpleNamespace(id=OWNER))
    interaction.data = {"custom_id": "config-undo:5"}
    await cog.on_interaction(interaction)
    assert interaction.response.messages == [] and not interaction.response.deferred


@pytest.mark.asyncio
async def test_a_reused_job_number_fails_instead_of_borrowing_an_old_pr(clean_jobs):
    worker = FakeWorker()

    async def reused(job_id, prompt, thread_url, effort):
        return 409, {"error": "job id already used by another request"}

    worker.start = reused
    cog = make_cog(worker=worker)
    job_id = await start(cog, FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))
    status, *_ = await row(job_id)
    assert status == "failed"


def test_use_a_model_for_something_is_a_request():
    request = parse_request(f"<@{BOT}> use haiku for the /poem command", BOT, ROLE)
    assert request == AgentRequest("use haiku for the /poem command")
    assert parse_request(
        f"<@{BOT}> use sonnet, add a coinflip command", BOT, ROLE
    ).error


@pytest.mark.asyncio
async def test_a_401_at_launch_keeps_the_queue(clean_jobs):
    worker = FakeWorker()
    worker.start_status = 401
    cog = make_cog(worker=worker)
    first = await start(cog, FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))
    second = await start(cog, FakeDiscordMessage(f"<@{BOT}> add a dice command too"))
    await cog.tick()
    assert (await row(first))[0] == "queued"
    assert (await row(second))[0] == "queued"


@pytest.mark.asyncio
async def test_a_worker_error_while_running_is_shown_and_not_fatal(clean_jobs):
    worker = FakeWorker()
    cog = make_cog(worker=worker)
    message = FakeDiscordMessage(f"<@{BOT}> add a coinflip command")
    job_id = await start(cog, message)

    async def broken(job_id):
        return 500, {}

    worker.status = broken
    await cog.tick()
    status, *_, status_id = await row(job_id)
    assert status == "running"
    assert (
        message.threads[0]
        .messages[status_id]
        .content.endswith("The worker answered 500; still waiting.")
    )
    # silence long past the worker's own clock ends it, so the queue moves on
    await db.perform_one(
        "UPDATE agent_jobs SET started_at = started_at - interval '3 hours' WHERE id = %s",
        (job_id,),
    )
    await cog.tick()
    assert (await row(job_id))[0] == "failed"


@pytest.mark.asyncio
async def test_cancel_keeps_the_job_when_the_worker_says_no(clean_jobs):
    worker = FakeWorker()
    cog = make_cog(worker=worker)
    job_id = await start(cog, FakeDiscordMessage(f"<@{BOT}> add a coinflip command"))

    async def refused(job_id):
        return 401, {"error": "unauthorized"}

    worker.cancel = refused
    interaction = press(job_id)
    await cog.on_interaction(interaction)
    assert (await row(job_id))[0] == "running"
    assert "couldn't be stopped" in interaction.followup.send_calls[0]["content"]


@pytest.mark.asyncio
async def test_nowhere_to_post_means_no_job(clean_jobs):
    cog = make_cog()
    message = FakeDiscordMessage(f"<@{BOT}> add a coinflip command", thread_fails=True)

    async def no_send(*args, **kwargs):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "missing")

    message.reply = no_send
    assert await start(cog, message) is None
    assert await db.fetch_one("SELECT count(*) FROM agent_jobs") == (0,)


@pytest.mark.asyncio
async def test_a_thread_that_refuses_the_status_falls_back_in_place(clean_jobs):
    cog = make_cog()
    message = FakeDiscordMessage(f"<@{BOT}> add a coinflip command")
    original = message.create_thread

    async def thread_that_refuses(name, **kwargs):
        thread = await original(name, **kwargs)

        async def refuse(*args, **kwargs):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "missing")

        thread.send = refuse
        return thread

    message.create_thread = thread_that_refuses
    job_id = await start(cog, message)
    assert (await row(job_id))[8] == message.channel.id
    assert message.replies[0].startswith(f"Job #{job_id}")
