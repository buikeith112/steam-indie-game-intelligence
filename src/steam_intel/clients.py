"""Thin, typed wrappers around each Steam / SteamSpy endpoint (see docs/api_findings.md).

Clients return `ApiResult` with the untouched JSON so it can be stored in raw.api_responses
before any parsing happens.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from .config import Settings
from .http import ApiResult, HttpClient

WEBAPI = "https://api.steampowered.com"
STORE = "https://store.steampowered.com"
STEAMSPY = "https://steamspy.com/api.php"

INDIE_TAG_ID = 492
GETITEMS_MAX_BATCH = 200          # 500 -> HTTP 400 (URL too long); POST is not allowed
QUERY_MAX_PAGE = 1000
QUERY_STABLE_SORT = 2             # ascending appid; other sorts shift between pages
PRICE_BATCH = 100                 # appdetails?filters=price_overview with many appids
CONTEXT = {"language": "english", "country_code": "US"}

# What we ask GetItems for during the catalog crawl.
CATALOG_DATA_REQUEST = {
    "include_basic_info": True,
    "include_tag_count": 20,
    "include_reviews": True,
    "include_release": True,
    "include_platforms": True,
}


def _input_json(obj: dict) -> dict:
    return {"input_json": json.dumps(obj, separators=(",", ":"))}


class SteamClient:
    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.settings = settings

    # ---- catalog ---------------------------------------------------------------
    def store_query_ids(self, start: int, count: int = QUERY_MAX_PAGE,
                        tag_id: int = INDIE_TAG_ID) -> ApiResult:
        """Released games carrying `tag_id`, IDs only.

        sort=2 (ascending appid) is the only order we found that paginates deterministically:
        a full pass returns every record exactly once. sort=21 lost ~8% and sort=0/10 lost ~33%
        to page shifting (see docs/api_findings.md).
        """
        q = {"query": {"start": start, "count": count, "sort": QUERY_STABLE_SORT,
                       "filters": {"released_only": True,
                                   "type_filters": {"include_games": True},
                                   "tagids_must_match": [{"tagids": [tag_id]}]}},
             "context": CONTEXT}
        return self.http.get("webapi", f"{WEBAPI}/IStoreQueryService/Query/v1/", _input_json(q))

    def get_items(self, app_ids: Sequence[int], data_request: dict | None = None) -> ApiResult:
        if len(app_ids) > GETITEMS_MAX_BATCH:
            raise ValueError(f"GetItems accepts at most {GETITEMS_MAX_BATCH} ids per call")
        req = {"ids": [{"appid": int(a)} for a in app_ids], "context": CONTEXT,
               "data_request": data_request or CATALOG_DATA_REQUEST}
        return self.http.get("webapi", f"{WEBAPI}/IStoreBrowseService/GetItems/v1/", _input_json(req))

    def app_list(self, last_appid: int = 0, if_modified_since: int | None = None,
                 max_results: int = 50000) -> ApiResult:
        """Full catalog deltas. Requires STEAM_API_KEY."""
        if not self.settings.steam_api_key:
            raise RuntimeError("STEAM_API_KEY is required for IStoreService/GetAppList")
        params = {"key": self.settings.steam_api_key, "last_appid": last_appid,
                  "max_results": max_results, "include_games": "true"}
        if if_modified_since:
            params["if_modified_since"] = if_modified_since
        return self.http.get("webapi", f"{WEBAPI}/IStoreService/GetAppList/v1/", params)

    def tag_list(self) -> ApiResult:
        return self.http.get("store", f"{STORE}/tagdata/populartags/english")

    # ---- details & prices -------------------------------------------------------
    def app_details(self, app_id: int) -> ApiResult:
        return self.http.get("store", f"{STORE}/api/appdetails",
                             {"appids": app_id, "cc": "us", "l": "english"})

    def price_overview_batch(self, app_ids: Iterable[int]) -> ApiResult:
        ids = ",".join(str(int(a)) for a in app_ids)
        return self.http.get("store", f"{STORE}/api/appdetails",
                             {"appids": ids, "cc": "us", "filters": "price_overview"})

    # ---- reviews -----------------------------------------------------------------
    def app_reviews(self, app_id: int, *, filter: int = 1, num_per_page: int = 1,
                    cursor: str = "*", review_type: int = 0, purchase_type: int = 0,
                    date_range: tuple[int, int] | None = None, languages: Sequence[str] = ("all",),
                    day_range: int | None = None) -> ApiResult:
        """IUserReviewsService/GetAppReviews. filter: 0 helpful, 1 recent, 2 updated, 3 funny.

        purchase_type: 0 Steam purchases, 1 all, 2 non-Steam purchases. The store page and GetItems
        count 0 for paid games and 1 for free games (which have no purchases); see ingest._purchase_type.
        """
        q: dict = {"appid": int(app_id), "filter": filter, "num_per_page": num_per_page,
                   "cursor": cursor, "review_type": review_type, "purchase_type": purchase_type,
                   "languages": list(languages)}
        if date_range:
            q["date_range_start"], q["date_range_end"] = int(date_range[0]), int(date_range[1])
        if day_range is not None:
            q["day_range"] = day_range
        return self.http.get("webapi", f"{WEBAPI}/IUserReviewsService/GetAppReviews/v1/", _input_json(q))

    def review_histogram(self, app_id: int) -> ApiResult:
        return self.http.get("store", f"{STORE}/appreviewhistogram/{int(app_id)}",
                             {"l": "english", "review_score_preference": 0})

    # ---- players, news -------------------------------------------------------------
    def current_players(self, app_id: int) -> ApiResult:
        return self.http.get("webapi", f"{WEBAPI}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
                             {"appid": int(app_id)})

    def news(self, app_id: int, count: int = 50) -> ApiResult:
        return self.http.get("webapi", f"{WEBAPI}/ISteamNews/GetNewsForApp/v2/",
                             {"appid": int(app_id), "count": count, "maxlength": 1})

    # ---- SteamSpy ----------------------------------------------------------------
    def steamspy_appdetails(self, app_id: int) -> ApiResult:
        return self.http.get("steamspy", STEAMSPY, {"request": "appdetails", "appid": int(app_id)})
