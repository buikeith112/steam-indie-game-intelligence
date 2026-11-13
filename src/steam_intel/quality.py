"""Data-quality checks and mart refresh. Results land in ops.data_quality_results
and surface on the dashboard's Methodology & Data Quality page via mart.dq_latest."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, text

MATERIALIZED_VIEWS = ["mart.dim_game", "mart.fact_game_metrics", "mart.tag_summary", "mart.price_bucket_summary"]


def refresh_marts(engine: Engine) -> None:
    """Refresh in dependency order. CONCURRENTLY keeps Power BI readable during refresh
    (needs a unique index, which dim_game and fact_game_metrics have)."""
    with engine.begin() as c:
        for mv in MATERIALIZED_VIEWS:
            populated = c.execute(text("SELECT ispopulated FROM pg_matviews WHERE schemaname || '.' || matviewname = :m"),
                                  {"m": mv}).scalar()
            concurrently = "CONCURRENTLY " if populated and mv in ("mart.dim_game", "mart.fact_game_metrics") else ""
            c.execute(text(f"REFRESH MATERIALIZED VIEW {concurrently}{mv}"))


@dataclass
class Check:
    name: str
    sql: str            # returns one numeric value
    op: str             # 'le' -> value <= threshold passes; 'ge' -> value >= threshold passes
    threshold: float
    details: str


CHECKS = [
    Check("games_without_name", "SELECT count(*) FROM core.games WHERE name = ''", "le", 0,
          "Every game must have a name"),
    Check("review_window_sum_mismatch",
          "SELECT count(*) FROM core.review_snapshots WHERE total_positive + total_negative <> total_reviews",
          "le", 0, "positive + negative must equal total"),
    Check("positive_ratio_out_of_range",
          "SELECT count(*) FROM core.games WHERE store_percent_positive NOT BETWEEN 0 AND 100", "le", 0,
          "percent positive within 0-100"),
    Check("launch_in_future_but_released",
          "SELECT count(*) FROM core.games WHERE launch_date > current_date + 1 AND NOT COALESCE(coming_soon, FALSE)",
          "le", 0, "released games cannot launch in the future"),
    Check("price_open_rows_per_app",
          "SELECT count(*) FROM (SELECT app_id FROM core.price_history WHERE valid_to IS NULL "
          "GROUP BY app_id HAVING count(*) > 1) x", "le", 0, "SCD-2 must have <= 1 open row per app"),
    Check("negative_prices", "SELECT count(*) FROM core.price_history WHERE final_cents < 0 OR initial_cents < 0",
          "le", 0, "prices are non-negative"),
    Check("scope_details_coverage_pct",
          "SELECT COALESCE(100 * avg((details_fetched_at IS NOT NULL)::int), 0) FROM core.games "
          "WHERE launch_date >= DATE '2018-01-01' AND store_review_count >= 10 AND delisted_at IS NULL",
          "ge", 95, "share of in-scope games with appdetails"),
    Check("scope_window_coverage_pct",
          "SELECT COALESCE(100 * avg((EXISTS (SELECT 1 FROM core.review_windows w WHERE w.app_id = g.app_id))::int), 0) "
          "FROM core.games g WHERE launch_date >= DATE '2018-01-01' AND store_review_count >= 10 AND delisted_at IS NULL",
          "ge", 95, "share of in-scope games with Q1 review windows"),
    Check("catalog_freshness_hours",
          "SELECT COALESCE(EXTRACT(EPOCH FROM now() - max(last_seen_in_catalog_at)) / 3600, 9999) FROM core.games",
          "le", 36, "catalog refreshed within 36h"),
    Check("failed_queue_items",
          "SELECT count(*) FROM ops.ingestion_queue WHERE status = 'failed' AND attempts >= 5", "le", 100,
          "permanently failed work items"),
]


def run_checks(engine: Engine, run_id: int | None = None) -> list[dict]:
    results = []
    with engine.begin() as c:
        for chk in CHECKS:
            value = float(c.execute(text(chk.sql)).scalar() or 0)
            passed = value <= chk.threshold if chk.op == "le" else value >= chk.threshold
            c.execute(text("""INSERT INTO ops.data_quality_results
                              (run_id, check_name, passed, observed_value, threshold, details)
                              VALUES (:r, :n, :p, :v, :t, :d)"""),
                      {"r": run_id, "n": chk.name, "p": passed, "v": value, "t": chk.threshold, "d": chk.details})
            results.append({"check": chk.name, "passed": passed, "value": round(value, 2), "threshold": chk.threshold})
    return results
