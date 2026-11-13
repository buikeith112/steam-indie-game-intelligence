"""Ingestion jobs.

Flow (see plan section 5):
  catalog            Query(Indie tag) ids -> GetItems batches -> core.games/tags/companies/prices + catalog_snapshots
  enqueue_candidates analytical scope (launch >= MIN_RELEASE_YEAR, reviews >= MIN_REVIEWS) -> ops.ingestion_queue
  run_source(src)    drain one queue: appdetails | reviews | review_sample | histogram | steamspy | players | news

Every response is written to raw.api_responses before parsing, so `core` can be rebuilt from `raw`.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from datetime import time as dtime

from sqlalchemy import Connection, Engine, text

from . import db
from .clients import GETITEMS_MAX_BATCH, QUERY_MAX_PAGE, SteamClient
from .config import Settings
from .http import CircuitOpenError
from .transform import load
from .transform import parsers as p

log = logging.getLogger(__name__)

# Q1 review windows, in days relative to launch_date.
REVIEW_WINDOWS = [("d0_30", 0, 30), ("d31_365", 30, 365), ("d366_730", 365, 730)]

# Queue sources and the order bootstrap drains them in.
SOURCES = ["appdetails", "reviews", "steamspy", "players", "histogram", "review_sample", "news"]
SOURCE_PRIORITY = {s: (i + 1) * 10 for i, s in enumerate(SOURCES)}


class FetchError(RuntimeError):
    pass


@dataclass
class Ctx:
    client: SteamClient
    settings: Settings
    run_id: int | None = None


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _require_ok(res, what: str):
    if not res.ok:
        raise FetchError(f"{what}: HTTP {res.status}")


# =================================================================================
# Catalog
# =================================================================================
def sync_tag_dictionary(engine: Engine, client: SteamClient) -> int:
    res = client.tag_list()
    _require_ok(res, "tag_list")
    with engine.begin() as c:
        db.save_raw(c, "tag_list", res)
        return load.upsert_tag_dictionary(c, p.parse_tag_list(res.payload))


def fetch_catalog_ids(engine: Engine, client: SteamClient, run_id: int, limit: int | None = None) -> list[int]:
    ids: list[int] = []
    start, total = 0, None
    while total is None or start < total:
        res = client.store_query_ids(start, QUERY_MAX_PAGE)
        _require_ok(res, f"store_query start={start}")
        with engine.begin() as c:
            db.save_raw(c, "store_query", res, request_key=f"start={start}", run_id=run_id)
        page, total = p.parse_query_ids(res.payload)
        if not page:
            break
        ids.extend(page)
        start += len(page)
        log.info("catalog ids: %d / %s", len(ids), total)
        if limit and len(ids) >= limit:
            break
    # Keep order, drop duplicates (new releases landing mid-crawl can shift a page by a few rows).
    unique = list(dict.fromkeys(ids))
    if not limit and total and len(unique) < 0.995 * total:
        log.warning("catalog coverage: %d unique ids of %d reported; pagination may be unstable",
                    len(unique), total)
    return unique[:limit] if limit else unique


def load_getitems_batch(conn: Connection, payload: dict, observed_at: datetime) -> tuple[set[int], set[int]]:
    """Upsert one GetItems response. Returns (visible_ids, invisible_ids)."""
    visible, invisible = set(), set()
    for item in (payload.get("response") or {}).get("store_items", []):
        parsed = p.parse_getitems_item(item)
        app_id = int(item.get("appid") or item.get("id") or 0)
        if not parsed:
            invisible.add(app_id)
            continue
        g = parsed["game"]
        visible.add(g["app_id"])
        load.upsert_game(conn, {**g, "last_seen_in_catalog_at": observed_at})
        load.replace_game_tags(conn, g["app_id"], parsed["tags"])
        load.replace_companies(conn, g["app_id"], parsed["companies"])
        if parsed["price"]:
            load.record_price(conn, parsed["price"], observed_at, "getitems")
        conn.execute(text("""
            INSERT INTO core.catalog_snapshots (app_id, snapshot_date, review_count, percent_positive,
                                                final_price_cents, discount_pct)
            VALUES (:a, :d, :rc, :pp, :fp, :dp)
            ON CONFLICT (app_id, snapshot_date) DO UPDATE SET review_count = EXCLUDED.review_count,
                percent_positive = EXCLUDED.percent_positive, final_price_cents = EXCLUDED.final_price_cents,
                discount_pct = EXCLUDED.discount_pct"""),
                     {"a": g["app_id"], "d": observed_at.date(), "rc": g["store_review_count"],
                      "pp": g["store_percent_positive"],
                      "fp": (parsed["price"] or {}).get("final_cents"),
                      "dp": (parsed["price"] or {}).get("discount_pct")})
    return visible, invisible


def run_catalog(engine: Engine, client: SteamClient, limit: int | None = None) -> dict:
    with engine.begin() as c:
        run_id = db.start_run(c, "catalog")
    started = db.utcnow()
    try:
        n_tags = sync_tag_dictionary(engine, client)
        ids = fetch_catalog_ids(engine, client, run_id, limit)
        seen, hidden = set(), set()
        for batch in _chunks(ids, GETITEMS_MAX_BATCH):
            res = client.get_items(batch)
            _require_ok(res, "get_items")
            observed = db.utcnow()
            with engine.begin() as c:
                rid = db.save_raw(c, "getitems", res, request_key=f"{batch[0]}..{batch[-1]}", run_id=run_id)
                v, h = load_getitems_batch(c, res.payload, observed)
                db.mark_processed(c, [rid])
            seen |= v
            hidden |= h
            log.info("catalog details: %d / %d", len(seen) + len(hidden), len(ids))

        disappeared = 0
        if limit is None:
            # Full run: games we knew about that are no longer listed get re-checked via appdetails.
            with engine.begin() as c:
                gone = c.execute(text("""
                    SELECT app_id FROM core.games
                    WHERE delisted_at IS NULL AND (last_seen_in_catalog_at IS NULL OR last_seen_in_catalog_at < :t)
                """), {"t": started}).scalars().all()
                disappeared = db.enqueue(c, "appdetails", gone, priority=5, requeue_done=True)
                db.set_state(c, "catalog_last_full_run", {"at": started.isoformat(), "apps": len(seen)})
        stats = {"ids": len(ids), "visible": len(seen), "hidden": len(hidden),
                 "tags": n_tags, "recheck_disappeared": disappeared}
        with engine.begin() as c:
            db.finish_run(c, run_id, "succeeded", len(seen), len(hidden), stats)
        return stats
    except Exception as exc:
        with engine.begin() as c:
            db.finish_run(c, run_id, "failed", 0, 0, {"error": str(exc)[:500]})
        raise


# =================================================================================
# Candidate selection
# =================================================================================
def enqueue_candidates(engine: Engine, settings: Settings, sources: list[str] | None = None,
                       limit: int | None = None) -> dict[str, int]:
    """Analytical scope -> queue.

    Items get a deterministic pseudo-random priority (0-99) so workers process games in a
    shuffled order: any partially-finished crawl is a representative sample of the scope,
    not just the oldest app_ids (which would skew launch year) or the biggest hits.
    """
    with engine.begin() as c:
        ids = c.execute(text("""
            SELECT app_id FROM core.games
            WHERE delisted_at IS NULL
              AND launch_date >= make_date(:y, 1, 1)
              AND store_review_count >= :min_reviews
            ORDER BY store_review_count DESC
            LIMIT :lim"""), {"y": settings.min_release_year, "min_reviews": settings.min_reviews,
                             "lim": limit or 10_000_000}).scalars().all()
        shuffled = {a: shuffle_priority(a) for a in ids}
        out = {}
        for s in sources or SOURCES:
            out[s] = db.enqueue(c, s, ids, priority=shuffled)
    return out


def shuffle_priority(app_id: int) -> int:
    """Stable pseudo-random bucket 0-99 (Knuth multiplicative hash)."""
    return ((app_id * 2654435761) % 2**32) * 100 // 2**32


# =================================================================================
# Per-app handlers: (ctx, conn, app_id) -> 'done' | 'skip'
# =================================================================================
def handle_appdetails(ctx: Ctx, c: Connection, app_id: int) -> str:
    res = ctx.client.app_details(app_id)
    _require_ok(res, "appdetails")
    rid = db.save_raw(c, "appdetails", res, app_id=app_id, run_id=ctx.run_id)
    parsed = p.parse_appdetails(res.payload, app_id)
    now = db.utcnow()
    if parsed is None:
        # success=false: removed or region-locked. If it is also gone from the catalog, soft-delete.
        c.execute(text("""UPDATE core.games SET delisted_at = :t, details_fetched_at = :t, updated_at = now()
                          WHERE app_id = :a AND delisted_at IS NULL
                            AND (last_seen_in_catalog_at IS NULL OR last_seen_in_catalog_at < now() - interval '1 day')"""),
                  {"a": app_id, "t": now})
        db.mark_processed(c, [rid])
        return "skip"
    game = {**parsed["game"], "details_fetched_at": now}
    load.upsert_game(c, game)
    load.replace_genres(c, app_id, parsed["genres"])
    load.replace_categories(c, app_id, parsed["categories"])
    load.replace_languages(c, app_id, parsed["languages"])
    load.replace_companies(c, app_id, parsed["companies"])
    if parsed["price"]:
        load.record_price(c, parsed["price"], now, "appdetails")
    db.mark_processed(c, [rid])
    return "done"


def _launch_dt(c: Connection, app_id: int) -> datetime | None:
    d = c.execute(text("SELECT launch_date FROM core.games WHERE app_id = :a"), {"a": app_id}).scalar()
    return datetime.combine(d, dtime.min, tzinfo=UTC) if d else None


PURCHASE_STEAM, PURCHASE_ALL = 0, 1


def _purchase_type(c: Connection, app_id: int) -> int:
    """Which review population matches the store page. Paid games: Steam purchases only (0).
    Free games have no purchases, so 0 returns almost nothing (Splitgate: 165 vs 110,314);
    the store page and GetItems count all reviews (1) for them."""
    is_free = c.execute(text("SELECT is_free FROM core.games WHERE app_id = :a"), {"a": app_id}).scalar()
    return PURCHASE_ALL if is_free else PURCHASE_STEAM


def _scrubbed(ctx: Ctx, res):
    """Hash SteamIDs (and drop review text unless enabled) before anything is stored, raw included."""
    return replace(res, payload=p.scrub_review_payload(res.payload, ctx.settings.store_review_text))


def handle_reviews(ctx: Ctx, c: Connection, app_id: int) -> str:
    """Lifetime summary snapshot + Q1 windows (one call each; only windows that have started)."""
    pt = _purchase_type(c, app_id)
    res = ctx.client.app_reviews(app_id, filter=1, num_per_page=1, purchase_type=pt)
    _require_ok(res, "reviews summary")
    res = _scrubbed(ctx, res)
    rid = db.save_raw(c, "reviews_summary", res, app_id=app_id, run_id=ctx.run_id)
    summary = p.parse_review_summary(res.payload)
    if summary:
        load.upsert_review_snapshot(c, app_id, db.utcnow().date(), summary)
    db.mark_processed(c, [rid])

    launch = _launch_dt(c, app_id)
    if not launch:
        return "done"
    now = db.utcnow()
    done_windows = set(c.execute(text("""SELECT window_code FROM core.review_windows
                                         WHERE app_id = :a AND is_complete"""), {"a": app_id}).scalars())
    for code, d0, d1 in REVIEW_WINDOWS:
        start, end = launch + timedelta(days=d0), launch + timedelta(days=d1)
        if start > now or code in done_windows:
            continue
        res = ctx.client.app_reviews(app_id, filter=1, num_per_page=1, purchase_type=pt,
                                     date_range=(int(start.timestamp()), int(end.timestamp()) - 1))
        _require_ok(res, f"reviews {code}")
        res = _scrubbed(ctx, res)
        rid = db.save_raw(c, "reviews_window", res, app_id=app_id, request_key=code, run_id=ctx.run_id)
        s = p.parse_review_summary(res.payload) or {"total_positive": 0, "total_negative": 0}
        load.upsert_review_window(c, {"app_id": app_id, "window_code": code, "window_start": start,
                                      "window_end": end, "positive": s["total_positive"],
                                      "negative": s["total_negative"], "is_complete": end <= now})
        db.mark_processed(c, [rid])
    return "done"


def handle_review_sample(ctx: Ctx, c: Connection, app_id: int) -> str:
    n = min(100, ctx.settings.review_sample_per_type)
    pt = _purchase_type(c, app_id)
    for sample_type, filt, extra in (("helpful", 0, {"day_range": 0}), ("recent", 1, {})):
        res = ctx.client.app_reviews(app_id, filter=filt, num_per_page=n, purchase_type=pt, **extra)
        _require_ok(res, f"review sample {sample_type}")
        res = _scrubbed(ctx, res)
        rid = db.save_raw(c, "reviews_sample", res, app_id=app_id, request_key=sample_type, run_id=ctx.run_id)
        rows, _ = p.parse_reviews(res.payload, app_id, sample_type, keep_text=ctx.settings.store_review_text)
        load.upsert_reviews(c, rows)
        db.mark_processed(c, [rid])
    return "done"


def handle_histogram(ctx: Ctx, c: Connection, app_id: int) -> str:
    res = ctx.client.review_histogram(app_id)
    _require_ok(res, "histogram")
    rid = db.save_raw(c, "histogram", res, app_id=app_id, run_id=ctx.run_id)
    load.replace_histogram(c, app_id, p.parse_histogram(res.payload, app_id))
    db.mark_processed(c, [rid])
    return "done"


def handle_steamspy(ctx: Ctx, c: Connection, app_id: int) -> str:
    res = ctx.client.steamspy_appdetails(app_id)
    _require_ok(res, "steamspy")
    rid = db.save_raw(c, "steamspy", res, app_id=app_id, run_id=ctx.run_id)
    row = p.parse_steamspy(res.payload)
    db.mark_processed(c, [rid])
    if not row:
        return "skip"
    load.upsert_engagement(c, row, db.utcnow().date())
    return "done"


def handle_players(ctx: Ctx, c: Connection, app_id: int) -> str:
    res = ctx.client.current_players(app_id)
    if res.status == 404:            # no stats for this app; not an error worth retrying
        return "skip"
    _require_ok(res, "players")
    count = p.parse_player_count(res.payload)
    if count is None:
        return "skip"
    load.insert_player_count(c, app_id, db.utcnow(), count)   # tiny payload: not stored in raw
    return "done"


def handle_news(ctx: Ctx, c: Connection, app_id: int) -> str:
    res = ctx.client.news(app_id)
    _require_ok(res, "news")
    load.upsert_news(c, p.parse_news(res.payload, app_id))
    return "done"


HANDLERS: dict[str, Callable[[Ctx, Connection, int], str]] = {
    "appdetails": handle_appdetails,
    "reviews": handle_reviews,
    "review_sample": handle_review_sample,
    "histogram": handle_histogram,
    "steamspy": handle_steamspy,
    "players": handle_players,
    "news": handle_news,
}


# =================================================================================
# Queue worker
# =================================================================================
def run_source(engine: Engine, ctx: Ctx, source: str, limit: int | None = None,
               minutes: float | None = None, batch: int = 25) -> dict:
    handler = HANDLERS[source]
    with engine.begin() as c:
        db.reset_stale(c, source)
        ctx.run_id = db.start_run(c, f"work:{source}")
    deadline = time.monotonic() + minutes * 60 if minutes else None
    ok = failed = skipped = 0
    status = "succeeded"
    stop = False
    while not stop:
        with engine.begin() as c:
            ids = db.claim(c, source, batch)
        if not ids:
            break
        for i, app_id in enumerate(ids):
            if (limit and ok + failed + skipped >= limit) or (deadline and time.monotonic() > deadline):
                with engine.begin() as c:
                    db.release(c, source, ids[i:])
                stop, status = True, "partial"
                break
            try:
                with engine.begin() as c:
                    result = handler(ctx, c, app_id)
                    db.mark_done(c, source, app_id, "skip" if result == "skip" else "done")
                skipped += result == "skip"
                ok += result != "skip"
            except CircuitOpenError as exc:
                log.error("circuit open for %s: %s; stopping", source, exc)
                with engine.begin() as c:
                    db.release(c, source, ids[i:])
                stop, status = True, "partial"
                break
            except Exception as exc:  # noqa: BLE001 - record and continue with the next app
                log.warning("%s app %s failed: %s", source, app_id, exc)
                with engine.begin() as c:
                    db.mark_failed(c, source, app_id, f"{type(exc).__name__}: {exc}")
                failed += 1
        log.info("%s: ok=%d skip=%d failed=%d", source, ok, skipped, failed)
    stats = {"ok": ok, "skipped": skipped, "failed": failed}
    with engine.begin() as c:
        db.finish_run(c, ctx.run_id, status, ok + skipped, failed, stats)
    return stats
