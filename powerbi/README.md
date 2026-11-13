# Power BI report: build guide

Save the report as a **Power BI Project (`.pbip`)** in this folder (File → Save as → Power BI project), so it can be diffed in git.

## 1. Connect
1. Get Data → **PostgreSQL database**. Server: `localhost:5432`, Database: `steam_intel`. Mode: **Import**.
2. Select these objects from the `mart` schema:

| Object | Role in the model |
|---|---|
| `dim_game` | Hub dimension |
| `fact_game_metrics` | One row per game, holding all metrics |
| `dim_price_bucket` | Dimension |
| `dim_success_tier` | Dimension |
| `dim_date` | Dimension |
| `bridge_game_tag` + `dim_tag` | Tag dimension, linked through a bridge |
| `bridge_game_genre` + `dim_genre` | Genre dimension, linked through a bridge |
| `bridge_game_category` + `dim_category` | Category dimension, linked through a bridge |
| `tag_summary` | Pre-aggregated summary |
| `tag_year_summary` | Pre-aggregated summary |
| `price_bucket_summary` | Pre-aggregated summary |
| `fact_price_history` | Time series |
| `fact_catalog_daily` | Time series |
| `fact_review_histogram` | Time series |
| `fact_ccu_daily` | Time series |
| `analysis_results` | Statistics for cards |
| `data_freshness`, `data_coverage`, `dq_latest` | Methodology & Data Quality page |

3. In Power Query, strip the `mart ` prefix from the query names.

## 2. Relationships (Model view)
| From (many) | To (one) | Direction |
|---|---|---|
| fact_game_metrics[app_id] | dim_game[app_id] | Both directions (1:1) |
| dim_game[price_bucket_id] | dim_price_bucket[price_bucket_id] | Single |
| fact_game_metrics[success_tier_id] | dim_success_tier[success_tier_id] | Single |
| dim_game[launch_date] | dim_date[date] | Single |
| bridge_game_tag[app_id] | dim_game[app_id] | **Both** (lets a tag slicer filter games) |
| bridge_game_tag[tag_id] | dim_tag[tag_id] | Single |
| bridge_game_genre / bridge_game_category | Same pattern as tags | |
| fact_price_history, fact_catalog_daily, fact_review_histogram, fact_ccu_daily [app_id] | dim_game[app_id] | Single |
| tag_summary[tag_id], tag_year_summary[tag_id] | dim_tag[tag_id] | Single |
| price_bucket_summary[price_bucket_id] | dim_price_bucket[price_bucket_id] | Single |

Leave `analysis_results`, `data_freshness`, `data_coverage` and `dq_latest` disconnected.

Sort `dim_price_bucket[price_bucket]` by `price_bucket_id`, and `dim_success_tier[success_tier]` by `success_tier_id`.

## 3. Measures
Paste the measures from [measures.dax](measures.dax) into a `_Measures` table.

## 4. Report-level filter
Put `dim_game[in_analytical_scope] = True` on every page except Market Overview. Market Overview should show the long tail.

## 5. Pages
| Page | Visuals |
|---|---|
| **1. Market Overview** | KPI cards: Games, Median Launch Price, % Very Positive+, Total Reviews, Median Playtime. Column chart of games by `dim_date[year]`. Bar chart of `dim_success_tier` with `Games`. Top 15 `dim_tag` by Games. Card showing Share Under 10 Reviews and the Gini from analysis_results. |
| **2. Early Sentiment → Popularity (Q1)** | Scatter: x = early_positive_ratio_30d, y = reviews_d31_365 (log axis), details = app_id, legend = primary_tag. Clustered column of `early_sentiment_band` against Median Later Reviews and Hit Rate, with an `early_volume_band` slicer. Cards: Q1 Spearman rho, Q1 Effect per +10pp, Q1 Sample n. |
| **3. Genre Sentiment (Q2)** | Bar of `tag_summary[share_very_positive_plus]` by tag, filtered to `n_games_50plus_reviews >= 30`, with an n label. Matrix heatmap of dim_tag by launch year showing % Very Positive+. Field parameter toggling Tag and Genre. |
| **4. Pricing (Q3)** | Combo chart of `dim_price_bucket` showing Hit Rate (columns) and Median Reviews (line). Matrix of primary tag by price bucket showing Hit Rate with conditional formatting. Histogram of days_to_first_discount. |
| **5. Engagement (Q4)** | Scatter of `tag_summary`: x = median_owners (log), y = median_of_median_playtime_h, size = n_games. Add average lines to form quadrants. Table of tags with median playtime, active ratio and n. |
| **6. Opportunity Explorer** | Scatter of `tag_year_summary` (latest full year): x = n_releases, y = median_reviews. Bar charts of Hit Rate by was_early_access, is_self_published, steam_deck_status, language_band and has_demo. |
| **7. Game Drill-through** | Drill-through on dim_game[app_id]. Step line of fact_price_history. Stacked columns of fact_review_histogram (up/down). Line of fact_ccu_daily. Tag list and KPI cards. |
| **8. Methodology & Data Quality** | Text from docs/methodology.md. Tables of data_coverage, data_freshness and dq_latest. Card showing DQ Checks Failing. |

**Design rules:**
- Always show `n` next to a rate.
- Use medians, not means.
- Use log axes for reviews and owners.
- Tooltips explain each estimate.

## 6. Refresh
- Desktop: click Refresh after `steam-intel daily` has run.
- Power BI Service: install the on-premises data gateway and schedule the refresh after 06:00.
