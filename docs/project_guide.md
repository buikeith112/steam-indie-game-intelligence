# Project Guide: How This Was Built, and How to Rebuild It

This is the companion to the README. It explains the build order, the reasoning behind each decision, the problems found along the way, and what is still open. Read it before rebuilding the project or changing its design.

**Contents**
1. [What the project is](#1-what-the-project-is)
2. [Build order, step by step](#2-build-order-step-by-step)
3. [How each part works](#3-how-each-part-works)
4. [Decision log](#4-decision-log)
5. [Problems found and how they were solved](#5-problems-found-and-how-they-were-solved)
6. [What I would do differently](#6-what-i-would-do-differently)
7. [Current status and open work](#7-current-status-and-open-work)
8. [Operations cheat sheet](#8-operations-cheat-sheet)

---

## 1. What the project is

**Goal:** answer four business questions about indie games on Steam with a dataset I built myself:
- Does early sentiment predict popularity?
- Which genres are reviewed best?
- What price works?
- Which genres retain players?

The deliverable is a Power BI dashboard on top of that dataset.

**The pieces:**
```
Steam APIs ─► Python pipeline ─► PostgreSQL (raw → core → mart) ─► Power BI
   │              │                     │
   │              ├─ HttpClient: rate limits, retries, circuit breaker
   │              ├─ clients: one wrapper per endpoint
   │              ├─ parsers (pure functions) + loaders (upserts)
   │              ├─ queue workers (resumable)
   │              └─ quality checks, statistics, daily job
   └─ store APIs, Web API services, SteamSpy
```

**Stack:**
- Python 3.12: `httpx`, `SQLAlchemy` 2 with `psycopg` 3, `alembic`, `pydantic-settings`, `typer`, `pandas`, `scipy`, `statsmodels`, `pytest`, `ruff`.
- PostgreSQL 16. During development it runs embedded through `pgserver`.
- Power BI Desktop.

---

## 2. Build order, step by step

The order matters. Each step depends on what the previous one proved.

### Step 0: Set up the environment
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -e ".[dev,analysis]"
copy .env.example .env
```
Use a `pyproject.toml` with a `src/` layout, so the package is importable and the `steam-intel` command is installed. Keep secrets in `.env`, which is git-ignored, and commit `.env.example` as the template.

### Step 1: Explore the APIs before designing anything
This step changed the most decisions, so don't skip it.

1. **Find candidate endpoints.**
   - Valve's official docs: https://partner.steamgames.com/doc/webapi
   - xPaw's Steam Web API reference, which also covers undocumented services: https://steamapi.xpaw.me
   - SteamSpy's API page: https://steamspy.com/api.php
   - Community reports, for rate limits.
2. **Write a throwaway probe script.** It calls each endpoint once for a few well-known games (Balatro 2379780, Hades 1145360, Stardew Valley 413150) and saves the raw JSON. For each endpoint, record the HTTP status, the response shape, which fields exist, and whether a key is needed.
3. **Test the assumptions the design depends on:**
   - The maximum batch size for multi-ID endpoints.
   - Whether date filters really filter.
   - Whether deep pagination works.
   - Whether the old endpoints still respond.
   - How fast you can call before getting HTTP 429.
4. **Save sanitized responses** as test fixtures, with reviewer IDs and text replaced. They become the unit-test inputs.
5. **Write the findings down** ([api_findings.md](api_findings.md)) before writing pipeline code.

**The request shapes that matter**, all JSON passed as the `input_json` query parameter:
```jsonc
// Catalog page: IStoreQueryService/Query/v1 (no key)
{"query": {"start": 0, "count": 1000, "sort": 2,
           "filters": {"released_only": true, "type_filters": {"include_games": true},
                       "tagids_must_match": [{"tagids": [492]}]}},
 "context": {"language": "english", "country_code": "US"}}

// Bulk details: IStoreBrowseService/GetItems/v1 (no key, at most 200 ids per GET)
{"ids": [{"appid": 2379780}, ...], "context": {...},
 "data_request": {"include_basic_info": true, "include_tag_count": 20, "include_reviews": true,
                  "include_release": true, "include_platforms": true}}

// Review counts inside a date window: IUserReviewsService/GetAppReviews/v1 (no key)
{"appid": 2379780, "filter": 1, "num_per_page": 1, "purchase_type": 0, "languages": ["all"],
 "date_range_start": 1708387200, "date_range_end": 1710979199}
// → response.query_summary.total_positive / total_negative cover that window only
```
Also used:
- `store.steampowered.com/api/appdetails?appids=X&cc=us&l=english`, which takes one ID at a time unless you pass `filters=price_overview`.
- `store.steampowered.com/appreviewhistogram/X`
- `ISteamUserStats/GetNumberOfCurrentPlayers`
- `ISteamNews/GetNewsForApp`
- `steamspy.com/api.php?request=appdetails&appid=X`
- `store.steampowered.com/tagdata/populartags/english`

### Step 2: Design the database
1. **Three layers** in separate PostgreSQL schemas:
   - `raw`: every API response as JSONB.
   - `core`: normalized tables.
   - `mart`: views for Power BI.
   - Plus `ops` for bookkeeping: runs, queue, data-quality results, checkpoints.
2. **Write the DDL as plain SQL files** (`db/sql/00N_*.sql`), each applied by a tiny Alembic migration. Hand-written SQL keeps full control over PostgreSQL features: JSONB, partial unique indexes, materialized views, `FILTER`, `percentile_cont`.
3. **Settle the grain of every table first.** For example, `core.games` has one row per app, `review_windows` one row per app per window, and `price_history` one row per app per price version.

### Step 3: Build the HTTP layer
One `HttpClient` handles every request:
- **Token bucket per host**, because each host has its own limit:
  - store: 0.6 requests/sec
  - Web API: 2/sec
  - SteamSpy: 1/sec
- **Retries on 429 and 5xx**, up to 6 attempts. The wait is exponential with jitter, or follows `Retry-After` when the server sends one. A 429 also pushes the bucket into "debt", so the next calls wait too.
- **Circuit breaker on 403**: pause 5 minutes, and stop the job if 403s keep coming. Steam uses 403 for temporary IP blocks, and hammering through one makes the block longer.
- **Tests run against a mock transport**, with no network.

Then write one thin client method per endpoint. Clients return the untouched JSON so it can be stored before parsing.

### Step 4: Write parsers and loaders
- **Parsers** are pure functions that turn JSON into Python dicts shaped like table rows. There's no database access, so each one is unit-tested against the fixtures. The fiddly cases live here:
  - mixed date formats
  - "2,000,000 .. 5,000,000" owner ranges
  - company-name variants
  - full-audio language markers
  - bundle prices
  - Early Access dates
- **Loaders** are upserts (`INSERT ... ON CONFLICT DO UPDATE`), so re-running anything is safe:
  - Game updates never overwrite a known value with NULL (`COALESCE`), because the two sources fill different columns.
  - Price history is SCD Type 2. A new row opens only when the price changes, and the previous row gets an end date.

### Step 5: Build ingestion
- **Catalog crawl:**
  1. Page through Query in 1,000-ID chunks with `sort=2`.
  2. Batch the IDs into GetItems calls of 200.
  3. Upsert games, tags, companies, prices and a daily catalog snapshot.

  This takes about 6 minutes for 65k games.
- **Candidate selection:** pick in-scope games (launched 2018 or later, ≥10 reviews) and write one queue row per game per source into `ops.ingestion_queue`.
- **Workers:**
  - Claim 25 items at a time with `FOR UPDATE SKIP LOCKED`.
  - Process each item in its own transaction: fetch, save raw, parse, upsert, mark done.
  - On failure, record the error and schedule a retry with exponential backoff, up to 5 attempts.
  - On a circuit-breaker trip, release the claimed items and stop.
- **Run one worker process per API host** in parallel. Each host's rate limit is independent, so this cuts wall time from about 60 h to about 30 h.

### Step 6: Build marts, statistics and quality checks
- **`mart.dim_game` and `mart.fact_game_metrics`** are materialized views with one row per game. They hold every derived metric: Wilson score, early-window ratios, success tier, price bucket, primary tag.
- **Bridge tables** handle the many-to-many links between games and tags, genres and categories.
- **Pre-aggregated summaries** (`tag_summary`, `price_bucket_summary`) avoid double counting in Power BI.
- **`analysis.py`** computes statistics with scipy and statsmodels and writes them to `mart.analysis_results`, so Power BI shows the same numbers as the analysis.
- **`quality.py`** runs 10 checks and logs pass or fail to `ops.data_quality_results`.

### Step 7: Build the daily pipeline
`steam-intel daily` runs these steps in order:
1. Full catalog crawl, which refreshes prices and review counts for all 65k games and detects new and removed games.
2. Re-queue per-game work on a cadence (see `daily.py`).
3. Drain the queues within a time budget.
4. Refresh the marts.
5. Recompute statistics.
6. Run the quality checks.

Windows Task Scheduler runs it at 03:00 through `scripts/run_daily.ps1`.

### Step 8: Test along the way
- 50 tests: parser tests against real fixtures, and HTTP tests with mocked 429, 403 and 503 responses.
- Database tests on an embedded PostgreSQL, including one full pipeline run with mocked APIs that checks the marts.
- A **live smoke test** before any large run: `bootstrap --limit 60` (0 failures).
- **Spot checks** against the store page, such as Balatro's review totals and price.

### Step 9: Build the dashboard (next)
See [powerbi/README.md](../powerbi/README.md) for the connection steps, relationships, DAX measures and page layout.

---

## 3. How each part works

| File | Responsibility |
|---|---|
| `src/steam_intel/config.py` | Settings from `.env`: database URL, request rates, scope (2018, 10 reviews), review-text flag |
| `src/steam_intel/http.py` | `RateLimiter` (token bucket), `HttpClient` (retries, circuit breaker), `ApiResult` |
| `src/steam_intel/clients.py` | `SteamClient`: one method per endpoint, plus batch and sort constants |
| `src/steam_intel/transform/parsers.py` | Pure JSON → row functions, date and name normalization, privacy scrub |
| `src/steam_intel/transform/load.py` | Upserts, SCD-2 price history, tag dictionary with the `is_meta` list |
| `src/steam_intel/db.py` | Engine, run log, raw store, queue (enqueue, claim, done, failed, release, reset_stale), state |
| `src/steam_intel/ingest.py` | Catalog crawl, candidate selection, per-source handlers, worker loop |
| `src/steam_intel/daily.py` | Refresh cadence rules and the daily orchestration |
| `src/steam_intel/quality.py` | Mart refresh order and the 10 data-quality checks |
| `src/steam_intel/analysis.py` | Q1 Spearman and OLS, Q3 Kruskal-Wallis, Q4 Spearman, Gini, segment hit rates |
| `src/steam_intel/cli.py` | `steam-intel` commands |
| `db/sql/001–003` | Schema: raw, ops, core, catalog snapshots, marts |

**One game's journey through the system:**
1. Query lists its app ID.
2. GetItems fills `core.games` (launch date, tags, review count, price) and writes a `catalog_snapshots` row and a `price_history` row.
3. The game is in scope, so 7 queue rows are created.
4. The `appdetails` worker adds genres, languages, categories and the self-published flag.
5. The reviews worker writes a lifetime snapshot plus three windowed counts (days 0–30, 31–365 and 366–730 after launch).
6. The SteamSpy worker adds owner and playtime estimates.
7. The other workers add player counts, the review histogram, a review sample and news.
8. `refresh-marts` turns all of it into one row in `fact_game_metrics`.

---

## 4. Decision log

Each entry gives the decision, why it was made, and the alternative that was rejected.

| # | Decision | Why | Alternative rejected |
|---|---|---|---|
| D1 | **Build the dataset from APIs, not Kaggle** | The point of the project is showing data acquisition. Kaggle dumps are stale snapshots with no price or review history. | A Kaggle dataset (kept only as an idea for a sanity check) |
| D2 | **"Indie" = user tag 492, with Steam genre 23 as a secondary flag** | The tag is what players see and search for, and Query can filter on it directly. The genre is self-assigned by developers. Both are stored so the definition can be swapped. | Genre-only, which needs `appdetails` for every Steam game first, about 7× more calls |
| D3 | **Catalog from `IStoreQueryService/Query`, not `GetAppList`** | No key needed, filters to indie games server-side, and about 66 calls cover everything | `GetAppList` (needs a key, returns all 150k+ apps) or SteamSpy (truncated) |
| D4 | **Bulk metadata from GetItems in batches of 200** | Batching cut catalog time from days to minutes | `appdetails` for everything (about 30 h for the catalog alone) |
| D5 | **Popularity = Steam-purchase review count** | Exact, public, available for every game, and widely used as a sales proxy | SteamSpy owners (model estimates in wide ranges), CCU (a snapshot that favours multiplayer games) |
| D6 | **Review counts match the store page: `purchase_type=0` (Steam purchases) for paid games, `1` (all) for free games** | Matches the store page and GetItems counts. Balatro has 163k reviews with 0 against 201k with all purchase types. Free games have no purchases, so 0 nearly zeroes them out (Splitgate: 165 against 110,314). This was caught when the per-game totals disagreed with the catalog for every free game. | One purchase type for every game |
| D7 | **Early sentiment from date-ranged review counts** | One call per window gives exact positive and negative counts. Paging through every review is impossible for games with 100k+ reviews. | Downloading all reviews, or the monthly histogram (too coarse for a 30-day window) |
| D8 | **Windows anchored to the first purchasable date** (Early Access start if any) | Early Access games collect reviews from their EA launch. Anchoring to the 1.0 date would mislabel years of reviews as "early". | Anchoring to `steam_release_date` |
| D9 | **Scope: launched 2018 or later, ≥10 reviews** | 2018+ keeps the market comparable. Below 10 reviews, a percentage is noise. The long tail stays in the database for market-shape analysis. | All games (most of the work would go to games with 0–9 reviews) |
| D10 | **Hit = ≥1,000 reviews; success measured as a rate, with medians** | Review counts are extremely skewed (the top 1% hold 71% of reviews), so means are meaningless. | Average reviews per segment |
| D11 | **Wilson lower bound and SteamDB-style rating for sentiment** | 9 of 10 positive shouldn't outrank 950 of 1,000. Both methods penalize small samples. | Raw % positive |
| D12 | **PostgreSQL** | Native Power BI connector, JSONB for raw payloads, materialized views, `SKIP LOCKED` queues, window and percentile functions | SQLite or DuckDB (weaker for concurrent writers and Power BI), SQL Server (heavier) |
| D13 | **Keep a raw JSONB layer** | Parsing bugs can be fixed and replayed without calling the API again. It also serves as an audit trail. | Parse on the fly and discard |
| D14 | **SCD Type 2 price history** | Shows when prices and discounts changed. One row per change, not per day. | Daily price rows (the catalog snapshot already gives a daily panel) |
| D15 | **Database-backed queue** | Crash-safe and resumable, with retries and backoff, visible in SQL, and no extra infrastructure | Celery or Redis (more moving parts), in-memory loops (lose progress on a crash) |
| D16 | **One worker per API host** | Rate limits are per host, so parallel hosts are free throughput. Limiters live inside each process, so a second worker on the same host would double that host's request rate and risk 429s or a temporary ban. | async concurrency within one process |
| D17 | **Shuffled queue order** (deterministic hash) | A partly finished crawl is an unbiased sample. App-ID order would mean oldest games first. | Processing the biggest games first (biased interim results) |
| D18 | **Tags, not Steam genres, as the "genre" dimension** | Steam has about 30 coarse genres and every game is Indie or Action. Tags carry the meaning (Roguelike, Metroidvania, Cozy). The top 5 non-meta tags are used. | Steam genres only (too coarse) |
| D19 | **Pre-aggregated tag summaries in SQL** | Many-to-many tag bridges double-count in DAX unless measures are written very carefully | Doing everything in DAX |
| D20 | **Statistics computed in Python, displayed in Power BI** | scipy and statsmodels are the right tools for Spearman, OLS and Kruskal-Wallis, and writing results to a table keeps one source of truth | DAX approximations of correlation |
| D21 | **Hash SteamIDs, drop review text by default, raw included** | Avoids storing personal identifiers unnecessarily. Hashing is idempotent so reprocessing stays consistent. | Storing raw IDs |
| D22 | **Hand-written SQL migrations** | Full control over PostgreSQL features. The schema is small enough to manage by hand. | ORM models plus autogenerate |
| D23 | **Embedded PostgreSQL (`pgserver`) for development** | Docker and PostgreSQL weren't installed. `pgserver` gives a real PostgreSQL 16 for tests and development without admin rights. | Waiting for an install, or SQLite in tests (different SQL dialect) |

---

## 5. Problems found and how they were solved

These make good interview stories. Each one was found by checking real data, not by reading documentation.

1. **Retired endpoints.**
   - Problem: most tutorials use `ISteamApps/GetAppList/v2` and `store.steampowered.com/appreviews`. The first now returns 404, and the second redirects to the store homepage.
   - Found by: the Phase 1 probes.
   - Fix: switched to `IStoreQueryService/Query` and `IUserReviewsService/GetAppReviews`.
2. **Batch limits weren't documented.**
   - Problem: GetItems fails at 500 IDs (HTTP 400, because the URL gets too long), and POST isn't allowed (405). Multi-ID `appdetails` fails unless you pass `filters=price_overview`.
   - Fix: batches of 200, and single-ID `appdetails`.
3. **SteamSpy's genre list is truncated.**
   - Problem: `genre=Indie` returned 1,642 apps, against about 65k real ones.
   - Fix: SteamSpy is used only for per-game engagement data.
4. **Pagination silently dropped games.**
   - Problem: the first full catalog run reported 65,468 records but returned only 60,230 unique IDs. Ordering shifts between pages.
   - Diagnosis: ran a full ID pass for each sort order and counted unique IDs. Sorts 0 and 10 lost about a third, sort 21 lost 8%, and **sort 2 (ascending app ID) lost none**.
   - Fix: switched to sort 2 and added a coverage warning at 99.5%.
   - Lesson: always compare the count you received with the count the API claims.
5. **Bundle prices leaked into price history.**
   - Problem: the spot check showed Crime Scene Cleaner launching at $44.98. GetItems' `best_purchase_option` was a two-game bundle.
   - Fix: ignore options with `bundleid` or `included_game_count > 1`, and added a regression test.
6. **™ normalization order.**
   - Problem: Unicode NFKC turns "™" into the letters "TM", so "Annapurna Interactive™" didn't merge with the plain name.
   - Fix: strip the symbols before normalizing. A unit test caught this.
7. **PostgreSQL `FILTER` syntax.**
   - Problem: `max(x)::float8 FILTER (...)` is a syntax error.
   - Fix: cast the whole aggregate instead: `(max(x) FILTER (...))::float8`.
8. **Small-sample regression.**
   - Problem: the 60-game smoke test produced a rank-deficient OLS, with year and tag dummies outnumbering the data.
   - Fix: the regression now needs n ≥ 200, and correlations need n ≥ 30.
9. **Biased interim results.**
   - Problem: the queue processed games in app-ID order, so a partial run would have meant "oldest games first".
   - Fix: deterministic shuffled priority.
10. **Personal data in the raw layer.**
    - Problem: `core.reviews` hashed SteamIDs, but `raw.api_responses` stored full payloads with plain IDs and review text. Found while writing this guide.
    - Fix: scrub review payloads before storage, and backfill the rows already stored. 1,942 payloads were scrubbed and 0 unscrubbed reviews remain.
11. **Unexplained 2026 surge (still open).**
    - Indie launches per month climb from about 750 in early 2026 to 2,039 in October. Many of those games have 0 reviews.
    - It might be a real release surge (cheap or AI-assisted development) or a catalog effect.
    - Handled by: rates use 2018–2024 launches only, and 2026 is labelled year-to-date. See §7.

---

## 6. What I would do differently

**Process**
- **Run the stability checks earlier.** The pagination test (comparing unique IDs to the reported total) should be part of Phase 1 for any paginated API. Here it was found after the first full run.
- **Develop against a 2–5k game sample first,** then scale. Iterating on marts is faster on a small, representative sample.
- **Add CI from day one:** GitHub Actions running `ruff` and `pytest`. The database tests already use embedded PostgreSQL, so they'd run in CI unchanged.

**Architecture**
- **dbt for the transform and mart layer.** Marts currently live in Alembic migrations, so changing a view means writing a new migration. dbt would give versioned models, built-in tests (`unique`, `not_null`, relationships) and generated lineage docs.
- **An orchestrator (Prefect or Dagster) instead of Task Scheduler.** That adds retries per step, run history, alerts and a UI. Task Scheduler is fine for one machine but invisible.
- **Raw payloads as compressed Parquet in object storage,** keeping only the latest per app in PostgreSQL. JSONB raw is convenient but grows quickly: GetItems payloads are about 38 MB per daily crawl. Raw pruning is planned but not implemented.
- **A managed cloud PostgreSQL** (Neon, Supabase, Azure). Power BI Service could then refresh without an on-premises gateway, and the laptop wouldn't need to stay awake.
- **async `httpx`** with one event loop per host instead of separate processes. Same throughput, simpler to operate, one log.
- **Type checking** (`mypy` or `pyright`) on the parsers, where most data-shape bugs live.

**Data and analysis**
- **True launch prices from IsThereAnyDeal.** The current launch price is the list price at first observation, which is accurate for recent games but not for older ones whose price was cut.
- **Survival-style analysis for Q1:** time to 1,000 reviews by early-sentiment band, instead of a fixed day 31–365 window.
- **Better genre modelling:** cluster tags into about 15 genre families (for example "Roguelike/Roguelite/Action Roguelike" → Roguelike) instead of the hand-written meta-tag list.
- **Control for publisher, studio size and marketing** with proxies such as follower counts and wishlist rank, which would make the causal story stronger.

---

## 7. Current status and open work

**Done (13 Nov 2026):**
- [x] API research and findings document, plus sanitized fixtures
- [x] Schema (raw, ops, core, marts), with three Alembic migrations
- [x] HTTP layer, clients, parsers, loaders, queue, workers, CLI
- [x] Full catalog: 65,471 indie games, 27,051 in scope, 22,602 old enough for Q1
- [x] Marts, 10 data-quality checks, statistics module, daily pipeline, scheduler scripts
- [x] 50 passing tests, `ruff` clean
- [x] Market snapshot page from catalog data: `reports/market_snapshot.html`
- [x] Privacy scrub of raw review payloads

**In progress:** the per-game collection, started 13 Nov at about 03:55. Three background workers run, one per API host:

| Worker | Sources, in order | Est. finish |
|---|---|---|
| store | appdetails → histogram | ~23 h |
| webapi | reviews → players → review_sample → news | ~31 h (finishes last) |
| steamspy | steamspy | ~7 h |

Check progress with `steam-intel status`. The PC must stay awake. If it sleeps, follow "Resume after sleep or reboot" in §8.

**Next, in order:**
1. **When collection finishes:** run `steam-intel refresh-marts`, then `steam-intel analyze`, then `steam-intel dq`.
2. **Answer Q1 and Q4:**
   - Q1: Spearman ρ overall and by early-volume band, plus the OLS coefficient.
   - Q4: tag-level playtime against owners.
   - Add both to `reports/market_snapshot.html`.
3. **Move to a permanent PostgreSQL** (Docker Desktop or a local install). Dump the dev database with the `pg_dump` binary bundled with pgserver, restore it, and update `DATABASE_URL`. Power BI needs a stable host and port.
4. **Build the Power BI report** with the 8 pages in `powerbi/README.md`. Save it as `.pbip`.
5. **Register the daily task:** `scripts/register_daily_task.ps1`.
6. **Commit in the 10-commit sequence, push, and open a PR** from `dev` to `main`.

**Backlog (planned, not built):**
- IsThereAnyDeal price backfill. A config key exists, but there's no client yet.
- Wire `GetAppList` deltas into the daily run. The client method exists, but the daily run currently relies on the full Query crawl.
- Raw pruning: keep only the latest payload per app and source after 30 days.
- A marts view that cross-checks playtime-at-review from the review sample against SteamSpy medians.
- Update-cadence analysis from `news_events`.
- Release-timing and localization analysis. The columns exist in `dim_game`, but no statistics are computed yet.
- Investigate the 2026 launch surge and whether Query hides content filtered by Steam's default store preferences.
- CI, type checking, dbt (see §6).

---

## 8. Operations cheat sheet

```powershell
# Daily-use commands
.\.venv\Scripts\steam-intel status                 # queue progress, row counts, recent runs
.\.venv\Scripts\steam-intel work reviews --minutes 30
.\.venv\Scripts\steam-intel refresh-marts; .\.venv\Scripts\steam-intel analyze; .\.venv\Scripts\steam-intel dq
.\.venv\Scripts\python -m pytest

# Resume after sleep or reboot
.\.venv\Scripts\steam-intel dev-db                 # restarts embedded Postgres, prints URL (update .env if the port changed)
.\scripts\start_bootstrap_workers.ps1              # workers resume from the queue

# See or stop workers
Get-CimInstance Win32_Process -Filter "Name='steam-intel.exe'" | Select ProcessId, CommandLine
Stop-Process -Id <pid>                             # claimed items go back to pending on the next run

# Move the dev database to a permanent server (example)
$bin = ".venv\Lib\site-packages\pgserver\pginstall\bin"
& "$bin\pg_dump.exe" -h 127.0.0.1 -p <devport> -U postgres -Fc -f steam_intel.dump postgres
& "$bin\pg_restore.exe" -h localhost -p 5432 -U steam -d steam_intel --no-owner steam_intel.dump
```

**Useful SQL:**
```sql
SELECT * FROM ops.ingestion_runs ORDER BY run_id DESC LIMIT 10;          -- what ran, how it went
SELECT source, status, count(*) FROM ops.ingestion_queue GROUP BY 1,2;   -- queue state
SELECT * FROM ops.ingestion_queue WHERE status = 'failed' LIMIT 20;      -- errors with messages
SELECT * FROM mart.dq_latest;                                             -- latest data-quality results
SELECT * FROM mart.analysis_results ORDER BY 1,2,3;                       -- statistics
```
