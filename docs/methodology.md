# Methodology

This page defines every metric on the dashboard and lists the known biases. The dashboard's "Methodology & Data Quality" page summarizes it.

## Universe and scope
- **Indie universe:** released Steam games carrying the user tag **Indie** (tag id 492), listed via `IStoreQueryService/Query`. Steam's self-assigned **Indie genre** (id 23, from `appdetails`) is kept as a secondary flag, `is_indie_genre`.
- **Analytical scope** (`dim_game.in_analytical_scope`): launched on or after 2018-01-01, launched no later than today, and at least 10 Steam-purchase reviews. Delisted games stay in scope. Market-wide views such as concentration and release counts use the whole universe.
- **Launch date:** the first date the game was purchasable on Steam. For Early Access games that's the Early Access start (`original_steam_release_date`), otherwise `steam_release_date`. All review windows are anchored to this date.
- **Delisted games** are soft-deleted (`delisted_at`) and kept in every rate and in the analytical scope, to avoid survivorship bias. Per-game collection skips them, because their store pages no longer respond.
- **Remaining survivorship gap:** Steam's catalog API lists only games on sale today. Games removed before the first crawl (13 Nov 2026) were never captured, so success rates for older cohorts are somewhat optimistic. The pipeline can't recover them retroactively.
- **Missing review counts:** Steam's catalog returns a review count for every listed game, including an explicit 0. In the 2018–2024 launch cohorts no count is missing (34,116 games, 2,674 with 0 reviews). Hit-rate queries still use `COALESCE(total_reviews, 0)`, so that a game with a missing count stays in the denominator as "not a hit" instead of silently inflating the rate. Ratios such as `positive_ratio` stay NULL when there are no reviews.

## Review metrics
All review counts match the store page. For paid games that means **Steam purchases only** (`purchase_type=0`). Free games have no purchases, so `purchase_type=0` returns almost nothing for them (Splitgate: 165 reviews against 110,314). For free games the store page and GetItems count **all reviews** (`purchase_type=1`), and so do we.

| Metric | Definition |
|---|---|
| `positive_ratio` | positive / total (lifetime) |
| `wilson_lower_bound` | 95% Wilson lower bound of `positive_ratio`. Ranks games fairly when review counts differ a lot. |
| `steamdb_rating` | `r − (r − 0.5) · 2^(−log10(n+1))`. Pulls low-volume games toward 50%. |
| `is_very_positive_plus` | ≥ 50 reviews and ≥ 80% positive (roughly Steam's "Very Positive" or better) |
| `early_positive_ratio_30d` | positive share of reviews written in days 0–30 after launch |
| `reviews_d31_365` | reviews written in days 31–365, the "later popularity" measure for Q1 |
| `early_volume_band` | review count in the first 30 days: <10 / 10–49 / 50–249 / 250+. Used as a control. |
| `success_tier_id` | lifetime reviews: <50 / 50–499 / 500–4,999 / 5,000+ |
| `is_hit_1k` | ≥ 1,000 lifetime reviews. "Success rate" means the share of games that are hits. |

**Why review counts measure popularity:** Steam doesn't publish sales. Reviews are an exact, public count that's proportional to sales (the Boxleiter method estimates about 30–60 sales per review), so `boxleiter_sales_low/high` are shown only as rough ranges.

## Engagement metrics (SteamSpy)
SteamSpy values are **model estimates**, not Steam data. Owners come as a range, and we use the midpoint. `median_forever` is the median lifetime playtime of owners.
- `active_ratio_2w = avg_2weeks / avg_forever` is a recency or retention proxy.
- `median_to_avg_ratio`: low values mean a few players play a lot while most play little.
- `peak_ccu_observed` comes from our own daily `GetNumberOfCurrentPlayers` snapshots, so it is a lower bound of the true peak.

## Price metrics
- **Launch price** (`launch_price_usd`) is the list (pre-discount) price at first observation. For games launched before collection began, this approximates the launch price. List prices for indie games rarely change after launch, but price cuts do happen. An IsThereAnyDeal backfill can replace it later (`launch_price_source`).
- **Price buckets:** Free, under $5, $5–9.99, $10–14.99, $15–19.99, $20–29.99, $30+.
- `price_history` is SCD Type 2. A row opens only when initial price, final price or discount changes. Bundle prices are ignored.
- `days_to_first_discount` is only computed for games we observed from launch week onward.

## Statistics (mart.analysis_results)
- **Q1:** Spearman ρ between `early_positive_ratio_30d` and `reviews_d31_365`, overall and within each `early_volume_band`. Plus OLS: `log(1+later reviews) ~ early ratio + log(1+early reviews) + launch price + launch year + primary tag (top 15)`, with HC3 robust errors. Included games need ≥10 early reviews and a completed d31–365 window, so they launched at least one year ago. The regression needs n ≥ 200.
- **Q3:** Kruskal-Wallis test of lifetime reviews across price buckets.
- **Q4:** Spearman ρ between median playtime and estimated owners.
- **Market concentration:** Gini coefficient of review counts, the top 1% share, and the share of games with fewer than 10 reviews.
- These results show association, not causation. Better-made games plausibly get both better early reviews and more later sales.

## Known biases
1. **Selection:** scope requires ≥10 reviews, which excludes the many games that never found an audience. The market overview shows that long tail separately.
2. **Tag drift:** user tags are voted on and change over time. We store the current top 20.
3. **Reviewer playtime** (in the review sample) skews toward engaged players.
4. **SteamSpy** owner and playtime estimates can lag or be missing for small games.
5. **Off-topic review bombs** are excluded by Steam's default filter.
