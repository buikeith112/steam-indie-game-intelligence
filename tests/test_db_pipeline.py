"""Integration tests against a real PostgreSQL.

Uses STEAM_INTEL_TEST_DB_URL if set, otherwise an embedded PostgreSQL via `pgserver` (skipped if
neither is available). HTTP is mocked with fixtures; no network access.
"""
import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from steam_intel import db, ingest
from steam_intel.clients import SteamClient
from steam_intel.config import Settings
from steam_intel.http import HttpClient
from steam_intel.transform import load

pytestmark = pytest.mark.db


@pytest.fixture(scope="session")
def pg_url(tmp_path_factory):
    url = os.environ.get("STEAM_INTEL_TEST_DB_URL")
    if url:
        return url
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    base = srv.get_uri().replace("postgresql://", "postgresql+psycopg://")
    dbname = f"t_{uuid.uuid4().hex[:8]}"
    eng = db.get_engine(base)
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text(f"CREATE DATABASE {dbname}"))
    return base.rsplit("/", 1)[0] + f"/{dbname}"


@pytest.fixture(scope="session")
def engine(pg_url):
    db.upgrade_schema(pg_url)
    return db.get_engine(pg_url)


@pytest.fixture
def clean(engine):
    with engine.begin() as c:
        c.execute(text("""TRUNCATE core.games, core.companies, core.tags, core.genres, core.categories,
                          ops.ingestion_queue, ops.ingestion_runs, raw.api_responses, ops.pipeline_state CASCADE"""))
    return engine


def _mock_client(load_fixture, routes):
    """routes: list of (predicate(request) -> bool, response_factory(request))."""
    def handler(request: httpx.Request):
        for pred, resp in routes:
            if pred(request):
                return resp(request)
        return httpx.Response(404, json={})
    settings = Settings(store_rps=1000, webapi_rps=1000, steamspy_rps=1000, min_release_year=2018, min_reviews=10)
    http = HttpClient(settings, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    return SteamClient(http, settings), settings


def test_price_history_scd2(clean):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    with clean.begin() as c:
        load.upsert_game(c, {"app_id": 1, "name": "X"})
        base = {"app_id": 1, "currency": "USD", "initial_cents": 1999, "final_cents": 1999, "discount_pct": 0}
        assert load.record_price(c, base, t0, "test")
        assert not load.record_price(c, base, t0 + timedelta(days=1), "test")           # unchanged
        assert load.record_price(c, {**base, "final_cents": 999, "discount_pct": 50}, t0 + timedelta(days=2), "test")
        assert load.record_price(c, base, t0 + timedelta(days=9), "test")                # sale ends
        rows = c.execute(text("""SELECT final_cents, valid_to IS NULL FROM core.price_history
                                 WHERE app_id = 1 ORDER BY valid_from""")).all()
        assert [tuple(r) for r in rows] == [(1999, False), (999, False), (1999, True)]
        assert c.execute(text("SELECT launch_price_cents FROM core.games WHERE app_id=1")).scalar() == 1999


def test_upsert_game_does_not_null_out(clean):
    with clean.begin() as c:
        load.upsert_game(c, {"app_id": 2, "name": "Y", "metacritic_score": 80})
        load.upsert_game(c, {"app_id": 2, "name": "Y", "metacritic_score": None, "is_free": True})
        row = c.execute(text("SELECT metacritic_score, is_free FROM core.games WHERE app_id=2")).one()
        assert tuple(row) == (80, True)


def test_queue_claim_fail_retry(clean):
    with clean.begin() as c:
        db.enqueue(c, "appdetails", [10, 11, 12])
        assert db.enqueue(c, "appdetails", [10]) == 1          # duplicate ignored (DO NOTHING)
        claimed = db.claim(c, "appdetails", 2)
        assert claimed == [10, 11]
        db.mark_done(c, "appdetails", 10)
        db.mark_failed(c, "appdetails", 11, "boom")
        assert db.claim(c, "appdetails", 5) == [12]             # 11 not due for retry yet
        db.release(c, "appdetails", [12])
        stats = {(s, st): n for s, st, n in db.queue_stats(c)}
        assert stats[("appdetails", "done")] == 1 and stats[("appdetails", "failed")] == 1
        assert stats[("appdetails", "pending")] == 1


def test_catalog_and_workers_end_to_end(clean, load_fixture):
    getitems = load_fixture("getitems_balatro_hades_stardew.json")
    ids_page = {"response": {"metadata": {"total_matching_records": 3},
                             "ids": [{"appid": 2379780}, {"appid": 1145360}, {"appid": 413150}]}}

    def is_(path):
        return lambda r: path in r.url.path

    routes = [
        (is_("/tagdata/populartags"), lambda r: httpx.Response(200, json=load_fixture("tagdata.json"))),
        (is_("IStoreQueryService/Query"), lambda r: httpx.Response(200, json=ids_page)),
        (is_("IStoreBrowseService/GetItems"), lambda r: httpx.Response(200, json=getitems)),
        (is_("/api/appdetails"), lambda r: httpx.Response(200, json=load_fixture("appdetails_balatro.json"))
            if r.url.params["appids"] == "2379780" else httpx.Response(200, json={r.url.params["appids"]: {"success": False}})),
        (is_("GetAppReviews"), lambda r: httpx.Response(200, json=load_fixture(
            "gar_page5.json" if json.loads(r.url.params["input_json"])["num_per_page"] > 1 else "gar_d30_all.json"))),
        (is_("appreviewhistogram"), lambda r: httpx.Response(200, json=load_fixture("histogram.json"))),
        (lambda r: r.url.host == "steamspy.com", lambda r: httpx.Response(200, json=load_fixture("steamspy_appdetails.json"))),
        (is_("GetNumberOfCurrentPlayers"), lambda r: httpx.Response(200, json=load_fixture("ccu.json"))),
        (is_("GetNewsForApp"), lambda r: httpx.Response(200, json=load_fixture("news.json"))),
    ]
    client, settings = _mock_client(load_fixture, routes)

    stats = ingest.run_catalog(clean, client)
    assert stats["visible"] == 3 and stats["tags"] > 400

    with clean.begin() as c:
        g = c.execute(text("SELECT * FROM core.games WHERE app_id = 2379780")).mappings().one()
        assert g["launch_date"] == date(2024, 2, 20) and g["is_indie_tag"]
        assert c.execute(text("SELECT count(*) FROM core.game_tags WHERE app_id = 2379780")).scalar() == 20
        assert c.execute(text("SELECT count(*) FROM core.catalog_snapshots")).scalar() == 3

    queued = ingest.enqueue_candidates(clean, settings)
    assert queued["appdetails"] >= 2       # Hades (2020) and Balatro (2024); Stardew (2016) out of scope

    ctx = ingest.Ctx(client, settings)
    for src in ingest.SOURCES:
        ingest.run_source(clean, ctx, src)

    with clean.begin() as c:
        g = c.execute(text("SELECT * FROM core.games WHERE app_id = 2379780")).mappings().one()
        assert g["is_indie_genre"] is True and g["num_languages"] == 12 and g["details_fetched_at"]
        w = {r.window_code: r for r in c.execute(text(
            "SELECT * FROM core.review_windows WHERE app_id = 2379780"))}
        assert set(w) == {"d0_30", "d31_365", "d366_730"} and all(r.is_complete for r in w.values())
        assert c.execute(text("SELECT count(*) FROM core.reviews WHERE app_id = 2379780")).scalar() == 5
        assert c.execute(text("SELECT count(*) FROM core.review_histogram WHERE app_id = 2379780")).scalar() == 64
        assert c.execute(text("SELECT owners_high FROM core.engagement_snapshots WHERE app_id = 2379780")).scalar() == 5_000_000
        assert c.execute(text("SELECT count(*) FROM core.player_count_snapshots")).scalar() >= 1
        # Hades appdetails mocked as success=false while still in catalog -> skipped, not delisted
        assert c.execute(text("SELECT delisted_at FROM core.games WHERE app_id = 1145360")).scalar() is None
        unprocessed = c.execute(text("""SELECT count(*) FROM raw.api_responses
                                        WHERE processed_at IS NULL AND source NOT IN ('store_query','tag_list')""")).scalar()
        assert unprocessed == 0
        assert c.execute(text("SELECT count(*) FROM ops.ingestion_queue WHERE status IN ('pending','in_progress')")).scalar() == 0

    # marts
    from steam_intel import quality
    quality.refresh_marts(clean)
    quality.refresh_marts(clean)          # second refresh exercises REFRESH ... CONCURRENTLY
    with clean.begin() as c:
        g = c.execute(text("SELECT * FROM mart.dim_game WHERE app_id = 2379780")).mappings().one()
        assert g["in_analytical_scope"] and g["price_bucket_id"] == 3 and g["primary_tag"] != "Indie"
        assert g["steam_deck_status"] == "Verified"
        f = c.execute(text("SELECT * FROM mart.fact_game_metrics WHERE app_id = 2379780")).mappings().one()
        assert 0.95 < f["positive_ratio"] <= 1 and f["wilson_lower_bound"] < f["positive_ratio"]
        assert f["early_reviews_30d"] == 21297 and f["early_sentiment_band"] == "90%+"
        assert float(f["median_playtime_h"]) == pytest.approx(1208 / 60)
        assert f["success_tier_id"] == 3 and f["is_hit_1k"]
        assert c.execute(text("SELECT count(*) FROM mart.tag_summary")).scalar() > 0
        assert c.execute(text("SELECT count(*) FROM mart.bridge_game_tag WHERE app_id = 2379780")).scalar() == 5
        assert c.execute(text("SELECT games_in_scope FROM mart.data_coverage")).scalar() >= 2
    results = {r["check"]: r for r in quality.run_checks(clean)}
    assert results["review_window_sum_mismatch"]["passed"]
    assert results["price_open_rows_per_app"]["passed"]

    # Delisted games stay in the analytical scope (no survivorship bias in rates)
    with clean.begin() as c:
        c.execute(text("UPDATE core.games SET delisted_at = now() WHERE app_id = 2379780"))
    quality.refresh_marts(clean)
    with clean.begin() as c:
        g = c.execute(text("SELECT * FROM mart.dim_game WHERE app_id = 2379780")).mappings().one()
        assert g["is_delisted"] and g["in_analytical_scope"]


def test_review_purchase_type_matches_store_page(clean):
    """Free games have no Steam purchases, so their reviews must be counted with purchase_type=all."""
    with clean.begin() as c:
        load.upsert_game(c, {"app_id": 1, "name": "Paid", "is_free": False})
        load.upsert_game(c, {"app_id": 2, "name": "Free", "is_free": True})
        assert ingest._purchase_type(c, 1) == ingest.PURCHASE_STEAM
        assert ingest._purchase_type(c, 2) == ingest.PURCHASE_ALL


def test_wilson_and_steamdb_functions(engine):
    with engine.begin() as c:
        w_small, w_big = c.execute(text("SELECT mart.wilson_lower(9, 10), mart.wilson_lower(900, 1000)")).one()
        assert w_small < w_big < 0.9
        assert c.execute(text("SELECT mart.wilson_lower(0, 0)")).scalar() is None
        r = c.execute(text("SELECT mart.steamdb_rating(10, 10)")).scalar()
        assert 0.5 < r < 1.0
