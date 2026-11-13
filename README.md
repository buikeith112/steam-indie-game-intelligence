# steam-indie-game-intelligence

A data pipeline and PostgreSQL dataset of indie games on Steam, built to feed a Power BI dashboard. Data comes from Steam's public APIs and SteamSpy.

## Business questions
- Does early review sentiment relate to later popularity in indie games?
- Which genres have the highest proportion of positively reviewed games in the indie market?
- What range of pricing is best for popular indie games?
- Which genres of indie games have higher total player counts and median playtimes, which could be correlated to player retention and high replayability?

**Additional questions the dataset supports:**
- Supply versus demand by tag
- Early Access versus direct launch
- Self-published versus publisher-backed
- Release timing
- Localization
- Steam Deck support
- Discount behaviour
- Sentiment drift
- Market concentration
- Hours per dollar
- Demos

## Architecture
```
Steam APIs ──► HttpClient (per-host token buckets, retry/backoff, 403 circuit breaker)
   │
   ├─ IStoreQueryService/Query (Indie tag)      ─┐
   ├─ IStoreBrowseService/GetItems (200/batch)   │  catalog: ~400 calls for all ~65k indie games
   ├─ store appdetails                           │
   ├─ IUserReviewsService/GetAppReviews          │  per-game queues (resumable)
   ├─ appreviewhistogram, GetNumberOfCurrentPlayers, GetNewsForApp
   └─ SteamSpy appdetails                       ─┘
          │
          ▼
PostgreSQL:  raw (JSONB payloads) ─► core (normalized + time series) ─► mart (star schema) ─► Power BI
                       ops (runs, queue, data-quality results, checkpoints)
```
- API findings and endpoint choices: [docs/api_findings.md](docs/api_findings.md)
- Tables: [docs/data_dictionary.md](docs/data_dictionary.md)
- Metric definitions and biases: [docs/methodology.md](docs/methodology.md)
- How it was built, the decision log, lessons learned and open work: [docs/project_guide.md](docs/project_guide.md)
- Findings so far: [reports/market_snapshot.html](reports/market_snapshot.html)
- Technical appendix (schema, pipeline, SQL, refresh, validation): [reports/technical_appendix.html](reports/technical_appendix.html)

## Setup
Requires Python 3.12+ and PostgreSQL 16. Use either option A or option B for the database.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -e ".[dev,analysis]"
copy .env.example .env        # then edit DATABASE_URL (and optionally STEAM_API_KEY)
```

**Option A: Docker**
```powershell
docker compose up -d
```

**Option B: local PostgreSQL install**
Create database `steam_intel`, then point `DATABASE_URL` at it.

**Development without either:** `steam-intel dev-db` starts an embedded PostgreSQL 16 from the `pgserver` package and prints a `DATABASE_URL`. The port can change between restarts, so it's not suited to Power BI.

Then apply the schema:
```powershell
.\.venv\Scripts\steam-intel db-upgrade
```

No API key is needed for the core pipeline. `STEAM_API_KEY` only enables `IStoreService/GetAppList` catalog deltas.

## Running
| Command | What it does |
|---|---|
| `steam-intel catalog` | Crawls the full indie catalog in a few minutes. Writes games, tags, companies, prices and the daily catalog snapshot. |
| `steam-intel enqueue` | Queues the analytical scope (launched 2018 or later, ≥10 reviews) for every per-game source |
| `steam-intel work <source> [--minutes N]` | Drains one queue. Sources: `appdetails`, `reviews`, `steamspy`, `players`, `histogram`, `review_sample`, `news`. Safe to stop and re-run because it resumes from the queue. |
| `steam-intel bootstrap [--limit N]` | catalog, then enqueue, then all sources in order. Use `--limit 50` for a smoke test. |
| `steam-intel daily` | Incremental run: catalog, re-queue due work, drain queues, refresh marts, compute stats, run DQ checks |
| `steam-intel refresh-marts` / `analyze` / `dq` / `status` | Refreshes materialized views, computes statistics, runs data-quality checks, or shows queue progress and row counts |

**Scheduling:** run `.\scripts\register_daily_task.ps1` to create a Windows scheduled task at 03:00 that runs `scripts\run_daily.ps1`.

**Bootstrap time** (measured scope: 65,471 indie games, 27,051 in analytical scope):
- Catalog: about 6 min.
- Per-game sources: about 30 h wall time if you run one worker per API host in parallel. Use three terminals:
  ```powershell
  steam-intel enqueue
  steam-intel work appdetails; steam-intel work histogram                                         # store host
  steam-intel work reviews; steam-intel work players; steam-intel work review_sample; steam-intel work news   # webapi host
  steam-intel work steamspy                                                                      # steamspy host
  ```
- Every worker can be stopped and restarted. It resumes from `ops.ingestion_queue`.

## Tests
```powershell
.\.venv\Scripts\python -m pytest
```
- Parser and HTTP tests run against recorded API fixtures in `tests/fixtures/`, with reviewer IDs removed.
- Database tests start an embedded PostgreSQL through `pgserver`, or use `STEAM_INTEL_TEST_DB_URL` if set.

## Project status
- [x] Phase 0–1: setup and live API exploration
- [x] Phase 2: schema (raw / ops / core, Alembic migrations)
- [x] Phase 3: HTTP layer, clients, parsers, queue (49 tests)
- [x] Phase 4: ingestion jobs. Live smoke test passed (60 games, 0 failures). Full catalog crawled: 65,471 games, 27,051 in scope.
- [ ] Phase 4: full per-game bootstrap (long-running)
- [x] Phase 5–6: transforms, DQ checks, mart views, statistics
- [x] Phase 7: daily pipeline and scheduler scripts
- [ ] Phase 8: Power BI report (`powerbi/`)
- [ ] Phase 9: findings write-up
