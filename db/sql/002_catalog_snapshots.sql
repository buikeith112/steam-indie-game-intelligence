-- Daily panel for the whole indie catalog, captured for free during the catalog crawl
-- (GetItems already returns review count, % positive and current price for every app).
-- Gives market-wide review velocity and discount behaviour without extra API calls.
CREATE TABLE core.catalog_snapshots (
    app_id                  INTEGER  NOT NULL REFERENCES core.games (app_id),
    snapshot_date           DATE     NOT NULL,
    review_count            INTEGER,
    percent_positive        SMALLINT,
    final_price_cents       INTEGER,
    discount_pct            SMALLINT,
    PRIMARY KEY (app_id, snapshot_date)
);
CREATE INDEX ix_catalog_snapshots_date ON core.catalog_snapshots (snapshot_date);
