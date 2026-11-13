"""Daily incremental pipeline (plan section 5, "Daily run").

Budget: the catalog crawl (~400 calls) refreshes prices, review counts and new/removed games for
the whole indie catalog. Per-game sources are refreshed on a cadence that keeps the run within a
few hours at the configured request rates:

  appdetails     new games + ~1/30 of scope per day (metadata drift: Deck status, languages, demos)
  reviews        games launched in the last 2 years daily; others weekly (app_id % 7); any game
                 whose Q1 window has just closed
  players        games with >= 100 reviews daily; the rest weekly
  steamspy       1/7 of scope per day (SteamSpy itself only refreshes daily)
  histogram      games with >= 50 reviews, 1/7 per day
  review_sample  new games only
  news           1/30 of scope per day
"""
from __future__ import annotations

import logging

from sqlalchemy import Engine, text

from . import db, ingest, quality
from .clients import SteamClient
from .config import Settings

log = logging.getLogger(__name__)

SCOPE = """
    FROM core.games g
    WHERE g.delisted_at IS NULL
      AND g.launch_date >= make_date(:y, 1, 1)
      AND g.store_review_count >= :min_reviews
"""

REFRESH_RULES: dict[str, str] = {
    "appdetails": "AND (g.details_fetched_at IS NULL OR g.app_id % 30 = :dom % 30)",
    "reviews": """AND (g.launch_date >= current_date - 730
                       OR g.app_id % 7 = :dow
                       OR EXISTS (SELECT 1 FROM core.review_windows w
                                  WHERE w.app_id = g.app_id AND NOT w.is_complete AND w.window_end <= now())
                       OR (g.launch_date + 30 <= current_date AND NOT EXISTS
                           (SELECT 1 FROM core.review_windows w WHERE w.app_id = g.app_id AND w.window_code = 'd0_30')))""",
    "players": "AND (g.store_review_count >= 100 OR g.app_id % 7 = :dow)",
    "steamspy": "AND g.app_id % 7 = :dow",
    "histogram": "AND g.store_review_count >= 50 AND g.app_id % 7 = :dow",
    "review_sample": "AND NOT EXISTS (SELECT 1 FROM core.reviews r WHERE r.app_id = g.app_id)",
    "news": "AND g.app_id % 30 = :dom % 30",
}


def requeue_due(engine: Engine, settings: Settings) -> dict[str, int]:
    today = db.utcnow()
    params = {"y": settings.min_release_year, "min_reviews": settings.min_reviews,
              "dow": today.isoweekday() % 7, "dom": today.day}
    out = {}
    with engine.begin() as c:
        for source, rule in REFRESH_RULES.items():
            ids = c.execute(text(f"SELECT g.app_id {SCOPE} {rule}"), params).scalars().all()
            out[source] = db.enqueue(c, source, ids, priority=ingest.SOURCE_PRIORITY[source], requeue_done=True)
    return out


def run_daily(engine: Engine, client: SteamClient, settings: Settings, minutes_per_source: float = 60) -> dict:
    with engine.begin() as c:
        run_id = db.start_run(c, "daily")
    summary: dict = {}
    status = "succeeded"
    try:
        summary["catalog"] = ingest.run_catalog(engine, client)
        summary["requeued"] = requeue_due(engine, settings)
        ctx = ingest.Ctx(client, settings)
        for source in ingest.SOURCES:
            summary[source] = ingest.run_source(engine, ctx, source, minutes=minutes_per_source)
            if summary[source]["failed"]:
                status = "partial"
        quality.refresh_marts(engine)
        try:
            from .analysis import run_all as run_analysis
            summary["analysis"] = run_analysis(engine)
        except ImportError:
            log.warning("analysis extras not installed (pip install -e .[analysis]); skipping stats")
        summary["dq_failed"] = [r["check"] for r in quality.run_checks(engine, run_id) if not r["passed"]]
    except Exception as exc:
        status = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        with engine.begin() as c:
            db.finish_run(c, run_id, status, 0, 0, summary)
    return summary
