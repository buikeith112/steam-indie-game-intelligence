"""Upserts of parsed rows into core.* tables. All functions take an open Connection
and run inside the caller's transaction."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from sqlalchemy import Connection, text

from .parsers import normalize_company

# Tags that describe format/art/audience rather than genre; excluded when picking primary_tag.
META_TAGS = {
    "Indie", "Singleplayer", "Multiplayer", "2D", "3D", "2.5D", "Great Soundtrack", "Atmospheric",
    "Free to Play", "Early Access", "Colorful", "Cute", "Funny", "Pixel Graphics", "Stylized",
    "Cartoony", "Cartoon", "Hand-drawn", "Beautiful", "Minimalist", "Retro", "Realistic",
    "Controller", "Family Friendly", "Female Protagonist", "Male Protagonist", "Replay Value",
    "Addictive", "Classic", "Masterpiece", "Difficult", "Short", "Nudity", "Sexual Content",
    "Mature", "Violent", "Gore", "Anime", "First-Person", "Third Person", "Top-Down", "Fantasy",
    "Sci-fi", "Dark", "Old School", "Remake", "Moddable", "Steam Machine", "Inventory Management",
    "Co-op", "Online Co-Op", "Local Co-Op", "Local Multiplayer", "PvP", "PvE", "Massively Multiplayer",
    "Soundtrack", "Character Customization", "Choices Matter", "Multiple Endings",
    "Relaxing", "Comedy", "Dark Humor", "Emotional", "Lore-Rich",
}

GAME_COLUMNS = {
    "name", "app_type", "release_date", "release_date_raw", "date_precision", "launch_date",
    "early_access_exit_date", "was_early_access", "is_early_access", "coming_soon", "is_indie_tag",
    "is_indie_genre", "is_self_published", "is_free", "required_age", "platform_windows", "platform_mac",
    "platform_linux", "controller_support", "steam_deck_category", "num_languages",
    "num_full_audio_languages", "num_achievements", "has_demo", "dlc_count", "metacritic_score",
    "store_review_count", "store_percent_positive", "store_review_score", "short_description",
    "header_image", "website", "last_seen_in_catalog_at", "details_fetched_at", "delisted_at",
}


def upsert_game(conn: Connection, game: dict) -> None:
    """Insert or update only the columns present in `game` (sources fill different columns)."""
    cols = [c for c in game if c in GAME_COLUMNS]
    if "name" not in cols:
        raise ValueError("game row needs a name for first insert")
    col_sql = ", ".join(["app_id"] + cols)
    val_sql = ", ".join(f":{c}" for c in ["app_id"] + cols)
    # Never overwrite a known value with NULL from a sparser source.
    upd_sql = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, core.games.{c})" for c in cols)
    conn.execute(text(f"""
        INSERT INTO core.games ({col_sql}) VALUES ({val_sql})
        ON CONFLICT (app_id) DO UPDATE SET {upd_sql}, updated_at = now()"""),
                 {"app_id": game["app_id"], **{c: game[c] for c in cols}})


def upsert_tag_dictionary(conn: Connection, tags: Iterable[dict]) -> int:
    rows = [{**t, "is_meta": t["name"] in META_TAGS} for t in tags]
    if rows:
        conn.execute(text("""
            INSERT INTO core.tags (tag_id, name, is_meta) VALUES (:tag_id, :name, :is_meta)
            ON CONFLICT (tag_id) DO UPDATE SET name = EXCLUDED.name, is_meta = EXCLUDED.is_meta"""), rows)
    return len(rows)


def replace_game_tags(conn: Connection, app_id: int, tags: list[dict]) -> None:
    conn.execute(text("DELETE FROM core.game_tags WHERE app_id = :a"), {"a": app_id})
    if tags:
        conn.execute(text("""INSERT INTO core.game_tags (app_id, tag_id, weight, rank)
                             VALUES (:app_id, :tag_id, :weight, :rank)"""), tags)


def replace_companies(conn: Connection, app_id: int, companies: list[tuple[str, str]]) -> None:
    """companies: [(display_name, role)]. Also derives core.games.is_self_published."""
    conn.execute(text("DELETE FROM core.game_companies WHERE app_id = :a"), {"a": app_id})
    seen = set()
    for name, role in companies:
        key = normalize_company(name)
        if not key or (key, role) in seen:
            continue
        seen.add((key, role))
        cid = conn.execute(text("""
            INSERT INTO core.companies (name_normalized, display_name) VALUES (:k, :n)
            ON CONFLICT (name_normalized) DO UPDATE SET display_name = core.companies.display_name
            RETURNING company_id"""), {"k": key, "n": name.strip()}).scalar_one()
        conn.execute(text("""INSERT INTO core.game_companies (app_id, company_id, role)
                             VALUES (:a, :c, :r) ON CONFLICT DO NOTHING"""), {"a": app_id, "c": cid, "r": role})
    devs = {k for k, r in seen if r == "developer"}
    pubs = {k for k, r in seen if r == "publisher"}
    if devs and pubs:
        conn.execute(text("UPDATE core.games SET is_self_published = :v WHERE app_id = :a"),
                     {"v": pubs <= devs, "a": app_id})


def _replace_dim_bridge(conn, app_id, rows, dim_table, id_col, bridge_table):
    conn.execute(text(f"DELETE FROM core.{bridge_table} WHERE app_id = :a"), {"a": app_id})
    if not rows:
        return
    conn.execute(text(f"""INSERT INTO core.{dim_table} ({id_col}, name) VALUES (:{id_col}, :name)
                          ON CONFLICT ({id_col}) DO UPDATE SET name = EXCLUDED.name"""), rows)
    conn.execute(text(f"""INSERT INTO core.{bridge_table} (app_id, {id_col}) VALUES (:app_id, :{id_col})
                          ON CONFLICT DO NOTHING"""), [{"app_id": app_id, id_col: r[id_col]} for r in rows])


def replace_genres(conn: Connection, app_id: int, genres: list[dict]) -> None:
    _replace_dim_bridge(conn, app_id, genres, "genres", "genre_id", "game_genres")


def replace_categories(conn: Connection, app_id: int, categories: list[dict]) -> None:
    _replace_dim_bridge(conn, app_id, categories, "categories", "category_id", "game_categories")


def replace_languages(conn: Connection, app_id: int, languages: list[dict]) -> None:
    conn.execute(text("DELETE FROM core.game_languages WHERE app_id = :a"), {"a": app_id})
    if languages:
        conn.execute(text("""INSERT INTO core.game_languages (app_id, language, full_audio)
                             VALUES (:app_id, :language, :full_audio)"""), languages)


def record_price(conn: Connection, price: dict, observed_at: datetime, source: str) -> bool:
    """SCD-2: close the open row and open a new one only when the price actually changed.
    Returns True if a new version was written. Also seeds games.launch_price_cents once."""
    if not price or price.get("final_cents") is None:
        return False
    app_id = price["app_id"]
    cur = conn.execute(text("""
        SELECT initial_cents, final_cents, discount_pct FROM core.price_history
        WHERE app_id = :a AND valid_to IS NULL FOR UPDATE"""), {"a": app_id}).first()
    new = (price["initial_cents"], price["final_cents"], price["discount_pct"])
    changed = cur is None or tuple(cur) != new
    if changed:
        conn.execute(text("""UPDATE core.price_history SET valid_to = :t
                             WHERE app_id = :a AND valid_to IS NULL AND valid_from < :t"""),
                     {"a": app_id, "t": observed_at})
        conn.execute(text("""
            INSERT INTO core.price_history (app_id, valid_from, currency, initial_cents, final_cents, discount_pct, source)
            VALUES (:app_id, :t, :currency, :initial_cents, :final_cents, :discount_pct, :source)
            ON CONFLICT (app_id, valid_from) DO NOTHING"""), {**price, "t": observed_at, "source": source})
    # Launch price approximation: list (pre-discount) price at first observation.
    conn.execute(text("""UPDATE core.games SET launch_price_cents = :p, launch_price_source = 'first_observed_list'
                         WHERE app_id = :a AND launch_price_cents IS NULL"""),
                 {"a": app_id, "p": price["initial_cents"]})
    return changed


def upsert_review_snapshot(conn: Connection, app_id: int, snap_date: date, summary: dict) -> None:
    conn.execute(text("""
        INSERT INTO core.review_snapshots (app_id, snapshot_date, total_positive, total_negative,
                                           total_reviews, review_score, review_score_desc)
        VALUES (:app_id, :d, :total_positive, :total_negative, :total_reviews, :review_score, :review_score_desc)
        ON CONFLICT (app_id, snapshot_date) DO UPDATE SET
            total_positive = EXCLUDED.total_positive, total_negative = EXCLUDED.total_negative,
            total_reviews = EXCLUDED.total_reviews, review_score = EXCLUDED.review_score,
            review_score_desc = EXCLUDED.review_score_desc"""), {"app_id": app_id, "d": snap_date, **summary})


def upsert_review_window(conn: Connection, row: dict) -> None:
    conn.execute(text("""
        INSERT INTO core.review_windows (app_id, window_code, window_start, window_end, positive, negative, is_complete)
        VALUES (:app_id, :window_code, :window_start, :window_end, :positive, :negative, :is_complete)
        ON CONFLICT (app_id, window_code) DO UPDATE SET
            window_start = EXCLUDED.window_start, window_end = EXCLUDED.window_end,
            positive = EXCLUDED.positive, negative = EXCLUDED.negative,
            is_complete = EXCLUDED.is_complete, computed_at = now()"""), row)


def replace_histogram(conn: Connection, app_id: int, rows: list[dict]) -> None:
    conn.execute(text("DELETE FROM core.review_histogram WHERE app_id = :a"), {"a": app_id})
    if rows:
        conn.execute(text("""
            INSERT INTO core.review_histogram (app_id, granularity, period_start, up, down)
            VALUES (:app_id, :granularity, :period_start, :up, :down)
            ON CONFLICT DO NOTHING"""), rows)


def upsert_reviews(conn: Connection, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("recommendation_id", "sample_type"))
    conn.execute(text(f"""
        INSERT INTO core.reviews ({", ".join(cols)}) VALUES ({", ".join(":" + c for c in cols)})
        ON CONFLICT (recommendation_id) DO UPDATE SET {upd}"""), rows)


def upsert_engagement(conn: Connection, row: dict, snap_date: date) -> None:
    conn.execute(text("""
        INSERT INTO core.engagement_snapshots (app_id, snapshot_date, owners_low, owners_high, avg_forever_min,
            median_forever_min, avg_2weeks_min, median_2weeks_min, ccu_peak_yesterday, initial_price_cents)
        VALUES (:app_id, :d, :owners_low, :owners_high, :avg_forever_min, :median_forever_min,
                :avg_2weeks_min, :median_2weeks_min, :ccu_peak_yesterday, :initial_price_cents)
        ON CONFLICT (app_id, snapshot_date) DO UPDATE SET
            owners_low = EXCLUDED.owners_low, owners_high = EXCLUDED.owners_high,
            avg_forever_min = EXCLUDED.avg_forever_min, median_forever_min = EXCLUDED.median_forever_min,
            avg_2weeks_min = EXCLUDED.avg_2weeks_min, median_2weeks_min = EXCLUDED.median_2weeks_min,
            ccu_peak_yesterday = EXCLUDED.ccu_peak_yesterday, initial_price_cents = EXCLUDED.initial_price_cents"""),
                 {**row, "d": snap_date})


def insert_player_count(conn: Connection, app_id: int, captured_at: datetime, count: int) -> None:
    conn.execute(text("""INSERT INTO core.player_count_snapshots (app_id, captured_at, player_count)
                         VALUES (:a, :t, :c) ON CONFLICT DO NOTHING"""), {"a": app_id, "t": captured_at, "c": count})


def upsert_news(conn: Connection, rows: list[dict]) -> None:
    if rows:
        conn.execute(text("""
            INSERT INTO core.news_events (gid, app_id, published_at, feed_label, title, is_patch)
            VALUES (:gid, :app_id, :published_at, :feed_label, :title, :is_patch)
            ON CONFLICT (gid) DO NOTHING"""), rows)
