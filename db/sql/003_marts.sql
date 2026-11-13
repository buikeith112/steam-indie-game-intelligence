-- =====================================================================
-- mart: star schema for Power BI.
--   Materialized: dim_game, fact_game_metrics, tag_summary, price_bucket_summary
--   (refresh with `steam-intel refresh-marts` after each pipeline run)
--   Plain views: everything else (cheap or time-series)
-- Metric definitions: docs/methodology.md
-- =====================================================================

-- ---------------------------------------------------------------------
-- Static dimensions
-- ---------------------------------------------------------------------
CREATE VIEW mart.dim_price_bucket AS
SELECT * FROM (VALUES
    (0, 'Free',          0,     0),
    (1, 'Under $5',      1,   499),
    (2, '$5 - $9.99',  500,   999),
    (3, '$10 - $14.99', 1000, 1499),
    (4, '$15 - $19.99', 1500, 1999),
    (5, '$20 - $29.99', 2000, 2999),
    (6, '$30+',        3000, 2147483647)
) AS t(price_bucket_id, price_bucket, min_cents, max_cents);

CREATE VIEW mart.dim_success_tier AS
SELECT * FROM (VALUES
    (0, 'Under 50 reviews',   0,    49),
    (1, '50 - 499',          50,   499),
    (2, '500 - 4,999',      500,  4999),
    (3, '5,000+',          5000, 2147483647)
) AS t(success_tier_id, success_tier, min_reviews, max_reviews);

CREATE VIEW mart.dim_date AS
SELECT d::date                                  AS date,
       extract(year FROM d)::int                AS year,
       extract(quarter FROM d)::int             AS quarter,
       extract(month FROM d)::int               AS month,
       to_char(d, 'Mon')                        AS month_name,
       extract(isodow FROM d)::int              AS iso_dow,
       to_char(d, 'Dy')                         AS dow_name,
       date_trunc('week', d)::date              AS week_start,
       date_trunc('month', d)::date             AS month_start
FROM generate_series(DATE '2005-01-01', DATE '2030-12-31', INTERVAL '1 day') AS d;

-- ---------------------------------------------------------------------
-- Helper: Wilson score lower bound (95%) for a positive ratio.
-- ---------------------------------------------------------------------
CREATE FUNCTION mart.wilson_lower(pos BIGINT, n BIGINT) RETURNS DOUBLE PRECISION
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN n IS NULL OR n = 0 THEN NULL ELSE
        ((pos::float8 / n) + 1.96^2 / (2 * n)
         - 1.96 * sqrt(((pos::float8 / n) * (1 - pos::float8 / n) + 1.96^2 / (4 * n)) / n))
        / (1 + 1.96^2 / n)
    END
$$;

-- SteamDB-style rating: pulls low-volume games toward 50%.
CREATE FUNCTION mart.steamdb_rating(pos BIGINT, n BIGINT) RETURNS DOUBLE PRECISION
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN n IS NULL OR n = 0 THEN NULL ELSE
        (pos::float8 / n) - ((pos::float8 / n) - 0.5) * power(2, -log(10, (n + 1)::numeric)::float8)
    END
$$;

-- ---------------------------------------------------------------------
-- dim_game (materialized)
-- ---------------------------------------------------------------------
CREATE MATERIALIZED VIEW mart.dim_game AS
WITH primary_tag AS (
    SELECT DISTINCT ON (gt.app_id) gt.app_id, t.tag_id, t.name
    FROM core.game_tags gt JOIN core.tags t USING (tag_id)
    WHERE NOT t.is_meta
    ORDER BY gt.app_id, gt.rank
), devs AS (
    SELECT gc.app_id, string_agg(c.display_name, ', ' ORDER BY c.display_name) AS developers
    FROM core.game_companies gc JOIN core.companies c USING (company_id)
    WHERE gc.role = 'developer' GROUP BY gc.app_id
), pubs AS (
    SELECT gc.app_id, string_agg(c.display_name, ', ' ORDER BY c.display_name) AS publishers
    FROM core.game_companies gc JOIN core.companies c USING (company_id)
    WHERE gc.role = 'publisher' GROUP BY gc.app_id
)
SELECT
    g.app_id,
    g.name,
    'https://store.steampowered.com/app/' || g.app_id      AS store_url,
    g.header_image,
    g.launch_date,
    g.release_date,
    extract(year FROM g.launch_date)::int                   AS launch_year,
    extract(quarter FROM g.launch_date)::int                AS launch_quarter,
    extract(month FROM g.launch_date)::int                  AS launch_month,
    extract(isodow FROM g.launch_date)::int                 AS launch_iso_dow,
    (current_date - g.launch_date)                          AS age_days,
    COALESCE(g.was_early_access, FALSE)                     AS was_early_access,
    COALESCE(g.is_early_access, FALSE)                      AS is_early_access,
    g.is_indie_tag,
    g.is_indie_genre,
    g.is_self_published,
    COALESCE(g.is_free, FALSE)                              AS is_free,
    pt.tag_id                                               AS primary_tag_id,
    COALESCE(pt.name, '(untagged)')                         AS primary_tag,
    d.developers,
    p.publishers,
    g.platform_mac,
    g.platform_linux,
    g.controller_support,
    CASE g.steam_deck_category WHEN 3 THEN 'Verified' WHEN 2 THEN 'Playable'
                               WHEN 1 THEN 'Unsupported' ELSE 'Unknown' END AS steam_deck_status,
    g.num_languages,
    CASE WHEN g.num_languages IS NULL THEN NULL
         WHEN g.num_languages <= 1 THEN '1'
         WHEN g.num_languages <= 4 THEN '2-4'
         WHEN g.num_languages <= 9 THEN '5-9'
         ELSE '10+' END                                     AS language_band,
    EXISTS (SELECT 1 FROM core.game_languages l
            WHERE l.app_id = g.app_id AND l.language = 'Simplified Chinese') AS supports_schinese,
    g.num_achievements,
    g.has_demo,
    g.dlc_count,
    g.metacritic_score,
    g.launch_price_cents,
    COALESCE((SELECT b.price_bucket_id FROM mart.dim_price_bucket b
              WHERE CASE WHEN g.is_free THEN 0 ELSE g.launch_price_cents END BETWEEN b.min_cents AND b.max_cents),
             -1)                                            AS price_bucket_id,
    -- Delisted games stay in scope: dropping them would remove failures from every rate (survivorship bias).
    (g.launch_date >= DATE '2018-01-01'
     AND g.launch_date <= current_date
     AND COALESCE(g.store_review_count, 0) >= 10)           AS in_analytical_scope,
    g.delisted_at IS NOT NULL                               AS is_delisted
FROM core.games g
LEFT JOIN primary_tag pt ON pt.app_id = g.app_id
LEFT JOIN devs d ON d.app_id = g.app_id
LEFT JOIN pubs p ON p.app_id = g.app_id
WHERE g.is_indie_tag OR g.is_indie_genre;

CREATE UNIQUE INDEX ux_dim_game ON mart.dim_game (app_id);

-- ---------------------------------------------------------------------
-- fact_game_metrics (materialized): one row per game, all derived metrics
-- ---------------------------------------------------------------------
CREATE MATERIALIZED VIEW mart.fact_game_metrics AS
WITH latest_reviews AS (
    SELECT DISTINCT ON (app_id) app_id, total_positive, total_negative, total_reviews, review_score_desc
    FROM core.review_snapshots ORDER BY app_id, snapshot_date DESC
), w AS (
    SELECT app_id,
        max(positive) FILTER (WHERE window_code = 'd0_30'    AND is_complete) AS pos_d0_30,
        max(negative) FILTER (WHERE window_code = 'd0_30'    AND is_complete) AS neg_d0_30,
        max(positive + negative) FILTER (WHERE window_code = 'd31_365'  AND is_complete) AS reviews_d31_365,
        max(positive + negative) FILTER (WHERE window_code = 'd366_730' AND is_complete) AS reviews_d366_730,
        (max(positive) FILTER (WHERE window_code = 'd31_365' AND is_complete))::float8
          / NULLIF(max(positive + negative) FILTER (WHERE window_code = 'd31_365' AND is_complete), 0)
                                                                                AS positive_ratio_d31_365
    FROM core.review_windows GROUP BY app_id
), eng AS (
    SELECT DISTINCT ON (app_id) *
    FROM core.engagement_snapshots ORDER BY app_id, snapshot_date DESC
), ccu AS (
    SELECT app_id, max(player_count) AS peak_ccu_observed, avg(player_count) AS avg_ccu_observed
    FROM core.player_count_snapshots GROUP BY app_id
), price AS (
    SELECT app_id,
        max(final_cents) FILTER (WHERE valid_to IS NULL)  AS current_price_cents,
        max(discount_pct)                                 AS max_discount_seen,
        min(valid_from) FILTER (WHERE discount_pct > 0)   AS first_discount_seen_at,
        min(valid_from)                                   AS first_price_seen_at
    FROM core.price_history GROUP BY app_id
), base AS (
    SELECT
        g.app_id,
        COALESCE(r.total_reviews, g.store_review_count)                         AS total_reviews,
        COALESCE(r.total_positive,
                 round(g.store_review_count * g.store_percent_positive / 100.0)::int) AS total_positive,
        r.review_score_desc,
        w.pos_d0_30, w.neg_d0_30, w.reviews_d31_365, w.reviews_d366_730, w.positive_ratio_d31_365,
        e.owners_low, e.owners_high, e.median_forever_min, e.avg_forever_min, e.avg_2weeks_min,
        e.ccu_peak_yesterday,
        c.peak_ccu_observed, c.avg_ccu_observed,
        p.current_price_cents, p.max_discount_seen, p.first_discount_seen_at, p.first_price_seen_at,
        g.launch_price_cents, g.is_free, g.launch_date
    FROM core.games g
    LEFT JOIN latest_reviews r ON r.app_id = g.app_id
    LEFT JOIN w ON w.app_id = g.app_id
    LEFT JOIN eng e ON e.app_id = g.app_id
    LEFT JOIN ccu c ON c.app_id = g.app_id
    LEFT JOIN price p ON p.app_id = g.app_id
    WHERE g.is_indie_tag OR g.is_indie_genre
)
SELECT
    b.app_id,
    -- popularity
    b.total_reviews,
    ln(1 + COALESCE(b.total_reviews, 0))                                        AS log_reviews,
    b.total_reviews * 30                                                        AS boxleiter_sales_low,
    b.total_reviews * 60                                                        AS boxleiter_sales_high,
    (b.owners_low + b.owners_high) / 2.0                                        AS est_owners_mid,
    b.owners_low, b.owners_high,
    -- sentiment
    b.total_positive,
    b.total_positive::float8 / NULLIF(b.total_reviews, 0)                       AS positive_ratio,
    mart.wilson_lower(b.total_positive, b.total_reviews)                        AS wilson_lower_bound,
    mart.steamdb_rating(b.total_positive, b.total_reviews)                      AS steamdb_rating,
    b.review_score_desc,
    (b.total_reviews >= 50 AND b.total_positive::float8 / NULLIF(b.total_reviews, 0) >= 0.80) AS is_very_positive_plus,
    -- Q1: early sentiment vs later popularity
    b.pos_d0_30 + b.neg_d0_30                                                   AS early_reviews_30d,
    b.pos_d0_30::float8 / NULLIF(b.pos_d0_30 + b.neg_d0_30, 0)                  AS early_positive_ratio_30d,
    CASE WHEN b.pos_d0_30 + b.neg_d0_30 < 10 THEN NULL
         WHEN b.pos_d0_30::float8 / (b.pos_d0_30 + b.neg_d0_30) < 0.70 THEN '<70%'
         WHEN b.pos_d0_30::float8 / (b.pos_d0_30 + b.neg_d0_30) < 0.80 THEN '70-79%'
         WHEN b.pos_d0_30::float8 / (b.pos_d0_30 + b.neg_d0_30) < 0.90 THEN '80-89%'
         ELSE '90%+' END                                                        AS early_sentiment_band,
    CASE WHEN b.pos_d0_30 + b.neg_d0_30 IS NULL THEN NULL
         WHEN b.pos_d0_30 + b.neg_d0_30 < 10  THEN '<10'
         WHEN b.pos_d0_30 + b.neg_d0_30 < 50  THEN '10-49'
         WHEN b.pos_d0_30 + b.neg_d0_30 < 250 THEN '50-249'
         ELSE '250+' END                                                        AS early_volume_band,
    b.reviews_d31_365,
    b.reviews_d366_730,
    b.positive_ratio_d31_365,
    b.reviews_d31_365::float8 / NULLIF(b.pos_d0_30 + b.neg_d0_30, 0)            AS later_to_early_review_ratio,
    b.reviews_d366_730::float8 / NULLIF(b.reviews_d31_365, 0)                   AS review_growth_y2_vs_y1,
    -- Q4: engagement
    b.median_forever_min / 60.0                                                 AS median_playtime_h,
    b.avg_forever_min / 60.0                                                    AS avg_playtime_h,
    b.median_forever_min::float8 / NULLIF(b.avg_forever_min, 0)                 AS median_to_avg_ratio,
    b.avg_2weeks_min::float8 / NULLIF(b.avg_forever_min, 0)                     AS active_ratio_2w,
    b.ccu_peak_yesterday,
    b.peak_ccu_observed,
    b.avg_ccu_observed,
    1000.0 * b.peak_ccu_observed / NULLIF((b.owners_low + b.owners_high) / 2.0, 0) AS ccu_per_1k_owners,
    -- Q3: price
    b.launch_price_cents / 100.0                                                AS launch_price_usd,
    b.current_price_cents / 100.0                                               AS current_price_usd,
    b.max_discount_seen,
    CASE WHEN b.first_discount_seen_at IS NOT NULL AND b.first_price_seen_at::date <= b.launch_date + 7
         THEN b.first_discount_seen_at::date - b.launch_date END               AS days_to_first_discount,
    CASE WHEN NOT COALESCE(b.is_free, FALSE) AND b.launch_price_cents > 0
         THEN (b.median_forever_min / 60.0) / (b.launch_price_cents / 100.0) END AS hours_per_dollar,
    -- tiers
    CASE WHEN b.total_reviews >= 5000 THEN 3 WHEN b.total_reviews >= 500 THEN 2
         WHEN b.total_reviews >= 50 THEN 1 ELSE 0 END                           AS success_tier_id,
    (b.total_reviews >= 1000)                                                   AS is_hit_1k
FROM base b;

CREATE UNIQUE INDEX ux_fact_game_metrics ON mart.fact_game_metrics (app_id);

-- ---------------------------------------------------------------------
-- Tag / genre bridges and dimensions
-- ---------------------------------------------------------------------
-- Top-5 genre-like (non-meta) tags per game. Many-to-many: a game counts once per tag.
CREATE VIEW mart.bridge_game_tag AS
SELECT app_id, tag_id, tag_rank, weight FROM (
    SELECT gt.app_id, gt.tag_id, gt.weight,
           row_number() OVER (PARTITION BY gt.app_id ORDER BY gt.rank) AS tag_rank
    FROM core.game_tags gt JOIN core.tags t USING (tag_id)
    WHERE NOT t.is_meta
) x WHERE tag_rank <= 5;

CREATE VIEW mart.dim_tag AS
SELECT t.tag_id, t.name AS tag, t.is_meta FROM core.tags t
WHERE EXISTS (SELECT 1 FROM core.game_tags gt WHERE gt.tag_id = t.tag_id);

CREATE VIEW mart.dim_genre AS SELECT genre_id, name AS genre FROM core.genres;
CREATE VIEW mart.bridge_game_genre AS SELECT app_id, genre_id FROM core.game_genres;
CREATE VIEW mart.dim_category AS SELECT category_id, name AS category FROM core.categories;
CREATE VIEW mart.bridge_game_category AS SELECT app_id, category_id FROM core.game_categories;

-- ---------------------------------------------------------------------
-- Pre-aggregated summaries (avoid many-to-many double counting in DAX)
-- ---------------------------------------------------------------------
CREATE MATERIALIZED VIEW mart.tag_summary AS
SELECT
    bt.tag_id,
    t.name                                                              AS tag,
    count(*)                                                            AS n_games,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.total_reviews)        AS median_reviews,
    avg(f.is_very_positive_plus::int) FILTER (WHERE f.total_reviews >= 50) AS share_very_positive_plus,
    count(*) FILTER (WHERE f.total_reviews >= 50)                       AS n_games_50plus_reviews,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.wilson_lower_bound)   AS median_wilson,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.positive_ratio)       AS median_positive_ratio,
    avg(f.is_hit_1k::int)                                               AS hit_rate_1k,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.median_playtime_h)    AS median_of_median_playtime_h,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.est_owners_mid)       AS median_owners,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.active_ratio_2w)      AS median_active_ratio_2w,
    count(f.median_playtime_h)                                          AS n_games_with_playtime,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.launch_price_usd)
        FILTER (WHERE NOT g.is_free)                                    AS median_launch_price_usd
FROM mart.bridge_game_tag bt
JOIN core.tags t ON t.tag_id = bt.tag_id
JOIN mart.dim_game g ON g.app_id = bt.app_id AND g.in_analytical_scope
JOIN mart.fact_game_metrics f ON f.app_id = bt.app_id
GROUP BY bt.tag_id, t.name;

-- Supply vs demand per tag per launch year (opportunity explorer).
CREATE VIEW mart.tag_year_summary AS
SELECT
    bt.tag_id, g.launch_year,
    count(*)                                                            AS n_releases,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.total_reviews)        AS median_reviews,
    avg(f.is_hit_1k::int)                                               AS hit_rate_1k,
    avg(f.is_very_positive_plus::int) FILTER (WHERE f.total_reviews >= 50) AS share_very_positive_plus
FROM mart.bridge_game_tag bt
JOIN mart.dim_game g ON g.app_id = bt.app_id
JOIN mart.fact_game_metrics f ON f.app_id = bt.app_id
WHERE g.launch_year >= 2018
GROUP BY bt.tag_id, g.launch_year;

CREATE MATERIALIZED VIEW mart.price_bucket_summary AS
SELECT
    g.price_bucket_id,
    count(*)                                                            AS n_games,
    avg(f.is_hit_1k::int)                                               AS hit_rate_1k,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.total_reviews)        AS median_reviews,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.positive_ratio)       AS median_positive_ratio,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.est_owners_mid)       AS median_owners,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY f.median_playtime_h)    AS median_playtime_h
FROM mart.dim_game g
JOIN mart.fact_game_metrics f USING (app_id)
WHERE g.in_analytical_scope
GROUP BY g.price_bucket_id;

-- ---------------------------------------------------------------------
-- Time series for drill-through pages
-- ---------------------------------------------------------------------
CREATE VIEW mart.fact_price_history AS
SELECT app_id, valid_from::date AS valid_from, COALESCE(valid_to, now())::date AS valid_to,
       valid_to IS NULL AS is_current, initial_cents / 100.0 AS list_price_usd,
       final_cents / 100.0 AS price_usd, discount_pct, source
FROM core.price_history;

CREATE VIEW mart.fact_catalog_daily AS
SELECT app_id, snapshot_date AS date, review_count, percent_positive,
       final_price_cents / 100.0 AS price_usd, discount_pct,
       review_count - lag(review_count) OVER (PARTITION BY app_id ORDER BY snapshot_date) AS new_reviews
FROM core.catalog_snapshots;

CREATE VIEW mart.fact_review_histogram AS
SELECT app_id, granularity, period_start AS date, up, down, up + down AS total,
       up::float8 / NULLIF(up + down, 0) AS positive_ratio
FROM core.review_histogram;

CREATE VIEW mart.fact_ccu_daily AS
SELECT app_id, captured_at::date AS date, max(player_count) AS peak_players
FROM core.player_count_snapshots GROUP BY app_id, captured_at::date;

-- ---------------------------------------------------------------------
-- Analysis results written by steam_intel.analysis (correlations, regressions)
-- ---------------------------------------------------------------------
CREATE TABLE mart.analysis_results (
    analysis        TEXT        NOT NULL,      -- e.g. 'q1_spearman_early_vs_later'
    segment         TEXT        NOT NULL DEFAULT 'all',
    metric          TEXT        NOT NULL,      -- e.g. 'rho', 'p_value', 'n', 'coef_early_ratio'
    value           DOUBLE PRECISION,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (analysis, segment, metric)
);

-- ---------------------------------------------------------------------
-- Data quality / freshness for the methodology page
-- ---------------------------------------------------------------------
CREATE VIEW mart.data_freshness AS
SELECT 'catalog' AS source, max(snapshot_date)::timestamptz AS last_data FROM core.catalog_snapshots
UNION ALL SELECT 'appdetails', max(details_fetched_at) FROM core.games
UNION ALL SELECT 'reviews', max(snapshot_date)::timestamptz FROM core.review_snapshots
UNION ALL SELECT 'review_windows', max(computed_at) FROM core.review_windows
UNION ALL SELECT 'steamspy', max(snapshot_date)::timestamptz FROM core.engagement_snapshots
UNION ALL SELECT 'players', max(captured_at) FROM core.player_count_snapshots;

CREATE VIEW mart.data_coverage AS
SELECT
    count(*)                                                          AS games_in_scope,
    avg((g2.details_fetched_at IS NOT NULL)::int)                     AS pct_with_details,
    avg((f.early_reviews_30d IS NOT NULL)::int)                       AS pct_with_early_window,
    avg((f.median_playtime_h IS NOT NULL)::int)                       AS pct_with_playtime,
    avg((f.peak_ccu_observed IS NOT NULL)::int)                       AS pct_with_ccu,
    avg((g.launch_price_cents IS NOT NULL OR g.is_free)::int)         AS pct_with_price
FROM mart.dim_game g
JOIN core.games g2 USING (app_id)
JOIN mart.fact_game_metrics f USING (app_id)
WHERE g.in_analytical_scope;

CREATE VIEW mart.dq_latest AS
SELECT DISTINCT ON (check_name) check_name, passed, observed_value, threshold, details, checked_at
FROM ops.data_quality_results ORDER BY check_name, checked_at DESC;
