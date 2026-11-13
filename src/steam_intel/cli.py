"""Command line entry point: `steam-intel --help`."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path

import typer

from . import db, ingest
from .clients import SteamClient
from .config import PROJECT_ROOT, get_settings
from .http import HttpClient

app = typer.Typer(add_completion=False, help="Steam indie game intelligence pipeline")


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v")):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


@contextmanager
def _client():
    settings = get_settings()
    with HttpClient(settings) as http:
        yield SteamClient(http, settings), settings


@app.command("db-upgrade")
def db_upgrade():
    """Apply all migrations (alembic upgrade head)."""
    db.upgrade_schema()
    typer.echo("schema up to date")


@app.command()
def catalog(limit: int | None = typer.Option(None, help="Only the first N apps (testing)")):
    """Crawl the indie catalog: Query ids -> GetItems -> core.games/tags/prices/catalog_snapshots."""
    with _client() as (client, _):
        stats = ingest.run_catalog(db.get_engine(), client, limit=limit)
    typer.echo(json.dumps(stats, indent=1))


@app.command("enqueue")
def enqueue(sources: list[str] = typer.Option(None, "--source", "-s", help="Default: all sources"),
            limit: int | None = typer.Option(None, help="Only the top-N games by review count")):
    """Queue the analytical scope (launch >= MIN_RELEASE_YEAR and reviews >= MIN_REVIEWS)."""
    out = ingest.enqueue_candidates(db.get_engine(), get_settings(), sources or None, limit)
    typer.echo(json.dumps(out, indent=1))


@app.command()
def work(source: str = typer.Argument(..., help=f"One of: {', '.join(ingest.SOURCES)}"),
         limit: int | None = typer.Option(None, help="Stop after N apps"),
         minutes: float | None = typer.Option(None, help="Stop after N minutes")):
    """Drain one work queue. Safe to interrupt and re-run: it resumes from ops.ingestion_queue."""
    if source not in ingest.HANDLERS:
        raise typer.BadParameter(f"unknown source {source}")
    with _client() as (client, settings):
        stats = ingest.run_source(db.get_engine(), ingest.Ctx(client, settings), source, limit, minutes)
    typer.echo(json.dumps(stats))


@app.command()
def bootstrap(limit: int | None = typer.Option(None, help="Cap catalog size and per-source work (testing)"),
              skip_catalog: bool = typer.Option(False, help="Reuse the existing catalog")):
    """One-time full load: catalog -> enqueue -> drain every source in priority order."""
    engine = db.get_engine()
    with _client() as (client, settings):
        if not skip_catalog:
            typer.echo(json.dumps(ingest.run_catalog(engine, client, limit=limit)))
        typer.echo(json.dumps(ingest.enqueue_candidates(engine, settings, limit=limit)))
        ctx = ingest.Ctx(client, settings)
        for source in ingest.SOURCES:
            typer.echo(f"{source}: {json.dumps(ingest.run_source(engine, ctx, source, limit=limit))}")


@app.command()
def daily(minutes_per_source: float = typer.Option(60, help="Time budget per queue source")):
    """Daily incremental run: catalog -> requeue due work -> drain queues -> marts -> stats -> DQ."""
    from .daily import run_daily
    with _client() as (client, settings):
        summary = run_daily(db.get_engine(), client, settings, minutes_per_source)
    typer.echo(json.dumps(summary, indent=1, default=str))


@app.command()
def analyze():
    """Recompute statistics (Q1 correlation/regression etc.) into mart.analysis_results."""
    from .analysis import run_all
    typer.echo(json.dumps(run_all(db.get_engine()), indent=1, default=str))


@app.command("refresh-marts")
def refresh_marts():
    """Refresh mart materialized views (dim_game, fact_game_metrics, summaries)."""
    from .quality import refresh_marts as _refresh
    _refresh(db.get_engine())
    typer.echo("marts refreshed")


@app.command()
def dq():
    """Run data-quality checks; results go to ops.data_quality_results."""
    from .quality import run_checks
    for r in run_checks(db.get_engine()):
        typer.echo(f"  {'PASS' if r['passed'] else 'FAIL'}  {r['check']:<34} value={r['value']} threshold={r['threshold']}")


@app.command()
def status():
    """Queue progress and recent runs."""
    from sqlalchemy import text
    with db.get_engine().begin() as c:
        typer.echo("queue (source, status, count):")
        for row in db.queue_stats(c):
            typer.echo(f"  {row[0]:<14} {row[1]:<12} {row[2]:>8}")
        typer.echo("core row counts:")
        for t in ("games", "game_tags", "price_history", "catalog_snapshots", "review_windows",
                  "review_snapshots", "reviews", "review_histogram", "engagement_snapshots",
                  "player_count_snapshots"):
            n = c.execute(text(f"SELECT count(*) FROM core.{t}")).scalar()
            typer.echo(f"  {t:<24} {n:>10}")
        typer.echo("last runs:")
        for r in c.execute(text("""SELECT run_id, job, status, items_ok, items_failed, started_at, finished_at
                                   FROM ops.ingestion_runs ORDER BY run_id DESC LIMIT 8""")):
            typer.echo(f"  #{r.run_id} {r.job:<20} {r.status:<10} ok={r.items_ok} failed={r.items_failed} "
                       f"{r.started_at:%Y-%m-%d %H:%M}")


@app.command("dev-db")
def dev_db(data_dir: Path = typer.Option(PROJECT_ROOT / "data" / "pgdata")):
    """Start an embedded PostgreSQL 16 (pgserver) for development and print its DATABASE_URL.

    Use this until a permanent PostgreSQL / Docker install is available. Needs `pip install pgserver`.
    """
    import pgserver  # optional dependency
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    srv =pgserver.get_server(str(data_dir), cleanup_mode=None)
    url = srv.get_uri().replace("postgresql://", "postgresql+psycopg://")
    typer.echo(url)


if __name__ == "__main__":
    app()
