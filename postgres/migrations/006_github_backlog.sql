-- Tracks which Discord message corresponds to which GitHub PR/issue, so the bot
-- can find and unpin it later when the PR merges or the issue closes -- these are
-- two separate webhook deliveries, possibly days apart, with no other shared state.

CREATE TABLE IF NOT EXISTS github_backlog_messages (
    repo       TEXT NOT NULL,
    number     INT NOT NULL,
    kind       TEXT NOT NULL,  -- 'pr' or 'issue'
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, number, kind)
);
