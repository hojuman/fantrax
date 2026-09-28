-- Local cache + normalized data. Safe to delete var/hockey.db: `hockey sync` rebuilds it.

CREATE TABLE IF NOT EXISTS http_cache (
    url         TEXT PRIMARY KEY,
    fetched_at  REAL NOT NULL,
    ttl         REAL,              -- seconds; NULL = never expires (immutable data, e.g. past seasons)
    status      INTEGER NOT NULL,
    body        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,     -- JSON
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fantasy_team (
    team_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    short_name  TEXT,
    source      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fantrax_player (
    fantrax_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,     -- display form "First Last"
    nhl_team    TEXT,              -- normalized to NHL abbreviations
    positions   TEXT,              -- e.g. "C,LW"
    pos_group   TEXT,              -- F / D / G
    pool_status TEXT,              -- FA / W / team id, when Fantrax exposes it
    extra       TEXT               -- JSON: any other fields (e.g. external ids)
);

CREATE TABLE IF NOT EXISTS roster_entry (
    team_id     TEXT NOT NULL,
    fantrax_id  TEXT NOT NULL,
    slot        TEXT,              -- position slot, when known
    status      TEXT,              -- ACTIVE / RESERVE / IR / MINORS ...
    source      TEXT NOT NULL,     -- fxea / csv
    as_of       REAL NOT NULL,
    PRIMARY KEY (team_id, fantrax_id)
);

CREATE TABLE IF NOT EXISTS nhl_player (
    nhl_id      INTEGER PRIMARY KEY,
    full_name   TEXT NOT NULL,
    pos_code    TEXT,              -- C / L / R / D / G
    pos_group   TEXT,              -- F / D / G
    team        TEXT,
    birth_date  TEXT
);

CREATE TABLE IF NOT EXISTS nhl_stat_season (
    nhl_id      INTEGER NOT NULL,
    season      INTEGER NOT NULL,  -- e.g. 20252026
    pos_group   TEXT NOT NULL,
    team        TEXT,
    gp          INTEGER NOT NULL,
    stats       TEXT NOT NULL,     -- JSON of canonical stat keys (see scoring/rules.py)
    PRIMARY KEY (nhl_id, season)
);

CREATE TABLE IF NOT EXISTS player_map (
    fantrax_id  TEXT PRIMARY KEY,
    nhl_id      INTEGER NOT NULL,
    method      TEXT NOT NULL,     -- override / external_id / exact / exact_anypos / alias / alias_team / fuzzy / sticky
    confidence  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS id_unmatched (
    fantrax_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    nhl_team    TEXT,
    pos_group   TEXT,
    reason      TEXT NOT NULL,
    candidates  TEXT,              -- JSON list of candidate nhl ids/names
    relevant    INTEGER NOT NULL,  -- 1 if rostered in the league or in the FA pool
    updated_at  REAL NOT NULL
);
