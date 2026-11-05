-- @Miku conversations. A chat is one Discord channel (or thread, or forum post);
-- its id there is also the chat's id on the worker, so the two can never drift
-- apart. Each message to Miku is a turn: the worker answers it, and the bot posts
-- the reply. A reply can offer a PR, which a button turns into an agent_jobs row.

CREATE TABLE IF NOT EXISTS agent_chats (
    channel_id   BIGINT PRIMARY KEY,
    requester_id BIGINT NOT NULL,          -- who first talked to her there
    created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_chat_turns (
    id            SERIAL PRIMARY KEY,
    channel_id    BIGINT NOT NULL REFERENCES agent_chats (channel_id) ON DELETE CASCADE,
    message_id    BIGINT NOT NULL UNIQUE,  -- what they said; the worker dedupes on it too
    requester_id  BIGINT NOT NULL,         -- who said it: only they (or the owner) can press its PR button
    n             INTEGER,                 -- the worker's turn number, once it took the message
    effort        TEXT NOT NULL,           -- for the PR, if this turn offers one
    status        TEXT NOT NULL,           -- sending, thinking, done, failed
    pr_request    TEXT,                    -- the request the "Make the PR" button sends
    pr_message_id BIGINT,                  -- the message holding that button
    job_id        INTEGER REFERENCES agent_jobs (id) ON DELETE SET NULL,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS agent_chat_turns_open ON agent_chat_turns (status) WHERE status IN ('sending', 'thinking');
