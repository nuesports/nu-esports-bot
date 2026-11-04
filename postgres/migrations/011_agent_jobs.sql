-- @Miku jobs: one row per request, from queued to a PR (or a reason there isn't one).
-- The bot owns the queue and everything people see in Discord, so this row is what
-- lets a restart pick a running job back up. The worker container owns the run.

CREATE TABLE IF NOT EXISTS agent_jobs (
    id                SERIAL PRIMARY KEY,
    requester_id      BIGINT NOT NULL,
    channel_id        BIGINT NOT NULL,   -- the thread, or the channel if it couldn't open one
    status_message_id BIGINT,
    prompt            TEXT NOT NULL,
    effort            TEXT NOT NULL,
    status            TEXT NOT NULL,     -- preparing, queued, running, done, draft, no_change, needs_input, failed, cancelled
    step              TEXT,              -- planning, coding, checks, review N, fixing N, pushing
    review_rounds     INTEGER NOT NULL DEFAULT 0,
    stop_reason       TEXT,              -- approved, max_rounds, no_progress, repeat_finding, disagreement, time, new_bugs_only, checks_red
    plan_summary      TEXT,              -- the one line posted to the thread after planning
    branch            TEXT,
    pr_url            TEXT,
    code_session_id   TEXT,              -- Sonnet's session, for Phase 4 follow-ups
    error             TEXT,
    limit_hit         TEXT,              -- session or weekly, when a usage limit ended it
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS agent_jobs_status ON agent_jobs (status, id);
