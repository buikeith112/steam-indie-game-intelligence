"""Pure functions: raw API JSON -> plain dicts shaped like core.* rows.

No I/O here, so everything is unit-testable against tests/fixtures.
"""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Any

# ---------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def ts_to_dt(ts: Any) -> datetime | None:
    """Unix seconds -> aware UTC datetime. 0 / None / garbage -> None."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=UTC) if ts > 0 else None


def ts_to_date(ts: Any) -> date | None:
    dt = ts_to_dt(ts)
    return dt.date() if dt else None


def to_int(v: Any) -> int | None:
    """Parse ints that may arrive as strings ('1499'), floats, or junk."""
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except ValueError:
        return None


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def hash_steamid(steamid: str | int) -> str:
    """SHA-256 of a SteamID. Idempotent: an already-hashed value is returned unchanged,
    so payloads scrubbed before storage parse to the same author_hash."""
    s = str(steamid)
    return s if _SHA256_HEX.match(s) else hashlib.sha256(s.encode()).hexdigest()


def scrub_review_payload(doc: dict, keep_text: bool = False) -> dict:
    """Privacy scrub applied BEFORE a review response is written to raw: SteamIDs become
    hashes and review text is dropped unless explicitly kept. Returns a modified copy."""
    import copy
    out = copy.deepcopy(doc) if doc else doc
    for r in ((out or {}).get("response") or {}).get("reviews") or []:
        author = r.get("author") or {}
        if "steamid" in author:
            author["steamid"] = hash_steamid(author["steamid"])
        if not keep_text:
            r["review"] = None
    return out


# ---------------------------------------------------------------------------------
# Release dates (appdetails.release_date.date)
# ---------------------------------------------------------------------------------
_DAY_MON_YEAR = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})$")     # 20 Feb, 2024
_MON_DAY_YEAR = re.compile(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})$")     # Feb 20, 2024
_MON_YEAR = re.compile(r"^([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})$")                     # Feb 2024
_QUARTER = re.compile(r"^Q([1-4])\s+(\d{4})$", re.IGNORECASE)                                 # Q1 2025
_YEAR = re.compile(r"^(\d{4})$")


def parse_release_date(raw: str | None) -> tuple[date | None, str]:
    """Return (date, precision). Partial dates resolve to the first day of the period."""
    if not raw:
        return None, "unknown"
    s = raw.strip()
    try:
        if m := _MON_DAY_YEAR.match(s):
            return date(int(m[3]), MONTHS[m[1].lower()], int(m[2])), "day"
        if m := _DAY_MON_YEAR.match(s):
            return date(int(m[3]), MONTHS[m[2].lower()], int(m[1])), "day"
        if m := _MON_YEAR.match(s):
            return date(int(m[2]), MONTHS[m[1].lower()], 1), "month"
        if m := _QUARTER.match(s):
            return date(int(m[2]), (int(m[1]) - 1) * 3 + 1, 1), "quarter"
        if m := _YEAR.match(s):
            return date(int(m[1]), 1, 1), "year"
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return date.fromisoformat(s), "day"
    except (KeyError, ValueError):
        pass
    return None, "unknown"


# ---------------------------------------------------------------------------------
# Company names
# ---------------------------------------------------------------------------------
_SUFFIX = re.compile(
    r"[,\s]+(inc|llc|ltd|limited|gmbh|s\.?a\.?|s\.?r\.?l\.?|co|corp|corporation|oy|ab|bv|pty|plc|k\.?k\.?)\.?$",
    re.IGNORECASE)


def normalize_company(name: str) -> str:
    """Key used to merge spelling variants: 'Devolver Digital, Inc.' == 'devolver digital'."""
    s = re.sub(r"[™®©]", "", html.unescape(name))       # strip before NFKC turns ™ into "TM"
    s = unicodedata.normalize("NFKC", s).strip()
    prev = None
    while prev != s:
        prev = s
        s = _SUFFIX.sub("", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s.casefold()


# ---------------------------------------------------------------------------------
# Supported languages (appdetails HTML string)
# ---------------------------------------------------------------------------------
def parse_languages(raw: str | None) -> list[tuple[str, bool]]:
    """'English<strong>*</strong>, French<br><strong>*</strong>languages with full audio support'
    -> [('English', True), ('French', False)]"""
    if not raw:
        return []
    s = re.split(r"<br\s*/?>", raw, maxsplit=1)[0]
    out: list[tuple[str, bool]] = []
    seen = set()
    for part in s.split(","):
        full_audio = "*" in part
        lang = re.sub(r"<[^>]+>", "", part).replace("*", "").strip()
        lang = html.unescape(lang)
        if lang and lang not in seen:
            seen.add(lang)
            out.append((lang, full_audio))
    return out


# ---------------------------------------------------------------------------------
# SteamSpy
# ---------------------------------------------------------------------------------
def parse_owners(raw: str | None) -> tuple[int | None, int | None]:
    """'2,000,000 .. 5,000,000' -> (2000000, 5000000)."""
    if not raw:
        return None, None
    parts = [p.strip() for p in str(raw).split("..")]
    if len(parts) != 2:
        return None, None
    return to_int(parts[0]), to_int(parts[1])


def parse_steamspy(doc: dict) -> dict | None:
    """SteamSpy appdetails -> engagement_snapshots row. None when data is hidden/missing."""
    if not doc or to_int(doc.get("appid")) in (None, 999999) or not doc.get("name"):
        return None
    lo, hi = parse_owners(doc.get("owners"))
    return {
        "app_id": int(doc["appid"]),
        "owners_low": lo,
        "owners_high": hi,
        "avg_forever_min": to_int(doc.get("average_forever")),
        "median_forever_min": to_int(doc.get("median_forever")),
        "avg_2weeks_min": to_int(doc.get("average_2weeks")),
        "median_2weeks_min": to_int(doc.get("median_2weeks")),
        "ccu_peak_yesterday": to_int(doc.get("ccu")),
        "initial_price_cents": to_int(doc.get("initialprice")),
    }


# ---------------------------------------------------------------------------------
# IStoreQueryService/Query and IStoreBrowseService/GetItems
# ---------------------------------------------------------------------------------
def parse_query_ids(doc: dict) -> tuple[list[int], int | None]:
    resp = (doc or {}).get("response", {})
    ids = [int(x["appid"]) for x in resp.get("ids", []) if x.get("appid")]
    total = (resp.get("metadata") or {}).get("total_matching_records")
    return ids, total


def parse_getitems_item(item: dict) -> dict | None:
    """One GetItems store_item -> {'game': {...}, 'tags': [...], 'companies': [...], 'price': {...}|None}.

    Returns None for items that failed / are invisible (delisted or region-locked).
    """
    if not item or item.get("success") != 1 or not item.get("visible", False):
        return None
    app_id = int(item.get("appid") or item["id"])
    rel = item.get("release") or {}
    plat = item.get("platforms") or {}
    summ = (item.get("reviews") or {}).get("summary_filtered") or {}
    basic = item.get("basic_info") or {}

    steam_release = ts_to_date(rel.get("steam_release_date"))
    original_steam = ts_to_date(rel.get("original_steam_release_date"))
    ea_exit = ts_to_date(rel.get("release_from_early_access_date"))
    is_ea = bool(rel.get("is_early_access") or item.get("is_early_access"))

    game = {
        "app_id": app_id,
        "name": item.get("name") or "",
        "release_date": steam_release,
        "launch_date": original_steam or steam_release,
        "early_access_exit_date": ea_exit,
        "was_early_access": bool(original_steam or ea_exit or is_ea),
        "is_early_access": is_ea,
        "is_indie_tag": any(t.get("tagid") == 492 for t in item.get("tags", [])) or 492 in item.get("tagids", []),
        "is_free": bool(item.get("is_free", False)),
        "platform_windows": bool(plat.get("windows", False)),
        "platform_mac": bool(plat.get("mac", False)),
        "platform_linux": bool(plat.get("steamos_linux", False)),
        "steam_deck_category": plat.get("steam_deck_compat_category"),
        "store_review_count": to_int(summ.get("review_count")),
        "store_percent_positive": to_int(summ.get("percent_positive")),
        "store_review_score": to_int(summ.get("review_score")),
        "short_description": basic.get("short_description"),
    }

    tags = [{"app_id": app_id, "tag_id": int(t["tagid"]), "weight": to_int(t.get("weight")), "rank": i}
            for i, t in enumerate(item.get("tags", []), start=1)]

    companies = [(d["name"], "developer") for d in basic.get("developers", []) if d.get("name")]
    companies += [(p["name"], "publisher") for p in basic.get("publishers", []) if p.get("name")]

    price = None
    bpo = item.get("best_purchase_option") or {}
    # The "best" option can be a bundle with other games (bundleid / included_game_count > 1);
    # that is not this game's price, so leave pricing to appdetails in that case.
    is_bundle = "bundleid" in bpo or (to_int(bpo.get("included_game_count")) or 1) > 1 or bpo.get("must_purchase_as_set")
    final = None if is_bundle else to_int(bpo.get("final_price_in_cents"))
    if final is not None and not game["is_free"]:
        initial = to_int(bpo.get("original_price_in_cents")) or final
        price = {"app_id": app_id, "currency": "USD", "initial_cents": initial, "final_cents": final,
                 "discount_pct": to_int(bpo.get("discount_pct")) or 0}
    elif game["is_free"]:
        price = {"app_id": app_id, "currency": "USD", "initial_cents": 0, "final_cents": 0, "discount_pct": 0}

    return {"game": game, "tags": tags, "companies": companies, "price": price}


def parse_tag_list(doc: list[dict]) -> list[dict]:
    return [{"tag_id": int(t["tagid"]), "name": t["name"]} for t in (doc or []) if "tagid" in t]


# ---------------------------------------------------------------------------------
# appdetails
# ---------------------------------------------------------------------------------
def parse_appdetails(doc: dict, app_id: int) -> dict | None:
    """Full appdetails for one app. None when success=false (delisted, region-locked, removed)."""
    entry = (doc or {}).get(str(app_id)) or {}
    if not entry.get("success"):
        return None
    d = entry.get("data") or {}
    rel = d.get("release_date") or {}
    rel_date, precision = parse_release_date(rel.get("date"))
    langs = parse_languages(d.get("supported_languages"))
    genres = [{"genre_id": int(g["id"]), "name": g["description"]} for g in d.get("genres") or []]
    categories = [{"category_id": int(c["id"]), "name": c["description"]} for c in d.get("categories") or []]
    devs = [n for n in d.get("developers") or [] if n]
    pubs = [n for n in d.get("publishers") or [] if n]
    po = d.get("price_overview")

    game = {
        "app_id": app_id,
        "name": d.get("name") or "",
        "app_type": d.get("type"),
        "release_date_raw": rel.get("date"),
        "date_precision": precision,
        "coming_soon": bool(rel.get("coming_soon", False)),
        "is_free": bool(d.get("is_free", False)),
        "is_indie_genre": any(g["genre_id"] == 23 for g in genres),
        "is_self_published": bool(devs and pubs and
                                  {normalize_company(x) for x in pubs} <= {normalize_company(x) for x in devs}),
        "required_age": to_int(d.get("required_age")),
        "platform_windows": bool((d.get("platforms") or {}).get("windows")),
        "platform_mac": bool((d.get("platforms") or {}).get("mac")),
        "platform_linux": bool((d.get("platforms") or {}).get("linux")),
        "controller_support": d.get("controller_support"),
        "num_languages": len(langs),
        "num_full_audio_languages": sum(1 for _, a in langs if a),
        "num_achievements": to_int((d.get("achievements") or {}).get("total")) or 0,
        "has_demo": bool(d.get("demos")),
        "dlc_count": len(d.get("dlc") or []),
        "metacritic_score": to_int((d.get("metacritic") or {}).get("score")),
        "short_description": html.unescape(d.get("short_description") or "") or None,
        "header_image": d.get("header_image"),
        "website": d.get("website"),
    }
    if rel_date and precision == "day":
        game["release_date"] = rel_date

    price = None
    if po and to_int(po.get("final")) is not None:
        price = {"app_id": app_id, "currency": po.get("currency", "USD"),
                 "initial_cents": to_int(po.get("initial")), "final_cents": to_int(po.get("final")),
                 "discount_pct": to_int(po.get("discount_percent")) or 0}
    elif game["is_free"]:
        price = {"app_id": app_id, "currency": "USD", "initial_cents": 0, "final_cents": 0, "discount_pct": 0}

    return {
        "game": game,
        "genres": genres,
        "categories": categories,
        "companies": [(n, "developer") for n in devs] + [(n, "publisher") for n in pubs],
        "languages": [{"app_id": app_id, "language": lang, "full_audio": a} for lang, a in langs],
        "price": price,
    }


def parse_price_batch(doc: dict) -> dict[int, dict | None]:
    """appdetails?filters=price_overview -> {app_id: price row or None (free / no price)}."""
    out: dict[int, dict | None] = {}
    for key, entry in (doc or {}).items():
        app_id = int(key)
        if not entry.get("success"):
            continue
        data = entry.get("data")
        po = data.get("price_overview") if isinstance(data, dict) else None
        out[app_id] = None if not po else {
            "app_id": app_id, "currency": po.get("currency", "USD"),
            "initial_cents": to_int(po.get("initial")), "final_cents": to_int(po.get("final")),
            "discount_pct": to_int(po.get("discount_percent")) or 0}
    return out


# ---------------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------------
def parse_review_summary(doc: dict) -> dict | None:
    """query_summary totals (first page, review_type=0). Works for date-ranged queries too."""
    qs = ((doc or {}).get("response") or {}).get("query_summary") or {}
    if "total_positive" not in qs:
        return None
    return {"total_positive": int(qs["total_positive"]), "total_negative": int(qs["total_negative"]),
            "total_reviews": int(qs["total_reviews"]), "review_score": to_int(qs.get("review_score")),
            "review_score_desc": qs.get("review_score_desc")}


def parse_reviews(doc: dict, app_id: int, sample_type: str, keep_text: bool = False) -> tuple[list[dict], str | None]:
    resp = (doc or {}).get("response") or {}
    rows = []
    for r in resp.get("reviews") or []:
        a = r.get("author") or {}
        rows.append({
            "recommendation_id": int(r["recommendationid"]),
            "app_id": app_id,
            "author_hash": hash_steamid(a.get("steamid", "")),
            "sample_type": sample_type,
            "voted_up": bool(r.get("voted_up")),
            "language": r.get("language"),
            "created_at": ts_to_dt(r.get("timestamp_created")),
            "updated_at": ts_to_dt(r.get("timestamp_updated")),
            "playtime_at_review_min": to_int(a.get("playtime_at_review")),
            "playtime_forever_min": to_int(a.get("playtime_forever")),
            "playtime_2weeks_min": to_int(a.get("playtime_last_two_weeks")),
            "author_num_reviews": to_int(a.get("num_reviews")),
            "votes_up": to_int(r.get("votes_up")),
            "votes_funny": to_int(r.get("votes_funny")),
            "weighted_vote_score": float(r.get("weighted_vote_score") or 0),
            "comment_count": to_int(r.get("comment_count")),
            "steam_purchase": r.get("steam_purchase"),
            "received_for_free": r.get("received_for_free"),
            "written_during_early_access": r.get("written_during_early_access"),
            "primarily_steam_deck": r.get("primarily_steam_deck"),
            "refunded": r.get("refunded"),
            "review_text": r.get("review") if keep_text else None,
        })
    return rows, resp.get("cursor")


def parse_histogram(doc: dict, app_id: int) -> list[dict]:
    res = (doc or {}).get("results") or {}
    if not (doc or {}).get("success"):
        return []
    gran = res.get("rollup_type") or "month"
    rows = [{"app_id": app_id, "granularity": gran, "period_start": ts_to_date(r["date"]),
             "up": int(r.get("recommendations_up", 0)), "down": int(r.get("recommendations_down", 0))}
            for r in res.get("rollups") or [] if ts_to_date(r.get("date"))]
    rows += [{"app_id": app_id, "granularity": "day", "period_start": ts_to_date(r["date"]),
              "up": int(r.get("recommendations_up", 0)), "down": int(r.get("recommendations_down", 0))}
             for r in res.get("recent") or [] if ts_to_date(r.get("date"))]
    return rows


# ---------------------------------------------------------------------------------
# Players & news
# ---------------------------------------------------------------------------------
def parse_player_count(doc: dict) -> int | None:
    resp = (doc or {}).get("response") or {}
    return to_int(resp.get("player_count")) if resp.get("result") == 1 else None


def parse_news(doc: dict, app_id: int) -> list[dict]:
    items = ((doc or {}).get("appnews") or {}).get("newsitems") or []
    out = []
    for n in items:
        tags = n.get("tags") or []
        title = n.get("title") or ""
        out.append({"gid": str(n["gid"]), "app_id": app_id, "published_at": ts_to_dt(n.get("date")),
                    "feed_label": n.get("feedlabel"), "title": title,
                    "is_patch": "patchnotes" in tags or bool(re.search(r"\b(patch|hotfix|update)\b", title, re.IGNORECASE))})
    return [o for o in out if o["published_at"]]
