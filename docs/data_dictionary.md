# Data Dictionary

Full DDL with column comments: [db/sql/](../db/sql/). Metric definitions: [methodology.md](methodology.md).

## Layers
| Schema | Purpose |
|---|---|
| `raw` | `api_responses`: every API payload as JSONB, plus source, app_id, request parameters, HTTP status and run_id. Parsing reads only from here, so `core` can be rebuilt without calling the APIs again. Review payloads are scrubbed before storage: SteamIDs are hashed and review text is dropped unless `STORE_REVIEW_TEXT=true`. API keys are never stored. |
| `ops` | `ingestion_runs` (job log), `ingestion_queue` (resumable work items per source and app), `data_quality_results`, `pipeline_state` (checkpoints) |
| `core` | Normalized entities and time series (below) |
| `mart` | Star schema for Power BI (below) |

## core
| Table | Grain | Source | Notes |
|---|---|---|---|
| `games` | app | GetItems + appdetails | Lifecycle (launch, Early Access), flags, features, latest store review summary. Soft delete via `delisted_at`. |
| `companies`, `game_companies` | company; app × company × role | GetItems, appdetails | Names normalized ("Devolver Digital, Inc." = "Devolver Digital") |
| `tags`, `game_tags` | tag; app × tag | tagdata, GetItems | Top 20 tags with vote weight and rank. `tags.is_meta` marks non-genre tags such as Indie, 2D, Singleplayer. |
| `genres`, `game_genres` | genre; app × genre | appdetails | Steam's coarse genres (id 23 = Indie) |
| `categories`, `game_categories` | category; app × category | appdetails | Single-player, Co-op, Achievements, controller, etc. |
| `game_languages` | app × language | appdetails | `full_audio` flag |
| `price_history` | app × version | GetItems (daily), appdetails | SCD-2 (`valid_from`, `valid_to`, one open row per app) |
| `catalog_snapshots` | app × day | GetItems (daily) | Review count, % positive and price for the whole indie catalog |
| `review_snapshots` | app × day | GetAppReviews | Exact lifetime positive and negative counts |
| `review_windows` | app × window | GetAppReviews (date range) | `d0_30`, `d31_365`, `d366_730` relative to launch. `is_complete` once the window has closed. |
| `review_histogram` | app × period | appreviewhistogram | Monthly or weekly rollups, plus 30 daily buckets |
| `reviews` | review | GetAppReviews | Sample of up to 100 most helpful and 100 most recent reviews per game. Author SteamID is SHA-256 hashed. Text is off by default. |
| `engagement_snapshots` | app × day | SteamSpy | Owners range, playtime average and median, CCU |
| `player_count_snapshots` | app × capture | GetNumberOfCurrentPlayers | |
| `news_events` | news item | GetNewsForApp | `is_patch` heuristic |

## mart (Power BI)
| Object | Type | Grain | Relationships |
|---|---|---|---|
| `dim_game` | materialized view | app | Hub: links to all facts and bridges via `app_id`. Holds `price_bucket_id`, `primary_tag`, `in_analytical_scope`. |
| `fact_game_metrics` | materialized view | app | 1:1 with `dim_game`. All per-game metrics. |
| `dim_price_bucket`, `dim_success_tier`, `dim_date` | view | | `dim_game.price_bucket_id`, `fact_game_metrics.success_tier_id`, date columns |
| `bridge_game_tag` → `dim_tag` | view | app × tag (top 5 non-meta) | Many-to-many. Filter direction is dim_tag → bridge → dim_game. |
| `bridge_game_genre` → `dim_genre`, `bridge_game_category` → `dim_category` | view | | Same pattern |
| `tag_summary`, `price_bucket_summary` | materialized view | tag; bucket | Pre-aggregated medians and rates for in-scope games |
| `tag_year_summary` | view | tag × launch year | Supply versus demand |
| `fact_price_history`, `fact_catalog_daily`, `fact_review_histogram`, `fact_ccu_daily` | view | app × time | Drill-through time series |
| `analysis_results` | table | analysis × segment × metric | Written by `steam-intel analyze` |
| `data_freshness`, `data_coverage`, `dq_latest` | view | | Methodology & Data Quality page |
