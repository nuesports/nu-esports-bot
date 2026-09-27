-- Every change to config.yaml: /config edits and undos, reloads of hand edits, and
-- the file as it stood at each startup. after_text is always a file that parsed, so
-- the newest row is what the bot falls back on if config.yaml ever won't load.

CREATE TABLE IF NOT EXISTS config_revisions (
    id          SERIAL PRIMARY KEY,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    action      TEXT NOT NULL,  -- 'edit', 'undo', 'reload' or 'startup'
    path        TEXT,           -- the setting an edit or undo touched
    user_id     BIGINT,
    user_name   TEXT,
    before_text TEXT,
    after_text  TEXT NOT NULL,
    undone_by   INT REFERENCES config_revisions (id)
);
