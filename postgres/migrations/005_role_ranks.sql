-- Per-role ranks/elo: some games (Overwatch) rank a player separately per role
-- rather than one rank per game. Opt-in via data/games/<game>.yaml's
-- per_role_ranks flag; League/Valorant keep using profile_stats/profile_elo.

CREATE TABLE IF NOT EXISTS profile_role_ranks (
    discordid BIGINT NOT NULL,
    game TEXT NOT NULL,
    role TEXT NOT NULL,
    rank_value INT,
    rank_label TEXT,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (discordid, game, role)
);

CREATE TABLE IF NOT EXISTS profile_role_elo (
    discordid BIGINT NOT NULL,
    game TEXT NOT NULL,
    role TEXT NOT NULL,
    elo NUMERIC NOT NULL,
    games_played INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (discordid, game, role)
);

-- Old single-rank Overwatch data is superseded by the tables above. The club
-- is small enough that resetting and having people re-set their rank beats
-- writing a real backfill from a single rank_value of unknown provenance.
UPDATE profile_stats SET rank_value = NULL, rank_label = NULL WHERE game = 'overwatch';
DELETE FROM profile_elo WHERE game = 'overwatch';
