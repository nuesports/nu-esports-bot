"""
@Miku <request> turns into a pull request.

The bot owns the queue and everything people see: a row per job in agent_jobs, a
thread, one status message edited as the job moves, and a Cancel button. The run
itself happens in the miku-worker container (deploy repo), reached over HTTP on a
private network, so a deploy that recreates this container never kills a job.

Only agent.owner_id (from secrets.yaml, so /config can't change it) can start one.
agent.enabled (config.yaml, editable with /config) is the kill switch.
"""

import asyncio
import re
import time
from dataclasses import dataclass

import aiohttp
import discord
import psycopg
from discord.ext import commands, tasks

from utils import config, db

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"
MODEL_WORDS = ("opus", "sonnet", "haiku", "fable")
MIN_WORDS = 3
# answered by on_interaction rather than a registered view, so it outlives restarts
CANCEL_PREFIX = "agent-job-cancel:"
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
    "Start with the request, like `@Miku add a /coinflip command that costs 10 points`. "
    "Put `medium effort` or `effort:medium` first to change the effort."
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


def mention_prefix(content: str, bot_id: int, role_id: int | None) -> str | None:
    """What follows a mention of Miku at the very start, or None. Only the start
    counts, unlike user.mentioned_in, which also fires on reply pings and passing
    mentions."""
    forms = [rf"<@!?{bot_id}>"]
    if role_id:
        forms.append(rf"<@&{role_id}>")
    match = re.match(rf"\s*(?:{'|'.join(forms)})", content)
    return content[match.end() :] if match else None


def _word(token: str) -> str:
    return token.lower().strip(",;:.")


def parse_request(
    content: str, bot_id: int, role_id: int | None
) -> AgentRequest | None:
    """The request if the message starts by mentioning Miku, else None."""
    rest = mention_prefix(content, bot_id, role_id)
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
    prompt = " ".join(tokens[i:]).strip()
    if len(prompt.split()) < MIN_WORDS:
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


def enabled() -> bool:
    return bool((config.config.get("agent") or {}).get("enabled"))


def owner_id() -> int | None:
    value = (config.secrets.get("agent") or {}).get("owner_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
    """@Miku <request> makes a PR."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.worker = WorkerClient()
        self.lock = asyncio.Lock()
        # what each status message last said, so it's only edited when that changes
        self.last_status: dict[int, str] = {}
        self.poll.start()

    def cog_unload(self) -> None:
        self.poll.cancel()

    # --- starting a job ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.bot.user is None:
            return
        guild = message.guild
        role = guild.self_role if guild is not None else None
        # first, because with no channel limit this runs on every message in the server
        request = parse_request(
            message.content, self.bot.user.id, role.id if role else None
        )
        if request is None or guild is None or not enabled():
            return
        owner = owner_id()
        if owner is None or message.author.id != owner:
            return
        if request.error:
            await message.reply(request.error, allowed_mentions=QUIET)
            return
        await self.start_job(message, request)

    async def start_job(
        self, message: discord.Message, request: AgentRequest
    ) -> int | None:
        # held until the job has its thread and status message, so the loop can't
        # launch it with nowhere to report
        async with self.lock:
            row = await db.fetch_one(
                """
                INSERT INTO agent_jobs (requester_id, channel_id, prompt, effort, status)
                VALUES (%s, %s, %s, %s, 'preparing') RETURNING id;
                """,
                (message.author.id, message.channel.id, request.prompt, request.effort),
            )
            assert row is not None
            job_id = row[0]
            try:
                target, status = await self.post_status(message, job_id, request)
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
        channel = self.bot.get_channel(job.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(job.channel_id)
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
        if not custom_id.startswith(CANCEL_PREFIX):
            return
        try:
            job_id = int(custom_id.removeprefix(CANCEL_PREFIX))
        except ValueError:
            return
        if interaction.user is None or interaction.user.id != owner_id():
            await interaction.response.send_message(
                "Only the person who asked can cancel it.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        result = await self.cancel(job_id)
        await interaction.followup.send(result, ephemeral=True)

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
