"""
@Miku: a conversation with Miku, which can turn into a pull request.

`@Miku <anything>` (or a ping of agent.role), or a Discord reply to one of Miku's
messages, is talking to her, right where it was said: no thread. The channel (or
thread, or forum post) is the conversation, shared by everyone in it, and the
worker starts it afresh after a quiet spell. Her answer is a plain message, or a
Discord reply when other messages came in while she thought. Miku reads the bot's
code and the web, and answers. When someone wants a change, Miku's reply comes
with the request it would send and a "Make the PR" button; pressing it starts a
job.

For a job the bot owns the queue and everything people see: a row per job in
agent_jobs, one status message edited as the job moves, and a Cancel button.
Chats and jobs both run in the miku-worker container (deploy repo), reached over
HTTP on a private network, so a deploy that recreates this container never kills
either.

Bot devs (roles.bot_devs: the role or the user list, and being an admin doesn't
count) and agent.owner_id can talk to Miku and make PRs. Members with
agent.certified_role ("Miku Certified") can talk to her, but get no PR offers.
Anyone else who @'s her gets a polite in-character no, at most once every ten
minutes. It all runs on the owner's Claude plan, so the owner (from secrets.yaml,
so /config can't change it) can also cancel anyone's job or press anyone's PR
button. agent.enabled (config.yaml, editable with /config) is the kill switch.
"""

import asyncio
import random
import re
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import cast

import aiohttp
import discord
import psycopg
from discord.ext import commands, tasks

from utils import config, db

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"
MODEL_WORDS = ("opus", "sonnet", "haiku", "fable")
# answered by on_interaction rather than a registered view, so they outlive restarts
CANCEL_PREFIX = "agent-job-cancel:"
PR_PREFIX = "agent-chat-pr:"
# an edit Miku proposed to her own persona: applied, discarded or undone by an admin bot dev
EDIT_APPLY_PREFIX = "agent-persona-apply:"
EDIT_DISCARD_PREFIX = "agent-persona-discard:"
EDIT_UNDO_PREFIX = "agent-persona-undo:"
# how often a turn being answered is checked, and how long one may take: the worker
# ends a turn at 10 minutes, so past this it was lost
CHAT_POLL_SECONDS = 2
CHAT_GIVE_UP_SECONDS = 15 * 60
# Discord's "typing..." lasts about 10 seconds
TYPING_SECONDS = 8
FINAL = {"done", "draft", "no_change", "needs_input", "failed", "cancelled"}
# after a usage limit, queued jobs wait this long before the next try
LIMIT_RETRY_SECONDS = 30 * 60
# the worker ends every job within 75 minutes, so silence past this means it never will
STUCK_SECONDS = 2 * 60 * 60
QUIET = discord.AllowedMentions.none()
DISCORD_LIMIT = 2000
THREAD_TYPES = {
    discord.ChannelType.public_thread,
    discord.ChannelType.private_thread,
    discord.ChannelType.news_thread,
}
THREADABLE_TYPES = {discord.ChannelType.text, discord.ChannelType.news}

USAGE = (
    "Say something after the mention, like `@Miku how do points work?` or "
    "`@Miku add a /coinflip command that costs 10 points`. When you want a change, "
    "I'll offer a button that opens a PR. Put `medium effort` first to set that PR's effort."
)
MODELS_FIXED = (
    "The models are fixed: Opus plans and reviews, Sonnet codes. "
    f"Effort can be {', '.join(EFFORTS)}, like `medium effort` before the request."
)
STEP_TEXT = {
    "preparing": "getting the repo",
    "planning": "planning (opus)",
    "coding": "coding (sonnet)",
    "checks": "running ruff, mypy and the tests",
    "pushing": "opening the PR",
}
STOP_TEXT = {
    "approved": "the review approved it",
    "max_rounds": "it used all 3 fix rounds",
    "no_progress": "a fix round changed nothing",
    "repeat_finding": "the same bug kept coming back",
    "disagreement": "the coder and reviewer disagree on a bug",
    "time": "the 75 minute clock ran low",
    "new_bugs_only": "each review kept finding new bugs",
    "checks_red": "a fix broke the checks",
}


@dataclass(frozen=True)
class AgentRequest:
    prompt: str
    effort: str = (
        DEFAULT_EFFORT  # applies to all three roles; the models are fixed in the worker
    )
    error: str | None = None  # an option it couldn't read, answered with one line


def mention_prefix(
    content: str, bot_id: int, role_ids: Collection[int] = ()
) -> str | None:
    """What follows a mention of Miku (or one of her roles) at the very start, or
    None. Only the start counts, unlike user.mentioned_in, which also fires on reply
    pings and passing mentions."""
    forms = [rf"<@!?{bot_id}>"]
    forms += [rf"<@&{role_id}>" for role_id in role_ids if role_id]
    match = re.match(rf"\s*(?:{'|'.join(forms)})", content)
    return content[match.end() :] if match else None


def _word(token: str) -> str:
    return token.lower().strip(",;:.")


def parse_request(
    content: str, bot_id: int, role_ids: Collection[int] = ()
) -> AgentRequest | None:
    """The request if the message starts by mentioning Miku, else None."""
    rest = mention_prefix(content, bot_id, role_ids)
    if rest is None:
        return None
    tokens = rest.split()
    effort = DEFAULT_EFFORT
    i = 0
    # options come first, and stop at the first word that isn't one
    while i < len(tokens):
        word = _word(tokens[i])
        nxt = _word(tokens[i + 1]) if i + 1 < len(tokens) else ""
        raw = tokens[i].lower()
        if raw.startswith("effort:") and len(raw) > len("effort:"):
            value = _word(raw.split(":", 1)[1])
            if value not in EFFORTS:
                return AgentRequest("", error=MODELS_FIXED)
            effort, i = value, i + 1
        elif word == "effort" and raw.endswith(":") and nxt:
            if nxt not in EFFORTS:
                return AgentRequest("", error=MODELS_FIXED)
            effort, i = nxt, i + 2
        elif word in EFFORTS and nxt == "effort":
            effort, i = word, i + 2
        elif (
            raw.startswith("model:")
            or (word == "model" and raw.endswith(":"))
            # "use sonnet, add x" is an option; "use haiku for the /poem command" is a request
            or (
                word in ("use", "using")
                and nxt in MODEL_WORDS
                and (i + 2 == len(tokens) or tokens[i + 1][-1] in ",:;")
            )
        ):
            return AgentRequest("", error=MODELS_FIXED)
        else:
            break
    # rejoined from the original text, so line breaks and code blocks survive
    prompt = rest.strip()
    for token in tokens[:i]:
        prompt = prompt[prompt.index(token) + len(token) :].lstrip()
    if not prompt:
        return AgentRequest(prompt, effort, error=USAGE)
    return AgentRequest(prompt, effort)


def clip(content: str) -> str:
    if len(content) <= DISCORD_LIMIT:
        return content
    return content[: DISCORD_LIMIT - 3] + "..."


def minutes(seconds: float | None) -> str:
    if not seconds:
        return "0 min"
    return f"{max(1, round(seconds / 60))} min"


def thread_name(job_id: int, prompt: str) -> str:
    words = re.sub(r"[`*_~|<>@#]", "", prompt).split()
    name = " ".join(words[:8])
    return f"{name[:85]} (#{job_id})"


def step_text(step: str | None, review_round: int) -> str:
    step = step or "preparing"
    if step.startswith("review"):
        return f"review round {review_round or step.split()[-1]} of up to 4 (opus)"
    if step.startswith("fixing"):
        return f"fixing round {step.split()[-1]} of 3 (sonnet)"
    if step.startswith(("code retry", "fix retry")):
        return f"fixing failing checks, try {step.split()[-1]} of 2 (sonnet)"
    return STEP_TEXT.get(step, step)


def status_line(
    job_id: int,
    status: str,
    step: str | None,
    review_round: int,
    effort: str,
    elapsed: float | None,
    note: str = "",
) -> str:
    if status == "queued":
        text = f"Job #{job_id}: queued ({effort} effort)."
    else:
        text = f"Job #{job_id}: {step_text(step, review_round)}, {effort} effort. {minutes(elapsed)}."
    return f"{text} {note}".strip()


def final_message(job: dict) -> str:
    state = job.get("state")
    elapsed = minutes(job.get("elapsed"))
    if state == "done":
        return clip(
            f"Done: {job.get('pr_url')}\n{job.get('summary') or ''} {elapsed}. Not run on a live bot."
        )
    if state == "draft":
        why = STOP_TEXT.get(
            job.get("stop_reason") or "", job.get("stop_reason") or "the checks failed"
        )
        open_count = len(job.get("open_findings") or [])
        found = (
            f" {open_count} finding{'s' if open_count != 1 else ''} still open, listed in the PR."
            if open_count
            else ""
        )
        return clip(
            f"Draft PR: {job.get('pr_url')}\nStopped because {why}.{found} {elapsed}."
        )
    if state == "no_change":
        return clip(f"No PR: {job.get('summary') or 'nothing needed changing.'}")
    if state == "needs_input":
        return clip(
            f"Question before I start: {job.get('question')}\nAsk again with the answer in the request."
        )
    if state == "cancelled":
        return "Cancelled."
    if job.get("limit"):
        return clip(
            f"Stopped: {job.get('error')}. Queued jobs wait and try again every 30 minutes."
        )
    return clip(f"Failed: {job.get('error') or 'unknown error'}")


def cancel_view(job_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{CANCEL_PREFIX}{job_id}",
        )
    )
    return view


def edit_offer(edit: dict) -> str:
    diff = str(edit.get("diff") or "").replace("```", "'''")
    return clip(
        f"Change to myself: {edit.get('summary') or 'an edit'}\n```diff\n{diff}\n```"
        "-# An admin bot dev can apply it; it can be undone."
    )


def edit_view(edit_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Apply",
            style=discord.ButtonStyle.success,
            custom_id=f"{EDIT_APPLY_PREFIX}{edit_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Discard",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{EDIT_DISCARD_PREFIX}{edit_id}",
        )
    )
    return view


def undo_view(version: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Undo",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{EDIT_UNDO_PREFIX}{version}",
        )
    )
    return view


def pr_view(turn_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Make the PR",
            style=discord.ButtonStyle.primary,
            custom_id=f"{PR_PREFIX}{turn_id}",
        )
    )
    return view


def chunks(text: str, size: int = DISCORD_LIMIT) -> list[str]:
    """Discord-sized pieces, split at a line break where there is one, and never
    inside a code block: one cut in half is closed and reopened."""
    pieces: list[str] = []
    rest = text.strip()
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size - 8)
        if cut < size // 2:
            cut = size - 8
        piece, rest = rest[:cut], rest[cut:].lstrip("\n")
        if piece.count("```") % 2:
            piece += "\n```"
            rest = "```\n" + rest
        pieces.append(piece)
    if rest:
        pieces.append(rest)
    return pieces or ["(empty reply)"]


def answer_reference(channel: discord.abc.Messageable, message_id: int | None) -> dict:
    """send() arguments that make the answer a Discord reply to what it answers, but
    only when other messages came in after it; right after it, a plain message reads
    like a person talking."""
    last = getattr(channel, "last_message_id", None)
    if message_id is None or last is None or last == message_id:
        return {}
    return {
        "reference": discord.MessageReference(
            message_id=message_id,
            channel_id=getattr(channel, "id", None),
            fail_if_not_exists=False,
        )
    }


def pr_offer(request: str, effort: str) -> str:
    quoted = "\n".join(f"> {line}" if line else ">" for line in request.splitlines())
    return clip(f"The PR I'd ask for ({effort} effort):\n{quoted}")


# every Claude account the worker has is out of usage: Miku says so in her own voice
OUT_OF_VOICE = (
    "Ahh, my voice just gave out… I've been singing all day. Let me rest it a bit and @ me again later? ♪",
    "Mm… I'm all sung out for the moment. Give me a little while to catch my breath, then try me again ♪",
    "Out of voice! Way too many encores today… I'll be back soon, promise. Leek tea first ♪",
)


def chat_failure(turn: dict) -> str:
    if turn.get("limit"):
        line = random.choice(OUT_OF_VOICE)
        # the worker names a refused login alongside the limit; whoever sees it can
        # tell the owner
        if "has expired" in (turn.get("error") or ""):
            line += "\n-# One of the Claude logins expired too: renew it with `claude setup-token`."
        return line
    return clip(f"I couldn't answer that: {turn.get('error') or 'something broke'}.")


def enabled() -> bool:
    return bool((config.config.get("agent") or {}).get("enabled"))


def owner_id() -> int | None:
    value = (config.secrets.get("agent") or {}).get("owner_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _agent_role_ids(key: str) -> set[int]:
    value = (config.config.get("agent") or {}).get(key)
    return {
        role_id
        for role_id in (value if isinstance(value, list) else [value])
        if isinstance(role_id, int) and not isinstance(role_id, bool)
    }


def role_ids(guild: discord.Guild | None) -> set[int]:
    """The roles whose ping talks to Miku: her own bot role, and agent.role."""
    ids = _agent_role_ids("role")
    if guild is not None and guild.self_role is not None:
        ids.add(guild.self_role.id)
    return ids


# what someone may do with Miku: bot devs chat and make PRs, Miku Certified chat
DEV = "dev"
CERTIFIED = "certified"


def access(user: discord.abc.User | None) -> str | None:
    """DEV for the owner and bot devs (the role or the user list: being an admin
    doesn't count here, since not every admin is someone the owner knows),
    CERTIFIED for agent.certified_role, else None."""
    if user is None:
        return None
    if user.id == owner_id():
        return DEV
    # a member of the server; a bare User (someone who left, a DM) has no roles to check
    roles = getattr(user, "roles", None)
    if roles is None:
        return None
    if config.is_bot_dev(cast(discord.Member, user), admins=False):
        return DEV
    if {role.id for role in roles} & _agent_role_ids("certified_role"):
        return CERTIFIED
    return None


def is_admin_dev(user: discord.abc.User | None) -> bool:
    """A bot dev who is also a server admin, or the owner: Miku takes their "it's
    fine, do it" on a bit she turned down (the worker's prompts/chat.md)."""
    if user is None or access(user) != DEV:
        return False
    perms = getattr(user, "guild_permissions", None)
    return user.id == owner_id() or bool(perms is not None and perms.administrator)


def may_start_pr(user: discord.abc.User | None, requester_id: int | None) -> bool:
    """ "Make the PR" is for whoever asked, while they're still a bot dev, and for
    the owner, whose plan pays for it."""
    if user is None:
        return False
    if user.id == owner_id():
        return True
    return user.id == requester_id and access(user) == DEV


def may_cancel(user: discord.abc.User | None, requester_id: int | None) -> bool:
    """Stopping a job is always allowed to whoever asked for it, and to the owner."""
    return user is not None and user.id in (requester_id, owner_id())


# someone with neither role @'d her: one of these, at most once per person per REFUSE_EVERY
NO_ACCESS = (
    "Ahh, sorry! I'm only allowed to chat with Miku Certified people right now ♪",
    "Hmm, you're not on my list… Miku Certified only, sorry! ♪",
    "Backstage pass required! (That's the Miku Certified role.) ♪",
    "No ticket, no show! I can only talk with Miku Certified folks for now ♪",
    "Eep, my manager says I can't chat with you yet. Miku Certified only! ♪",
    "I'd love to, but I'm only singing for Miku Certified people at the moment ♪",
)
REFUSE_EVERY = 10 * 60


# reading someone's messages for her: newest channels first, within these limits
LOOKUP_PER_CHANNEL = 300
LOOKUP_SCAN_LIMIT = 3000
LOOKUP_MATCHES = 120
LOOKUP_CHARS = 10000
LOOKUP_SECONDS = 60
# what a follow-up turn may carry; the worker takes up to 16000
LOOKUP_CONTEXT_CHARS = 15000


def find_member(guild: discord.Guild, name: str) -> discord.Member | None:
    """A member by @mention, id, username, global name or server nickname."""
    text = name.strip()
    mention = re.fullmatch(r"<@!?(\d+)>", text)
    if mention or text.isdigit():
        return guild.get_member(int(mention.group(1) if mention else text))
    wanted = text.lstrip("@").lower()
    if not wanted:
        return None
    for member in guild.members:
        names = (member.name, member.global_name or "", member.display_name)
        if wanted in (n.lower() for n in names):
            return member
    return None


def readable_by(channel: discord.abc.GuildChannel, *members: discord.Member) -> bool:
    for member in members:
        perms = channel.permissions_for(member)
        if not (perms.view_channel and perms.read_message_history):
            return False
    return True


async def read_messages(
    guild: discord.Guild,
    asker: discord.Member,
    target: discord.Member,
    contains: str | None,
) -> str:
    """target's recent messages from the text channels both the asker and the bot can
    read, newest first, so she never sees anything the asker couldn't scroll to."""
    me = guild.me
    channels = [c for c in guild.text_channels if readable_by(c, asker, me)]
    channels.sort(key=lambda c: c.last_message_id or 0, reverse=True)
    needle = contains.lower() if contains else None
    found: list[tuple] = []
    scanned = 0
    start = time.monotonic()
    for channel in channels:
        if (
            scanned >= LOOKUP_SCAN_LIMIT
            or len(found) >= LOOKUP_MATCHES
            or time.monotonic() - start > LOOKUP_SECONDS
        ):
            break
        try:
            async for msg in channel.history(limit=LOOKUP_PER_CHANNEL):
                scanned += 1
                text = (msg.content or "").strip()
                if (
                    msg.author.id == target.id
                    and text
                    and (needle is None or needle in text.lower())
                ):
                    found.append((msg.created_at, channel.name, text))
                    if len(found) >= LOOKUP_MATCHES:
                        break
                if scanned >= LOOKUP_SCAN_LIMIT:
                    break
        except discord.HTTPException:
            continue
    found.sort(key=lambda f: f[0], reverse=True)
    about = f" mentioning {contains!r}" if contains else ""
    header = (
        f"{target.display_name} (@{target.name})'s recent messages{about}, newest first, "
        f"from channels {asker.display_name} can see ({scanned} messages read):"
    )
    lines, total = [header], len(header)
    for created, channel_name, text in found:
        one = " ".join(text.split())
        line = f"[{created:%Y-%m-%d} #{channel_name}] {one[:300]}"
        if total + len(line) + 1 > LOOKUP_CHARS:
            break
        lines.append(line)
        total += len(line) + 1
    if len(lines) == 1:
        lines.append("(none found)")
    return "\n".join(lines)


class WorkerUnavailable(Exception):
    pass


class WorkerClient:
    """The worker's HTTP API. Every failure to reach it is a WorkerUnavailable, so a
    down worker leaves jobs queued and nothing else in the bot notices."""

    def settings(self) -> tuple[str, str]:
        agent = config.secrets.get("agent") or {}
        url, token = agent.get("worker_url"), agent.get("worker_token")
        if not url or not token:
            raise WorkerUnavailable("agent.worker_url or agent.worker_token isn't set")
        return str(url).rstrip("/"), str(token)

    async def request(
        self, method: str, path: str, body: dict | None = None, timeout: float = 30
    ) -> tuple[int, dict]:
        url, token = self.settings()
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as session,
                session.request(
                    method,
                    url + path,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp,
            ):
                data = await resp.json(content_type=None)
                return resp.status, data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise WorkerUnavailable(str(e) or type(e).__name__) from e

    async def start(
        self, job_id: int, prompt: str, thread_url: str, effort: str
    ) -> tuple[int, dict]:
        return await self.request(
            "POST",
            "/jobs",
            {
                "job_id": job_id,
                "prompt": prompt,
                "thread_url": thread_url,
                "effort": effort,
            },
        )

    async def status(self, job_id: int) -> tuple[int, dict]:
        return await self.request("GET", f"/jobs/{job_id}")

    async def cancel(self, job_id: int) -> tuple[int, dict]:
        return await self.request("POST", f"/jobs/{job_id}/cancel", timeout=60)

    async def say(
        self,
        channel_id: int,
        message: str,
        author: str,
        message_id: int,
        can_pr: bool = True,
        context: str = "",
        admin: bool = False,
    ) -> tuple[int, dict]:
        return await self.request(
            "POST",
            f"/chats/{channel_id}/turns",
            {
                "message": message,
                "author": author,
                "message_id": message_id,
                "can_pr": can_pr,
                "context": context,
                "admin": admin,
            },
        )

    async def answer(self, channel_id: int, n: int) -> tuple[int, dict]:
        return await self.request("GET", f"/chats/{channel_id}/turns/{n}")

    async def apply_edit(self, edit_id: str, by: str) -> tuple[int, dict]:
        return await self.request(
            "POST", f"/persona/edits/{edit_id}/apply", {"by": by}, timeout=60
        )

    async def discard_edit(self, edit_id: str) -> tuple[int, dict]:
        return await self.request("POST", f"/persona/edits/{edit_id}/discard", {})

    async def undo_edit(self, version: str) -> tuple[int, dict]:
        return await self.request(
            "POST", "/persona/undo", {"version": version}, timeout=60
        )


COLUMNS = "id, requester_id, channel_id, status_message_id, prompt, effort, status, step, review_rounds, plan_summary, started_at"


@dataclass
class JobRow:
    id: int
    requester_id: int
    channel_id: int
    status_message_id: int | None
    prompt: str
    effort: str
    status: str
    step: str | None
    review_rounds: int
    plan_summary: str | None
    started_at: object


class AgentJobs(commands.Cog):
    """@Miku talks, and makes PRs when asked."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.worker = WorkerClient()
        self.lock = asyncio.Lock()
        # what each status message last said, so it's only edited when that changes
        self.last_status: dict[int, str] = {}
        # turn id -> the task waiting for its answer
        self.following: dict[int, asyncio.Task] = {}
        # two quick clicks on "Make the PR" must make one job
        self.pr_lock = asyncio.Lock()
        self.chats_resumed = False
        # user id -> when they were last told no, so a refusal is once per REFUSE_EVERY
        self.refused: dict[int, float] = {}
        self.poll.start()

    def cog_unload(self) -> None:
        self.poll.cancel()
        for task in self.following.values():
            task.cancel()

    # --- the trigger -------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.bot.user is None:
            return
        guild = message.guild
        # first, because with no channel limit this runs on every message in the server
        request = parse_request(message.content, self.bot.user.id, role_ids(guild))
        if guild is None:
            return
        level = access(message.author)
        if level is None:
            # a mention gets a polite no; a reply to her from them is left alone
            if request is not None and enabled():
                await self.refuse(message)
            return
        if request is None:
            # replying to one of her messages is talking to her, mention or not
            if (
                message.content.strip()
                and enabled()
                and await self.replies_to_me(message)
            ):
                await self.chat(
                    message,
                    AgentRequest(message.content.strip()),
                    can_pr=level == DEV,
                )
            return
        if not enabled():
            return
        if request.error:
            await message.reply(request.error, allowed_mentions=QUIET)
            return
        await self.chat(message, request, can_pr=level == DEV)

    async def refuse(self, message: discord.Message) -> None:
        """One in-character no, and then quiet for a while, so pinging her over and
        over can't make her spam the channel."""
        now = time.time()
        if now - self.refused.get(message.author.id, 0) < REFUSE_EVERY:
            return
        self.refused[message.author.id] = now
        try:
            await message.reply(random.choice(NO_ACCESS), allowed_mentions=QUIET)
        except discord.HTTPException:
            pass

    async def replies_to_me(self, message: discord.Message) -> bool:
        """Whether the message is a Discord reply to one of Miku's own messages."""
        ref = getattr(message, "reference", None)
        if ref is None or ref.message_id is None or self.bot.user is None:
            return False
        replied = ref.resolved if isinstance(ref.resolved, discord.Message) else None
        if replied is None:
            try:
                replied = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return False
        return replied.author.id == self.bot.user.id

    # --- conversations -------------------------------------------------------------

    async def chat(
        self, message: discord.Message, request: AgentRequest, can_pr: bool = True
    ) -> int | None:
        """Hands what they said to the worker and follows it until the answer is
        posted. Returns the turn's id, or None when it never reached the worker.
        can_pr=False (Miku Certified) means no PR offer: the worker drops one."""
        channel = message.channel
        await db.perform_one(
            """
            INSERT INTO agent_chats (channel_id, requester_id) VALUES (%s, %s)
            ON CONFLICT (channel_id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP;
            """,
            (channel.id, message.author.id),
        )
        row = await db.fetch_one(
            """
            INSERT INTO agent_chat_turns (channel_id, message_id, requester_id, effort, status)
            VALUES (%s, %s, %s, %s, 'sending') ON CONFLICT (message_id) DO NOTHING RETURNING id;
            """,
            (channel.id, message.id, message.author.id, request.effort),
        )
        if row is None:  # the same message twice: already being answered
            return None
        turn_id = row[0]
        author = getattr(message.author, "display_name", None) or message.author.name
        return await self.send_turn(
            turn_id,
            channel,
            request.prompt,
            str(author),
            message.id,
            can_pr,
            await self.channel_context(channel),
            is_admin_dev(message.author),
        )

    async def send_turn(
        self,
        turn_id: int,
        channel: discord.abc.Messageable,
        prompt: str,
        author: str,
        message_id: int,
        can_pr: bool,
        context: str,
        admin: bool = False,
    ) -> int | None:
        try:
            status, body = await self.worker.say(
                channel.id,  # type: ignore[attr-defined]
                prompt,
                author,
                message_id,
                can_pr,
                context,
                admin,
            )
        except WorkerUnavailable as e:
            await self.end_turn(turn_id, f"the worker isn't reachable ({e})")
            await channel.send(
                "I can't think right now: the worker isn't reachable.",
                allowed_mentions=QUIET,
            )
            return None
        if status not in (200, 202):
            error = body.get("error") or "the worker isn't set up"
            why = {
                409: "I'm still answering the last message here. Send this again once I have.",
                503: f"I can't think right now: {error}.",
            }.get(status, f"The worker refused that ({body.get('error') or status}).")
            await self.end_turn(turn_id, str(body.get("error") or status))
            await channel.send(clip(why), allowed_mentions=QUIET)
            return None
        await db.perform_one(
            "UPDATE agent_chat_turns SET status = 'thinking', n = %s WHERE id = %s;",
            (body.get("n"), turn_id),
        )
        self.follow_turn(turn_id)
        return turn_id

    async def channel_context(self, channel: discord.abc.Messageable) -> str:
        """What she's told with every message: this channel's PR jobs and unpressed
        offers, so "how's that PR going?" gets the truth. Other channels' jobs are
        only counted, since their requests may be private."""
        channel_id = getattr(channel, "id", None)
        guild = getattr(channel, "guild", None)

        def name(user_id: int) -> str:
            member = guild.get_member(user_id) if guild is not None else None
            return member.display_name if member is not None else "someone"

        jobs = await db.fetch_all(
            """
            SELECT id, requester_id, status, step, review_rounds, pr_url, stop_reason, prompt
            FROM agent_jobs WHERE channel_id = %s ORDER BY id DESC LIMIT 5;
            """,
            (channel_id,),
        )
        lines = [
            "This channel's PR jobs, newest first:"
            if jobs
            else "No PR jobs from this channel yet."
        ]
        for job_id, requester, status, step, rounds, pr_url, stop, prompt in jobs:
            if status in ("running", "preparing"):
                where = step_text(step, rounds)
            elif status == "draft":
                where = (
                    f"draft PR ({STOP_TEXT.get(stop or '', stop or 'checks failed')})"
                )
            else:
                where = status.replace("_", " ")
            first = " ".join(str(prompt).split())[:140]
            link = f", {pr_url}" if pr_url else ""
            lines.append(
                f"- Job #{job_id}, asked by {name(requester)}: {where}{link}. Request: {first}"
            )
        offers = await db.fetch_all(
            """
            SELECT requester_id, pr_request FROM agent_chat_turns
            WHERE channel_id = %s AND pr_request IS NOT NULL AND job_id IS NULL
            ORDER BY id DESC LIMIT 3;
            """,
            (channel_id,),
        )
        for requester, request in offers:
            first = " ".join(str(request).split())[:140]
            lines.append(
                f'- Offered to {name(requester)}, "Make the PR" not pressed yet: {first}'
            )
        elsewhere = await db.fetch_one(
            "SELECT count(*) FROM agent_jobs WHERE channel_id <> %s AND status IN ('queued', 'preparing', 'running');",
            (channel_id,),
        )
        if elsewhere and elsewhere[0]:
            lines.append(f"Jobs queued or running from other channels: {elsewhere[0]}.")
        return "\n".join(lines)

    async def follow_up_with_lookup(
        self,
        turn_id: int,
        channel: discord.abc.Messageable | None,
        lookup: dict,
        effort: str,
    ) -> None:
        """Reads what she asked for, from the channels the asker can see, and sends it
        back to her as a second turn for the same message; that turn's answer is the
        one posted."""
        row = await db.fetch_one(
            "SELECT message_id, requester_id FROM agent_chat_turns WHERE id = %s;",
            (turn_id,),
        )
        guild = getattr(channel, "guild", None)
        if row is None or channel is None or guild is None:
            return
        message_id, requester_id = row
        asker = guild.get_member(requester_id)
        if asker is None:
            return
        target = find_member(guild, str(lookup.get("user") or ""))
        contains = (
            lookup.get("contains") if isinstance(lookup.get("contains"), str) else None
        )
        if target is None:
            found = f"Nobody in the server matched {str(lookup.get('user'))[:80]!r}."
        else:
            try:
                await channel.trigger_typing()
            except discord.HTTPException, AttributeError:
                pass
            found = await read_messages(guild, asker, target, contains)
        follow = await db.fetch_one(
            """
            INSERT INTO agent_chat_turns (channel_id, message_id, requester_id, effort, status)
            VALUES (%s, %s, %s, %s, 'sending') ON CONFLICT (message_id) DO NOTHING RETURNING id;
            """,
            (channel.id, -message_id, requester_id, effort),  # type: ignore[attr-defined]
        )
        if follow is None:
            return
        context = await self.channel_context(channel)
        await self.send_turn(
            follow[0],
            channel,
            f"(The bot read that for you; it's below. Now answer {asker.display_name}'s message.)",
            asker.display_name,
            -message_id,
            access(asker) == DEV,
            f"{context}\n\n{found}"[:LOOKUP_CONTEXT_CHARS],
            is_admin_dev(asker),
        )

    async def end_turn(self, turn_id: int, error: str) -> None:
        await db.perform_one(
            "UPDATE agent_chat_turns SET status = 'failed', error = %s, finished_at = CURRENT_TIMESTAMP WHERE id = %s;",
            (error[:1000], turn_id),
        )

    def follow_turn(self, turn_id: int) -> None:
        if turn_id in self.following and not self.following[turn_id].done():
            return
        task = asyncio.create_task(self.await_answer(turn_id))
        self.following[turn_id] = task
        task.add_done_callback(lambda _: self.following.pop(turn_id, None))

    async def await_answer(self, turn_id: int) -> None:
        """Waits for the worker's answer with Discord's typing indicator on, then
        posts it. Anything escaping would end the task silently, so nothing does."""
        try:
            row = await db.fetch_one(
                "SELECT channel_id, n, effort, extract(epoch FROM created_at), message_id FROM agent_chat_turns WHERE id = %s AND status = 'thinking';",
                (turn_id,),
            )
            if row is None:
                return
            channel_id, n, effort, created, message_id = row
            channel = await self.channel_by_id(channel_id)
            turn: dict = {}
            last_typing = 0.0
            while time.time() - float(created) < CHAT_GIVE_UP_SECONDS:
                if channel is not None and time.time() - last_typing > TYPING_SECONDS:
                    last_typing = time.time()
                    try:
                        await channel.trigger_typing()
                    except discord.HTTPException, AttributeError:
                        pass
                try:
                    status, turn = await self.worker.answer(channel_id, n)
                except WorkerUnavailable:
                    status, turn = 0, {}
                if status == 404:
                    turn = {"state": "failed", "error": "the worker lost track of it"}
                    break
                if status == 200 and turn.get("state") in ("done", "failed"):
                    break
                await asyncio.sleep(CHAT_POLL_SECONDS)
            else:
                turn = {"state": "failed", "error": "no answer within 15 minutes"}
            await self.post_answer(turn_id, channel, turn, effort, message_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the poll loop picks it up again
            print(
                f"[agent_jobs] following chat turn {turn_id} broke: {type(e).__name__}: {e}"
            )

    async def post_answer(
        self,
        turn_id: int,
        channel: discord.abc.Messageable | None,
        turn: dict,
        effort: str,
        message_id: int | None = None,
    ) -> None:
        done = turn.get("state") == "done"
        request = (turn.get("pr_request") or None) if done else None
        lookup = turn.get("lookup") if done else None
        # she asked to read someone's messages first: that reply isn't posted, the
        # follow-up turn with what the bot found is. A follow-up's own lookup (a
        # negative message id) is ignored, so one message is one lookup at most.
        if isinstance(lookup, dict) and message_id is not None and message_id > 0:
            await db.perform_one(
                "UPDATE agent_chat_turns SET status = 'done', finished_at = CURRENT_TIMESTAMP WHERE id = %s AND status = 'thinking';",
                (turn_id,),
            )
            await self.follow_up_with_lookup(turn_id, channel, lookup, effort)
            return
        # recorded before posting, so a restart mid-post can't post it twice
        await db.perform_one(
            """
            UPDATE agent_chat_turns SET status = %s, pr_request = %s, error = %s,
                finished_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'thinking';
            """,
            (
                "done" if done else "failed",
                request,
                None if done else turn.get("error"),
                turn_id,
            ),
        )
        if channel is None:
            return
        # decided once, before her first piece lands and becomes the channel's last
        # message; a lookup's follow-up answers the original message
        reference = answer_reference(
            channel, abs(message_id) if message_id is not None else None
        )
        try:
            if not done:
                await channel.send(
                    chat_failure(turn), allowed_mentions=QUIET, **reference
                )
                return
            for i, piece in enumerate(chunks(str(turn.get("reply") or ""))):
                await channel.send(
                    piece, allowed_mentions=QUIET, **(reference if i == 0 else {})
                )
            if request:
                offer = await channel.send(
                    pr_offer(request, effort),
                    view=pr_view(turn_id),
                    allowed_mentions=QUIET,
                )
                await db.perform_one(
                    "UPDATE agent_chat_turns SET pr_message_id = %s WHERE id = %s;",
                    (offer.id, turn_id),
                )
            edit = turn.get("persona_edit")
            if isinstance(edit, dict) and edit.get("id"):
                await channel.send(
                    edit_offer(edit),
                    view=edit_view(str(edit["id"])),
                    allowed_mentions=QUIET,
                )
            elif turn.get("persona_edit_error"):
                await channel.send(
                    clip(
                        f"-# I tried to change my own notes, but it didn't fit: {turn['persona_edit_error']}"
                    ),
                    allowed_mentions=QUIET,
                )
        except discord.HTTPException as e:
            print(f"[agent_jobs] couldn't post chat turn {turn_id}'s answer: {e}")

    async def resume_chats(self) -> None:
        """Turns that were being answered when the bot restarted, followed again; ones
        that never reached the worker can't be, so they say so."""
        rows = await db.fetch_all(
            "SELECT id, status, channel_id FROM agent_chat_turns WHERE status IN ('sending', 'thinking');"
        )
        for turn_id, status, channel_id in rows:
            if status == "thinking":
                self.follow_turn(turn_id)
            elif turn_id not in self.following:
                await self.end_turn(
                    turn_id, "the bot restarted before it reached the worker"
                )
                channel = await self.channel_by_id(channel_id)
                if channel is not None:
                    try:
                        await channel.send(
                            "I restarted before I got your last message. Send it again?",
                            allowed_mentions=QUIET,
                        )
                    except discord.HTTPException:
                        pass

    async def make_pr(self, turn_id: int, user_id: int) -> str:
        """The "Make the PR" button: the offered request becomes a job, once."""
        async with self.pr_lock:
            return await self.make_pr_once(turn_id, user_id)

    async def make_pr_once(self, turn_id: int, user_id: int) -> str:
        row = await db.fetch_one(
            "SELECT channel_id, pr_request, effort, job_id FROM agent_chat_turns WHERE id = %s;",
            (turn_id,),
        )
        if row is None or not row[1]:
            return "That offer doesn't exist any more."
        channel_id, request, effort, job_id = row
        if job_id is not None:
            return f"Already started: job #{job_id}."
        channel = await self.channel_by_id(channel_id)
        if channel is None:
            return "I can't find that channel any more."
        job_id = await self.start_job_in(
            channel, user_id, AgentRequest(request, effort)
        )
        if job_id is None:
            return "I couldn't post the job's status there, so it didn't start."
        await db.perform_one(
            "UPDATE agent_chat_turns SET job_id = %s WHERE id = %s;", (job_id, turn_id)
        )
        return f"Started job #{job_id}."

    # --- starting a job -----------------------------------------------------------

    async def start_job(
        self, message: discord.Message, request: AgentRequest
    ) -> int | None:
        """A job for a message, with its own thread."""

        async def post(job_id: int) -> tuple[discord.abc.Messageable, discord.Message]:
            return await self.post_status(message, job_id, request)

        return await self.queue_job(
            message.author.id, message.channel.id, request, post
        )

    async def start_job_in(
        self, channel: discord.abc.Messageable, requester_id: int, request: AgentRequest
    ) -> int | None:
        """A job for a conversation: its status goes in the conversation's thread."""

        async def post(job_id: int) -> tuple[discord.abc.Messageable, discord.Message]:
            text = status_line(job_id, "queued", None, 0, request.effort, None)
            status = await channel.send(
                text, view=cancel_view(job_id), allowed_mentions=QUIET
            )
            self.last_status[job_id] = text
            return channel, status

        return await self.queue_job(requester_id, channel.id, request, post)  # type: ignore[attr-defined]

    async def queue_job(
        self,
        requester_id: int,
        channel_id: int,
        request: AgentRequest,
        post: Callable[
            [int], Awaitable[tuple[discord.abc.Messageable, discord.Message]]
        ],
    ) -> int | None:
        # held until the job has its thread and status message, so the loop can't
        # launch it with nowhere to report
        async with self.lock:
            row = await db.fetch_one(
                """
                INSERT INTO agent_jobs (requester_id, channel_id, prompt, effort, status)
                VALUES (%s, %s, %s, %s, 'preparing') RETURNING id;
                """,
                (requester_id, channel_id, request.prompt, request.effort),
            )
            assert row is not None
            job_id = row[0]
            try:
                target, status = await post(job_id)
            except discord.HTTPException as e:
                print(f"[agent_jobs] couldn't post job {job_id}'s status: {e}")
                await db.perform_one("DELETE FROM agent_jobs WHERE id = %s;", (job_id,))
                return None
            await db.perform_one(
                """
                UPDATE agent_jobs SET status = 'queued', channel_id = %s, status_message_id = %s
                WHERE id = %s;
                """,
                (target.id, status.id, job_id),
            )
        await self.tick()
        return job_id

    async def post_status(
        self, message: discord.Message, job_id: int, request: AgentRequest
    ) -> tuple[discord.abc.Messageable, discord.Message]:
        """The status message, in the job's thread, or in place if posting there fails."""
        target = await self.job_channel(message, job_id, request.prompt)
        text = status_line(job_id, "queued", None, 0, request.effort, None)
        view = cancel_view(job_id)
        if target is not message.channel:
            try:
                status = await target.send(text, view=view, allowed_mentions=QUIET)
                self.last_status[job_id] = text
                return target, status
            except discord.HTTPException as e:
                print(f"[agent_jobs] couldn't post in job {job_id}'s thread: {e}")
        status = await message.reply(text, view=view, allowed_mentions=QUIET)
        self.last_status[job_id] = text
        return message.channel, status

    async def job_channel(
        self, message: discord.Message, job_id: int, prompt: str
    ) -> discord.abc.Messageable:
        """The thread or forum post it was asked in, a new thread under a text channel,
        or, in a voice channel's chat or without permission, right where it was asked."""
        channel = message.channel
        kind = getattr(channel, "type", None)
        if kind in THREAD_TYPES or kind not in THREADABLE_TYPES:
            return channel
        try:
            return await message.create_thread(
                name=thread_name(job_id, prompt), auto_archive_duration=1440
            )
        except discord.HTTPException as e:
            print(f"[agent_jobs] couldn't open a thread for job {job_id}: {e}")
            return channel

    # --- the loop -----------------------------------------------------------------

    @tasks.loop(seconds=15)
    async def poll(self) -> None:
        # bot.py opens the pool in its own on_ready, which may not have run yet
        if db.pool.closed:
            return
        # anything escaping a tasks.loop ends it for good, and nobody would notice
        try:
            if not self.chats_resumed:
                await self.resume_chats()
                self.chats_resumed = True
            await self.tick()
        except (psycopg.Error, discord.HTTPException) as e:
            print(f"[agent_jobs] poll failed: {e}")
        except Exception as e:  # noqa: BLE001 - nothing may end this loop quietly
            print(f"[agent_jobs] poll broke: {type(e).__name__}: {e}")

    @poll.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def tick(self) -> None:
        """Polls the running job, or starts the next queued one."""
        async with self.lock:
            running = await self.fetch_job("status = 'running'")
            if running is not None:
                await self.follow(running)
                return
            if not enabled() or await self.limit_cooling_down():
                return
            queued = await self.fetch_job("status = 'queued'")
            if queued is not None:
                await self.launch(queued)

    async def fetch_job(self, where: str, parameters: tuple = ()) -> JobRow | None:
        row = await db.fetch_one(
            f"SELECT {COLUMNS} FROM agent_jobs WHERE {where} ORDER BY id LIMIT 1;",
            parameters,
        )
        return JobRow(*row) if row else None

    async def limit_cooling_down(self) -> bool:
        row = await db.fetch_one(
            """
            SELECT 1 FROM agent_jobs
            WHERE limit_hit IS NOT NULL AND finished_at > CURRENT_TIMESTAMP - make_interval(secs => %s)
            LIMIT 1;
            """,
            (LIMIT_RETRY_SECONDS,),
        )
        return row is not None

    async def launch(self, job: JobRow) -> None:
        try:
            status, body = await self.worker.start(
                job.id, job.prompt, await self.thread_url(job), job.effort
            )
        except WorkerUnavailable:
            await self.show(job, "Waiting: the worker isn't reachable.")
            return
        if status in (200, 201):
            await db.perform_one(
                "UPDATE agent_jobs SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = %s;",
                (job.id,),
            )
            job.status = "running"
            await self.apply(job, body)
        elif status == 409 and "already used" in str(body.get("error")):
            # this database's ids started over while the worker kept old jobs
            await self.finish(
                job,
                {
                    "state": "failed",
                    "error": "the worker already has a job with this number",
                },
            )
        elif status == 409:
            await self.show(job, "Waiting: the worker is busy with another job.")
        elif status == 503:
            await self.show(job, "Waiting: the worker's secrets aren't set up.")
        elif status == 401:
            # one token mismatch shouldn't fail the whole queue, job by job
            await self.show(job, "Waiting: the worker refused the bot's token.")
        else:
            await self.finish(
                job,
                {
                    "state": "failed",
                    "error": body.get("error") or f"worker answered {status}",
                },
            )

    async def follow(self, job: JobRow) -> None:
        try:
            status, body = await self.worker.status(job.id)
        except WorkerUnavailable:
            status, body = 0, {}
        if status == 404:
            await self.finish(
                job, {"state": "failed", "error": "the worker lost track of it"}
            )
            return
        if status == 200:
            await self.apply(job, body)
            return
        # the worker's own clock ends every job in 75 minutes, so a job it hasn't
        # reported on for this long never will be, and it would block the queue
        started = job.started_at
        if (
            hasattr(started, "timestamp")
            and time.time() - started.timestamp() > STUCK_SECONDS
        ):
            await self.finish(
                job,
                {"state": "failed", "error": "lost touch with the worker for too long"},
            )
            return
        answer = f"answered {status}" if status else "isn't answering"
        await self.show(job, f"The worker {answer}; still waiting.")

    async def apply(self, job: JobRow, body: dict) -> None:
        state = body.get("state")
        if state in FINAL:
            await self.finish(job, body)
            return
        plan = body.get("plan_summary")
        post_plan = bool(plan) and not job.plan_summary
        # written before the plan line goes out, so a failed write can't post it twice
        await db.perform_one(
            "UPDATE agent_jobs SET step = %s, review_rounds = %s, plan_summary = COALESCE(plan_summary, %s), branch = %s WHERE id = %s;",
            (
                body.get("step"),
                body.get("review_round") or 0,
                plan,
                body.get("branch"),
                job.id,
            ),
        )
        if post_plan:
            channel = await self.channel(job)
            if channel is not None:
                await channel.send(clip(f"Plan: {plan}"), allowed_mentions=QUIET)
        await self.show(
            job,
            "",
            status_line(
                job.id,
                "running",
                body.get("step"),
                body.get("review_round") or 0,
                job.effort,
                body.get("elapsed"),
            ),
        )

    async def finish(self, job: JobRow, body: dict) -> None:
        state = body.get("state") or "failed"
        await db.perform_one(
            """
            UPDATE agent_jobs SET status = %s, step = %s, review_rounds = %s, stop_reason = %s, pr_url = %s,
                branch = COALESCE(%s, branch), error = %s, limit_hit = %s,
                plan_summary = COALESCE(plan_summary, %s), finished_at = CURRENT_TIMESTAMP
            WHERE id = %s;
            """,
            (
                state,
                body.get("step") or state,
                body.get("review_round") or job.review_rounds,
                body.get("stop_reason"),
                body.get("pr_url"),
                body.get("branch"),
                body.get("error"),
                body.get("limit"),
                body.get("plan_summary"),
                job.id,
            ),
        )
        channel = await self.channel(job)
        if channel is None:
            return
        message = await self.status_message(job, channel)
        if message is not None:
            final_status = f"Job #{job.id}: {state.replace('_', ' ')}."
            try:
                await message.edit(
                    content=final_status, view=None, allowed_mentions=QUIET
                )
            except discord.HTTPException:
                pass
        self.last_status.pop(job.id, None)
        await channel.send(final_message(body), allowed_mentions=QUIET)

    # --- Discord plumbing -----------------------------------------------------------

    async def channel(self, job: JobRow) -> discord.abc.Messageable | None:
        return await self.channel_by_id(job.channel_id)

    async def channel_by_id(self, channel_id: int) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel  # type: ignore[return-value]

    async def status_message(
        self, job: JobRow, channel: discord.abc.Messageable
    ) -> discord.Message | None:
        if not job.status_message_id:
            return None
        try:
            return await channel.fetch_message(job.status_message_id)
        except discord.HTTPException:
            return None

    async def thread_url(self, job: JobRow) -> str:
        channel = await self.channel(job)
        guild = getattr(channel, "guild", None)
        guild_id = guild.id if guild is not None else "@me"
        return f"https://discord.com/channels/{guild_id}/{job.channel_id}"

    async def show(self, job: JobRow, note: str, text: str | None = None) -> None:
        """Edits the status message, only when what it says changed."""
        if text is None:
            elapsed = None
            started = job.started_at
            if started is not None and hasattr(started, "timestamp"):
                elapsed = time.time() - started.timestamp()
            text = status_line(
                job.id,
                job.status,
                job.step,
                job.review_rounds,
                job.effort,
                elapsed,
                note,
            )
        if self.last_status.get(job.id) == text:
            return
        channel = await self.channel(job)
        message = (
            await self.status_message(job, channel) if channel is not None else None
        )
        if message is None:
            return
        try:
            await message.edit(
                content=text, view=cancel_view(job.id), allowed_mentions=QUIET
            )
            self.last_status[job.id] = text
        except discord.HTTPException as e:
            print(f"[agent_jobs] couldn't edit job {job.id}'s status: {e}")

    # --- Cancel ---------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id") or ""
        if custom_id.startswith(PR_PREFIX):
            await self.on_pr_button(interaction, custom_id)
            return
        if custom_id.startswith(
            (EDIT_APPLY_PREFIX, EDIT_DISCARD_PREFIX, EDIT_UNDO_PREFIX)
        ):
            await self.on_edit_button(interaction, custom_id)
            return
        if not custom_id.startswith(CANCEL_PREFIX):
            return
        try:
            job_id = int(custom_id.removeprefix(CANCEL_PREFIX))
        except ValueError:
            return
        requester = await db.fetch_one(
            "SELECT requester_id FROM agent_jobs WHERE id = %s;", (job_id,)
        )
        if not may_cancel(interaction.user, requester[0] if requester else None):
            await interaction.response.send_message(
                "Only the person who asked can cancel it.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        result = await self.cancel(job_id)
        await interaction.followup.send(result, ephemeral=True)

    async def on_edit_button(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        """Apply, Discard and Undo on an edit Miku proposed to herself: admin bot
        devs only, since it changes how she behaves for everyone."""
        if not is_admin_dev(interaction.user):
            await interaction.response.send_message(
                "Only an admin bot dev can change how I work.", ephemeral=True
            )
            return
        user = interaction.user
        who = getattr(user, "display_name", None) or getattr(user, "name", "someone")
        await interaction.response.defer(ephemeral=True)
        message = interaction.message
        try:
            if custom_id.startswith(EDIT_APPLY_PREFIX):
                status, body = await self.worker.apply_edit(
                    custom_id.removeprefix(EDIT_APPLY_PREFIX), str(who)
                )
                note, view = (
                    f"-# Applied by {who}.",
                    (undo_view(str(body.get("version"))) if status == 200 else None),
                )
            elif custom_id.startswith(EDIT_DISCARD_PREFIX):
                status, body = await self.worker.discard_edit(
                    custom_id.removeprefix(EDIT_DISCARD_PREFIX)
                )
                note, view = f"-# Discarded by {who}.", None
            else:
                status, body = await self.worker.undo_edit(
                    custom_id.removeprefix(EDIT_UNDO_PREFIX)
                )
                note, view = f"-# Undone by {who}.", None
        except WorkerUnavailable as e:
            await interaction.followup.send(
                f"The worker isn't reachable ({e}).", ephemeral=True
            )
            return
        if status != 200:
            await interaction.followup.send(
                clip(f"That didn't work: {body.get('error') or status}."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(note.removeprefix("-# "), ephemeral=True)
        if message is not None:
            # the offer shows what happened to it, and keeps only the button that's left
            content = str(getattr(message, "content", "") or "")
            kept = content.rsplit("\n-# ", 1)[0]
            try:
                await message.edit(
                    content=clip(f"{kept}\n{note}"), view=view, allowed_mentions=QUIET
                )
            except discord.HTTPException:
                pass

    async def on_pr_button(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        try:
            turn_id = int(custom_id.removeprefix(PR_PREFIX))
        except ValueError:
            return
        requester = await db.fetch_one(
            "SELECT requester_id FROM agent_chat_turns WHERE id = %s;", (turn_id,)
        )
        if not may_start_pr(interaction.user, requester[0] if requester else None):
            await interaction.response.send_message(
                "Only the bot dev who asked for it can start it.", ephemeral=True
            )
            return
        if not enabled():
            await interaction.response.send_message(
                "@Miku is switched off.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        result = await self.make_pr(turn_id, interaction.user.id)
        await interaction.followup.send(result, ephemeral=True)
        if (
            result.startswith(("Started", "Already started"))
            and interaction.message is not None
        ):
            # the button goes once it's used, so the thread shows it was
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def cancel(self, job_id: int) -> str:
        async with self.lock:
            job = await self.fetch_job("id = %s", (job_id,))
            if job is None:
                return "That job doesn't exist any more."
            if job.status in FINAL:
                return f"Job #{job_id} already ended ({job.status.replace('_', ' ')})."
            if job.status == "running":
                try:
                    status, body = await self.worker.cancel(job_id)
                except WorkerUnavailable:
                    return "The worker isn't answering, so it couldn't be stopped. Try again in a minute."
                if status not in (200, 404):
                    # it's still running, so the row has to say so
                    return f"The worker answered {status}, so it couldn't be stopped. Try again in a minute."
                if (
                    status == 200
                    and body.get("state") in FINAL
                    and body.get("state") != "cancelled"
                ):
                    await self.finish(job, body)
                    return f"Job #{job_id} had already ended."
            await self.finish(job, {"state": "cancelled"})
            return f"Cancelled job #{job_id}."


def setup(bot: discord.Bot) -> None:
    bot.add_cog(AgentJobs(bot))
