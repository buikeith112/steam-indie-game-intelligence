from datetime import date

import pytest

from steam_intel.transform import parsers as p

BALATRO = 2379780


@pytest.mark.parametrize("raw,expected", [
    ("Feb 20, 2024", (date(2024, 2, 20), "day")),
    ("20 Feb, 2024", (date(2024, 2, 20), "day")),
    ("Sept 5, 2023", (date(2023, 9, 5), "day")),
    ("Feb 2024", (date(2024, 2, 1), "month")),
    ("Q3 2025", (date(2025, 7, 1), "quarter")),
    ("2026", (date(2026, 1, 1), "year")),
    ("Coming soon", (None, "unknown")),
    ("To be announced", (None, "unknown")),
    ("", (None, "unknown")),
    (None, (None, "unknown")),
    ("Feb 30, 2024", (None, "unknown")),
])
def test_parse_release_date(raw, expected):
    assert p.parse_release_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2,000,000 .. 5,000,000", (2_000_000, 5_000_000)),
    ("0 .. 20,000", (0, 20_000)),
    ("garbage", (None, None)),
    (None, (None, None)),
])
def test_parse_owners(raw, expected):
    assert p.parse_owners(raw) == expected


@pytest.mark.parametrize("a,b", [
    ("Devolver Digital", "Devolver Digital, Inc."),
    ("Team Cherry", "  team cherry "),
    ("Supergiant Games, LLC", "Supergiant Games"),
    ("Re-Logic", "Re-Logic Ltd."),
    ("Annapurna Interactive™", "Annapurna Interactive"),
])
def test_normalize_company_merges_variants(a, b):
    assert p.normalize_company(a) == p.normalize_company(b)


def test_parse_languages_full_audio_marker():
    raw = ("English<strong>*</strong>, French, Simplified Chinese<strong>*</strong>"
           "<br><strong>*</strong>languages with full audio support")
    assert p.parse_languages(raw) == [("English", True), ("French", False), ("Simplified Chinese", True)]
    assert p.parse_languages(None) == []


def test_parse_appdetails_balatro(load_fixture):
    out = p.parse_appdetails(load_fixture("appdetails_balatro.json"), BALATRO)
    g = out["game"]
    assert g["name"] == "Balatro" and g["app_type"] == "game"
    assert g["release_date"] == date(2024, 2, 20) and g["date_precision"] == "day"
    assert g["is_indie_genre"] is True
    assert g["is_self_published"] is False          # LocalThunk / Playstack
    assert g["controller_support"] == "full"
    assert g["num_languages"] == 12
    assert g["dlc_count"] == 1 and g["metacritic_score"] == 90
    assert {x["genre_id"] for x in out["genres"]} == {2, 4, 23}
    assert out["price"] == {"app_id": BALATRO, "currency": "USD", "initial_cents": 1499,
                            "final_cents": 1499, "discount_pct": 0}
    assert ("LocalThunk", "developer") in out["companies"]


def test_parse_appdetails_failure_returns_none():
    assert p.parse_appdetails({"123": {"success": False}}, 123) is None
    assert p.parse_appdetails({}, 123) is None


def test_parse_price_batch(load_fixture):
    prices = p.parse_price_batch(load_fixture("appdetails_multi_price.json"))
    assert len(prices) == 5
    assert prices[1794680]["final_cents"] == 499


def test_parse_getitems_balatro(load_fixture):
    items = load_fixture("getitems_balatro_hades_stardew.json")["response"]["store_items"]
    out = p.parse_getitems_item(items[0])
    g = out["game"]
    assert g["app_id"] == BALATRO and g["is_indie_tag"] is True
    assert g["launch_date"] == date(2024, 2, 20)
    assert g["steam_deck_category"] == 3
    assert g["store_review_count"] > 100_000 and g["store_percent_positive"] >= 90
    assert out["tags"][0]["rank"] == 1 and len(out["tags"]) == 20
    assert out["price"]["final_cents"] == 1499


def test_parse_getitems_early_access_and_discount(load_fixture):
    items = {i["appid"]: i for i in load_fixture("getitems_sample.json")["response"]["store_items"]}
    valheim = p.parse_getitems_item(items[892970])
    assert valheim["game"]["was_early_access"] is True
    assert valheim["game"]["launch_date"] < valheim["game"]["release_date"]   # EA launch precedes 1.0
    assert valheim["price"]["discount_pct"] == 10
    assert valheim["price"]["initial_cents"] == 2999
    phasmo = p.parse_getitems_item(items[739630])
    assert phasmo["game"]["is_early_access"] is True
    free = p.parse_getitems_item(items[304930])
    assert free["price"]["final_cents"] == 0


def test_parse_getitems_ignores_bundle_price(load_fixture):
    item = load_fixture("getitems_balatro_hades_stardew.json")["response"]["store_items"][0]
    item = {**item, "best_purchase_option": {
        "bundleid": 44099, "included_game_count": 2, "final_price_in_cents": "2860",
        "original_price_in_cents": "4498", "discount_pct": 36}}
    assert p.parse_getitems_item(item)["price"] is None


def test_parse_getitems_invisible_item_skipped():
    assert p.parse_getitems_item({"appid": 1, "success": 2}) is None
    assert p.parse_getitems_item({"appid": 1, "success": 1, "visible": False}) is None


def test_parse_query_ids(load_fixture):
    ids, total = p.parse_query_ids(load_fixture("query_ids_page.json"))
    assert len(ids) == 50 and total > 60_000


def test_review_summary_window(load_fixture):
    s = p.parse_review_summary(load_fixture("gar_d30_all.json"))
    assert s["total_positive"] + s["total_negative"] == s["total_reviews"]


def test_parse_reviews_hashes_author(load_fixture):
    rows, cursor = p.parse_reviews(load_fixture("gar_page5.json"), BALATRO, "helpful")
    assert len(rows) == 5 and cursor
    assert all(len(r["author_hash"]) == 64 for r in rows)
    assert all(r["review_text"] is None for r in rows)
    assert all(r["created_at"].tzinfo is not None for r in rows)


def test_scrub_review_payload_hashes_ids_and_drops_text(load_fixture):
    raw = load_fixture("gar_page5.json")
    scrubbed = p.scrub_review_payload(raw)
    reviews = scrubbed["response"]["reviews"]
    assert all(len(r["author"]["steamid"]) == 64 and r["review"] is None for r in reviews)
    assert raw["response"]["reviews"][0]["review"] is not None          # input not mutated
    # parsing a scrubbed payload yields the same author_hash as parsing the original
    a, _ = p.parse_reviews(raw, BALATRO, "helpful")
    b, _ = p.parse_reviews(scrubbed, BALATRO, "helpful")
    assert [r["author_hash"] for r in a] == [r["author_hash"] for r in b]
    assert p.scrub_review_payload(raw, keep_text=True)["response"]["reviews"][0]["review"] is not None


def test_parse_histogram(load_fixture):
    rows = p.parse_histogram(load_fixture("histogram.json"), BALATRO)
    months = [r for r in rows if r["granularity"] == "month"]
    days = [r for r in rows if r["granularity"] == "day"]
    assert len(months) == 34 and len(days) == 30
    assert months[0]["period_start"] == date(2024, 2, 1)


def test_parse_steamspy(load_fixture):
    row = p.parse_steamspy(load_fixture("steamspy_appdetails.json"))
    assert row["owners_low"] == 2_000_000 and row["owners_high"] == 5_000_000
    assert row["median_forever_min"] == 1208
    assert p.parse_steamspy({"appid": 999999, "name": "x"}) is None


def test_parse_player_count(load_fixture):
    assert p.parse_player_count(load_fixture("ccu.json")) == 5154
    assert p.parse_player_count({"response": {"result": 42}}) is None


def test_parse_news(load_fixture):
    rows = p.parse_news(load_fixture("news.json"), BALATRO)
    assert rows and all(r["published_at"] for r in rows)
