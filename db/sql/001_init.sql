-- =====================================================================
-- Steam Indie Game Intelligence: initial schema
--   raw  : untouched API payloads (bronze; parsing always starts here)
--   ops  : pipeline bookkeeping (runs, resumable queue, DQ, state)
--   core : normalized entities and time series (silver)
--   mart : views for Power BI (gold; created in later migrations)
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;

-- ---------------------------------------------------------------------
-- ops
-- ---------------------------------------------------------------------
CREATE TABLE ops.ingestion_runs (
    run_id          BIGSERIAL PRIMARY KEY,
    job             TEXT        NOT NULL,              -- e.g. 'catalog', 'details', 'daily'
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT        NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'succeeded', 'failed', 'partial')),
    items_ok        INTEGER     NOT NULL DEFAULT 0,
    items_failed    INTEGER     NOT NULL DEFAULT 0,
    notes           JSONB
);

-- One row per (source, app). The crawler always resumes from here.
CREATE TABLE ops.ingestion_queue (
    source          TEXT        NOT NULL,              -- 'appdetails', 'review_windows', ...
    app_id          INTEGER     NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_progress', 'done', 'failed', 'skip')),
    priority        SMALLINT    NOT NULL DEFAULT 100,  -- lower = sooner
    attempts        SMALLINT    NOT NULL DEFAULT 0,
    last_error      TEXT,
    next_retry_at   TIMESTAMPTZ,
    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, app_id)
);
CREATE INDEX ix_queue_ready ON ops.ingestion_queue (source, status, priority, next_retry_at);

CREATE TABLE ops.data_quality_results (
    check_id        BIGSERIAL PRIMARY KEY,
    run_id          BIGINT REFERENCES ops.ingestion_runs (run_id),
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    check_name      TEXT        NOT NULL,
    passed          BOOLEAN     NOT NULL,
    observed_value  NUMERIC,
    threshold       NUMERIC,
    details         TEXT
);

-- Small key/value store for checkpoints (e.g. last catalog sync time).
CREATE TABLE ops.pipeline_state (
    key             TEXT PRIMARY KEY,
    value           JSONB       NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- raw
-- ---------------------------------------------------------------------
CREATE TABLE raw.api_responses (
    response_id     BIGSERIAL PRIMARY KEY,
    source          TEXT        NOT NULL,              -- 'store_query', 'getitems', 'appdetails', ...
    app_id          INTEGER,                            -- NULL for batch / catalog responses
    request_key     TEXT,                               -- window code, page number, batch id...
    request_params  JSONB,
    http_status     SMALLINT    NOT NULL,
    payload         JSONB,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id          BIGINT REFERENCES ops.ingestion_runs (run_id),
    processed_at    TIMESTAMPTZ                         -- set once transformed into core
);
CREATE INDEX ix_raw_source_app_time ON raw.api_responses (source, app_id, fetched_at DESC);
CREATE INDEX ix_raw_unprocessed     ON raw.api_responses (source, response_id) WHERE processed_at IS NULL;

-- ---------------------------------------------------------------------
-- core: dimensions
-- ---------------------------------------------------------------------
CREATE TABLE core.games (
    app_id                  INTEGER PRIMARY KEY,
    name                    TEXT        NOT NULL,
    app_type                TEXT,                       -- 'game' (appdetails.type)
    -- release / lifecycle
    release_date            DATE,                       -- current Steam release date (post-EA if applicable)
    release_date_raw        TEXT,                       -- appdetails string as published
    date_precision          TEXT CHECK (date_precision IN ('day', 'month', 'quarter', 'year', 'unknown')),
    launch_date             DATE,                       -- first date purchasable (EA start or release)
    early_access_exit_date  DATE,
    was_early_access        BOOLEAN,
    is_early_access         BOOLEAN,                    -- currently in Early Access
    coming_soon             BOOLEAN,
    -- classification
    is_indie_tag            BOOLEAN     NOT NULL DEFAULT FALSE,
    is_indie_genre          BOOLEAN,                    -- Steam genre id 23 (NULL until appdetails fetched)
    is_self_published       BOOLEAN,
    -- commercial
    is_free                 BOOLEAN,
    launch_price_cents      INTEGER,
    launch_price_source     TEXT,                       -- 'first_observed', 'steamspy_initial', 'itad'
    required_age            SMALLINT,
    -- features
    platform_windows        BOOLEAN,
    platform_mac            BOOLEAN,
    platform_linux          BOOLEAN,
    controller_support      TEXT,                       -- 'full', 'partial', NULL
    steam_deck_category     SMALLINT,                   -- 0 unknown, 1 unsupported, 2 playable, 3 verified
    num_languages           SMALLINT,
    num_full_audio_languages SMALLINT,
    num_achievements        INTEGER,
    has_demo                BOOLEAN,
    dlc_count               INTEGER,
    metacritic_score        SMALLINT,
    -- store review summary as last seen in catalog (Steam purchases)
    store_review_count      INTEGER,
    store_percent_positive  SMALLINT,
    store_review_score      SMALLINT,
    -- descriptive
    short_description       TEXT,
    header_image            TEXT,
    website                 TEXT,
    -- bookkeeping
    first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_in_catalog_at TIMESTAMPTZ,
    details_fetched_at      TIMESTAMPTZ,
    delisted_at             TIMESTAMPTZ,                -- soft delete: never hard-delete games
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_games_launch ON core.games (launch_date);

-- Developers and publishers share one table; role lives on the bridge.
CREATE TABLE core.companies (
    company_id      SERIAL PRIMARY KEY,
    name_normalized TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL
);
CREATE TABLE core.game_companies (
    app_id      INTEGER  NOT NULL REFERENCES core.games (app_id),
    company_id  INTEGER  NOT NULL REFERENCES core.companies (company_id),
    role        TEXT     NOT NULL CHECK (role IN ('developer', 'publisher')),
    PRIMARY KEY (app_id, company_id, role)
);

CREATE TABLE core.genres (
    genre_id    INTEGER PRIMARY KEY,
    name        TEXT NOT NULL
);
CREATE TABLE core.game_genres (
    app_id      INTEGER NOT NULL REFERENCES core.games (app_id),
    genre_id    INTEGER NOT NULL REFERENCES core.genres (genre_id),
    PRIMARY KEY (app_id, genre_id)
);

CREATE TABLE core.tags (
    tag_id      INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    is_meta     BOOLEAN NOT NULL DEFAULT FALSE      -- 'Indie', 'Singleplayer', '2D'... excluded from primary_tag
);
CREATE TABLE core.game_tags (
    app_id      INTEGER  NOT NULL REFERENCES core.games (app_id),
    tag_id      INTEGER  NOT NULL,                   -- no FK: tag dictionary can lag new tags
    weight      INTEGER,
    rank        SMALLINT NOT NULL,                   -- 1 = most voted
    PRIMARY KEY (app_id, tag_id)
);

CREATE TABLE core.categories (
    category_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL
);
CREATE TABLE core.game_categories (
    app_id      INTEGER NOT NULL REFERENCES core.games (app_id),
    category_id INTEGER NOT NULL REFERENCES core.categories (category_id),
    PRIMARY KEY (app_id, category_id)
);

CREATE TABLE core.game_languages (
    app_id      INTEGER NOT NULL REFERENCES core.games (app_id),
    language    TEXT    NOT NULL,
    full_audio  BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (app_id, language)
);

-- ---------------------------------------------------------------------
-- core: facts / time series
-- ---------------------------------------------------------------------
-- SCD type 2: one open row (valid_to IS NULL) per app.
CREATE TABLE core.price_history (
    app_id          INTEGER     NOT NULL REFERENCES core.games (app_id),
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,
    currency        CHAR(3)     NOT NULL DEFAULT 'USD',
    initial_cents   INTEGER     NOT NULL,
    final_cents     INTEGER     NOT NULL,
    discount_pct    SMALLINT    NOT NULL DEFAULT 0,
    source          TEXT        NOT NULL,           -- 'appdetails', 'getitems', 'itad'
    PRIMARY KEY (app_id, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE UNIQUE INDEX ux_price_open ON core.price_history (app_id) WHERE valid_to IS NULL;

CREATE TABLE core.review_snapshots (
    app_id              INTEGER  NOT NULL REFERENCES core.games (app_id),
    snapshot_date       DATE     NOT NULL,
    total_positive      INTEGER  NOT NULL,
    total_negative      INTEGER  NOT NULL,
    total_reviews       INTEGER  NOT NULL,
    review_score        SMALLINT,
    review_score_desc   TEXT,
    PRIMARY KEY (app_id, snapshot_date)
);

-- Q1 core: review counts in fixed windows relative to launch_date.
CREATE TABLE core.review_windows (
    app_id          INTEGER     NOT NULL REFERENCES core.games (app_id),
    window_code     TEXT        NOT NULL,           -- 'd0_30', 'd31_365', 'd366_730'
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    positive        INTEGER     NOT NULL,
    negative        INTEGER     NOT NULL,
    is_complete     BOOLEAN     NOT NULL,           -- window_end already in the past when fetched
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, window_code)
);

CREATE TABLE core.review_histogram (
    app_id          INTEGER  NOT NULL REFERENCES core.games (app_id),
    granularity     TEXT     NOT NULL CHECK (granularity IN ('day', 'week', 'month')),
    period_start    DATE     NOT NULL,
    up              INTEGER  NOT NULL,
    down            INTEGER  NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (app_id, granularity, period_start)
);

-- Sample of individual reviews (most helpful + most recent), not the full corpus.
CREATE TABLE core.reviews (
    recommendation_id       BIGINT PRIMARY KEY,
    app_id                  INTEGER     NOT NULL REFERENCES core.games (app_id),
    author_hash             TEXT        NOT NULL,   -- sha256 of SteamID; raw ID never stored in core
    sample_type             TEXT        NOT NULL,   -- 'helpful', 'recent'
    voted_up                BOOLEAN     NOT NULL,
    language                TEXT,
    created_at              TIMESTAMPTZ NOT NULL,
    updated_at              TIMESTAMPTZ,
    playtime_at_review_min  INTEGER,
    playtime_forever_min    INTEGER,
    playtime_2weeks_min     INTEGER,
    author_num_reviews      INTEGER,
    votes_up                INTEGER,
    votes_funny             INTEGER,
    weighted_vote_score     REAL,
    comment_count           INTEGER,
    steam_purchase          BOOLEAN,
    received_for_free       BOOLEAN,
    written_during_early_access BOOLEAN,
    primarily_steam_deck    BOOLEAN,
    refunded                BOOLEAN,
    review_text             TEXT
);
CREATE INDEX ix_reviews_app ON core.reviews (app_id, created_at);

-- SteamSpy estimates (refreshed on a rotating weekly schedule).
CREATE TABLE core.engagement_snapshots (
    app_id              INTEGER  NOT NULL REFERENCES core.games (app_id),
    snapshot_date       DATE     NOT NULL,
    owners_low          BIGINT,
    owners_high         BIGINT,
    avg_forever_min     INTEGER,
    median_forever_min  INTEGER,
    avg_2weeks_min      INTEGER,
    median_2weeks_min   INTEGER,
    ccu_peak_yesterday  INTEGER,
    initial_price_cents INTEGER,
    PRIMARY KEY (app_id, snapshot_date)
);

CREATE TABLE core.player_count_snapshots (
    app_id          INTEGER     NOT NULL REFERENCES core.games (app_id),
    captured_at     TIMESTAMPTZ NOT NULL,
    player_count    INTEGER     NOT NULL,
    PRIMARY KEY (app_id, captured_at)
);

CREATE TABLE core.news_events (
    gid             TEXT PRIMARY KEY,
    app_id          INTEGER     NOT NULL REFERENCES core.games (app_id),
    published_at    TIMESTAMPTZ NOT NULL,
    feed_label      TEXT,
    title           TEXT,
    is_patch        BOOLEAN
);
CREATE INDEX ix_news_app ON core.news_events (app_id, published_at);
