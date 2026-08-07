CREATE TABLE IF NOT EXISTS game_accounts (
    discordid BIGINT NOT NULL,
    game TEXT NOT NULL,
    external_id TEXT NOT NULL ,         -- Name#Tag, Name#1234, SteamID/Vanity

    display_name TEXT,                  -- canonical form resolved from the API at link time
    region TEXT,                        -- routing value some APIs need alongside the id; NULL where unused
    provider_account_id TEXT,           -- primary resolved id refresh is keyed on (League/Valorant: puuid;
                                         -- Deadlock: resolved SteamID64; Overwatch: NULL, BattleTag is the key)
    provider_secondary_id TEXT,         -- second chained id only League needs (encrypted summonerId)
    
    linked_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (discordid, game)
);