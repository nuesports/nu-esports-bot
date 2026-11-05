"""The cog against a real miku-worker, running its fake claude. Skipped unless
MIKU_WORKER_URL and MIKU_WORKER_TOKEN point at one, so CI never needs it. The
deploy repo's miku-worker/compose.test.yaml runs such a worker; publish its port
and run:

    MIKU_WORKER_URL=http://localhost:18080 MIKU_WORKER_TOKEN=... uv run pytest tests/test_agent_jobs_worker.py
"""

import asyncio
import os
import time

import pytest
import pytest_asyncio
from test_agent_jobs import BOT, OWNER, FakeDiscordMessage, make_cog, press, row, start

from cogs import agent_jobs
from utils import config, db

URL = os.environ.get("MIKU_WORKER_URL")
TOKEN = os.environ.get("MIKU_WORKER_TOKEN")
pytestmark = pytest.mark.skipif(
    not (URL and TOKEN), reason="needs a running miku-worker"
)


@pytest_asyncio.fixture
async def live(migrated_db, monkeypatch):
    monkeypatch.setitem(config.config, "agent", {"enabled": True})
    monkeypatch.setitem(
        config.secrets,
        "agent",
        {"worker_url": URL, "worker_token": TOKEN, "owner_id": OWNER},
    )
    await db.perform_one("DELETE FROM agent_jobs")
    # the worker keys jobs by id, and this database's ids start again at 1
    await db.perform_one(
        "SELECT setval('agent_jobs_id_seq', %s)", (int(time.time()) % 1_000_000_000,)
    )
    yield
    await db.perform_one("DELETE FROM agent_jobs")


async def tick_until(cog, job_id, states, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        await cog.tick()
        status = (await row(job_id))[0]
        if status in states:
            return status
        await asyncio.sleep(1)
    raise AssertionError(f"job {job_id} stuck at {status}")


@pytest.mark.asyncio
async def test_a_request_reaches_done_and_records_the_pr(live):
    cog = make_cog(worker=agent_jobs.WorkerClient())
    message = FakeDiscordMessage(
        f"<@{BOT}> add a coinflip command FAKE:review_bug_fixed"
    )
    job_id = await start(cog, message)
    assert await tick_until(cog, job_id, agent_jobs.FINAL) == "done"
    _, _, rounds, stop, pr_url, *_ = await row(job_id)
    assert (rounds, stop) == (2, "approved")
    assert pr_url.startswith("https://github.com/nuesports/nu-esports-bot/pull/")
    thread = message.threads[0]
    assert any(s.startswith("Plan: Fake plan") for s in thread.sent)
    assert thread.sent[-1].startswith(f"Done: {pr_url}")


@pytest.mark.asyncio
async def test_a_restart_mid_job_loses_nothing(live):
    first = make_cog(worker=agent_jobs.WorkerClient())
    message = FakeDiscordMessage(f"<@{BOT}> add a coinflip command FAKE:review_forever")
    job_id = await start(first, message)
    await tick_until(first, job_id, {"running"})
    second = make_cog(bot=first.bot, worker=agent_jobs.WorkerClient())
    assert await tick_until(second, job_id, agent_jobs.FINAL) == "draft"
    assert (await row(job_id))[3] == "max_rounds"
    assert message.threads[0].sent[-1].startswith("Draft PR:")


@pytest.mark.asyncio
async def test_cancel_after_a_restart(live):
    first = make_cog(worker=agent_jobs.WorkerClient())
    job_id = await start(
        first, FakeDiscordMessage(f"<@{BOT}> add a coinflip command FAKE:hang_code")
    )
    await tick_until(first, job_id, {"running"})
    second = make_cog(bot=first.bot, worker=agent_jobs.WorkerClient())
    await asyncio.sleep(3)
    interaction = press(job_id)
    await second.on_interaction(interaction)
    assert (await row(job_id))[0] == "cancelled"
    _, body = await agent_jobs.WorkerClient().status(job_id)
    assert body["state"] == "cancelled"
