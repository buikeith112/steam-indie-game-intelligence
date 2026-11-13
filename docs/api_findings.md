# Phase 1: API Exploration Findings (probed 2026-11-13)

Live probes against the APIs below. Sanitized responses are in [`tests/fixtures/`](../tests/fixtures/), with reviewer SteamIDs and review text replaced by placeholders.

## Summary

| Endpoint | Key? | Result | Use in pipeline |
|---|---|---|---|
| `api.steampowered.com/IStoreQueryService/Query/v1` | **No** | 200. Filtering on `tagids_must_match=[492]` (Indie tag), `released_only` and `include_games` returns **65,468** apps. Max `count` is 1,000 for ID-only pages and deep pagination works (`start=60000`). Adjacent pages don't overlap. | **Catalog source.** About 66 calls list the whole indie universe. |
| `IStoreBrowseService/GetItems/v1` | **No** | 200 for ≤200 appids per GET. 500 appids gives an HTTP 400 (URL length), and POST returns 405. | **Bulk metadata.** Returns tags with weights, a review summary, release timestamps (including Early Access dates), platforms, Steam Deck category, best price, developers and publishers. About 330 calls cover all indie games. ⚠ `best_purchase_option` can be a **bundle** (it has `bundleid` and `included_game_count > 1`), so those prices are ignored. |
| `IStoreService/GetAppList/v1` | **Yes** | 403 without a key. | Optional. Gives catalog-wide `last_modified` and `price_change_number` deltas once a key is configured. |
| `ISteamApps/GetAppList/v2` | n/a | **404** ("Method not found"). | Retired. Don't use. |
| `store.steampowered.com/api/appdetails` | No | 200 for a single appid. **Multiple appids return 400 unless you pass `filters=price_overview`**, which does return a batch. | Per-game details for candidates: genres (incl. Indie id 23), categories, languages, demos, DLC, metacritic, controller support, ratings. Batched daily price check. |
| `IUserReviewsService/GetAppReviews/v1` (`input_json`) | No | 200. With `date_range_start/end`, `query_summary` returns **`total_positive` and `total_negative` within the window in a single call**. Balatro days 0–30: 20,817 up and 480 down, matching 21,297 total. | Q1 review windows (1 call per window), review sample, lifetime summary. |
| `store.steampowered.com/appreviews/{id}?json=1` | No | **302 → store homepage.** | Retired, which confirms the reported Oct 2026 shutdown. |
| `store.steampowered.com/appreviewhistogram/{id}` | No | 200. `rollup_type` is `month` for older games, with a `rollups[]` array of `{date, recommendations_up, recommendations_down}` and `recent[]` holding 30 daily buckets. | Sentiment-over-time curve for drill-through and the discount-spike question. |
| `ISteamUserStats/GetNumberOfCurrentPlayers/v1` | No | 200: `{player_count, result}`. | Daily CCU snapshots. |
| `ISteamNews/GetNewsForApp/v2` | No | 200. | Optional update cadence. |
| `steamspy.com/api.php?request=appdetails` | No | 200. Returns owners as a range string ("2,000,000 .. 5,000,000"), playtime in minutes, `ccu`, prices as strings in cents, and `tags` with votes. | Engagement estimates (Q4). |
| `steamspy.com/api.php?request=genre&genre=Indie` | No | 200, but only **1,642 apps** (it's truncated). | **Not usable as a catalog.** |
| `store.steampowered.com/tagdata/populartags/english` | No | 200. 429 `{tagid, name}` pairs. | Tag dictionary that maps `tagid` to a name. |

## Decisions these findings change in the plan

1. **The universe comes from `IStoreQueryService/Query` with the Indie tag (492), not from SteamSpy.** No API key is needed to bootstrap. Steam's "Indie" genre (id 23) from `appdetails` is still stored as `is_indie_genre`, a secondary flag.
2. **Review counts use `purchase_type=0`** (Steam purchases only). This matches the store page and GetItems `summary_filtered`: Balatro has 163,033 reviews this way versus 201,389 with all purchase types. Using one definition everywhere keeps the numbers comparable. **Exception, found later:** free games have no purchases, so `purchase_type=0` returns almost nothing for them (Splitgate: 165 against 110,314). For free games the store page counts all reviews (`purchase_type=1`), so the pipeline uses 1 for them.
3. **Q1 windows cost 1 call each, not 2,** because `query_summary` honours the date range. We'll fetch 3 windows per game (`d0_30`, `d31_365`, `d366_730`), and only after each window has closed.
4. **Early Access** comes from GetItems `release`: `original_steam_release_date` and `release_from_early_access_date`. The "launch date" used to anchor review windows is the **first date the game was purchasable** (the original release date if present, otherwise `steam_release_date`).
5. **Steam Deck**: GetItems `platforms.steam_deck_compat_category` uses 0 = unknown, 1 = unsupported, 2 = playable, 3 = verified.

## Pagination stability (`IStoreQueryService/Query`)
We ran a full ID pass with each sort order and counted unique IDs against the 65,471 records reported:

| `sort` | Unique IDs returned | Result |
|---|---|---|
| 0 | 43,214 | Lost 34% |
| 10 | 44,076 | Lost 33% |
| 21 | 60,293 | Lost 8% |
| 1 | 65,471 | Complete |
| **2 (ascending appid)** | **65,471** | **Complete, and deterministic** |

The pipeline uses `sort=2`. `fetch_catalog_ids` logs a warning if unique IDs fall below 99.5% of `total_matching_records`.

## Observed throughput
- GetAppReviews: 60 sequential calls in 12.6 s (about 4.7 per second) with no 429s. The pipeline still budgets **2 per second** for that host.
- GetItems: 200 items in about 0.3 s per call.
- appdetails: we use the community-reported limit of about 200 per 5 min, budgeted at **0.6 per second**.

## Scale: measured on the full catalog crawl (2026-11-13)
- **65,471** indie apps: 66 ID pages and 328 GetItems batches, about 6 min, with 0 hidden items. Four HTTP 429s were absorbed by the retry logic.
- Launched in 2018 or later: 55,113. **In analytical scope (≥10 reviews): 27,051.** Of those, 22,602 launched at least a year ago, so their Q1 windows are complete.
- 28,062 games launched since 2018 have fewer than 10 reviews. That long tail is kept for market-concentration views.
- Per-game bootstrap for 27k games at the default rates, by host:

| Host | Sources | Time |
|---|---|---|
| store | appdetails + histogram | about 25 h at 0.6/s |
| webapi | reviews + review_sample + players + news | about 30 h at 2/s |
| steamspy | steamspy | about 7.5 h at 1/s |

- The hosts have separate rate limits, so running one `steam-intel work` process per host in parallel brings wall time down to about 30 h. The queue uses `FOR UPDATE SKIP LOCKED`, so concurrent workers are safe.
