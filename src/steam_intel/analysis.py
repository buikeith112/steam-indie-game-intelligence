"""Statistics that back the dashboard (written to mart.analysis_results).

Requires the `analysis` extra: pip install -e .[analysis]
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import Engine, text

log = logging.getLogger(__name__)
MIN_N = 30          # minimum sample for a correlation
MIN_N_OLS = 200     # minimum sample for the Q1 regression (year + tag dummies need room)


def _games(engine: Engine) -> pd.DataFrame:
    sql = """
        SELECT g.app_id, g.launch_year, g.primary_tag, g.price_bucket_id, g.is_free, g.in_analytical_scope,
               g.was_early_access, g.is_self_published,
               f.total_reviews, f.positive_ratio, f.early_reviews_30d, f.early_positive_ratio_30d,
               f.early_volume_band, f.reviews_d31_365, f.launch_price_usd, f.median_playtime_h,
               f.est_owners_mid, f.is_hit_1k
        FROM mart.dim_game g JOIN mart.fact_game_metrics f USING (app_id)"""
    with engine.connect() as c:
        df = pd.read_sql(text(sql), c)
    num = ["total_reviews", "positive_ratio", "early_reviews_30d", "early_positive_ratio_30d", "reviews_d31_365",
           "launch_price_usd", "median_playtime_h", "est_owners_mid"]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    return df


def q1_early_sentiment(df: pd.DataFrame) -> list[tuple]:
    """Does early (first 30 days) sentiment relate to later (days 31-365) review volume?"""
    d = df[df.in_analytical_scope & (df.early_reviews_30d >= 10) & df.reviews_d31_365.notna()].copy()
    out: list[tuple] = [("q1_sample", "all", "n", len(d))]
    if len(d) < MIN_N:
        return out
    rho, p = stats.spearmanr(d.early_positive_ratio_30d, d.reviews_d31_365)
    out += [("q1_spearman_early_vs_later", "all", "rho", rho), ("q1_spearman_early_vs_later", "all", "p_value", p),
            ("q1_spearman_early_vs_later", "all", "n", len(d))]
    # Within launch-volume bands, to separate "liked" from "big launch".
    for band, g in d.groupby("early_volume_band"):
        if len(g) >= MIN_N:
            r, pv = stats.spearmanr(g.early_positive_ratio_30d, g.reviews_d31_365)
            out += [("q1_spearman_early_vs_later", band, "rho", r), ("q1_spearman_early_vs_later", band, "p_value", pv),
                    ("q1_spearman_early_vs_later", band, "n", len(g))]
    # OLS: log later reviews ~ early ratio + log early volume + price + year + primary tag (top 15)
    if len(d) < MIN_N_OLS:
        return out
    try:
        import statsmodels.formula.api as smf
        top_tags = d.primary_tag.value_counts().index[:15]
        d["tag"] = np.where(d.primary_tag.isin(top_tags), d.primary_tag, "Other")
        d["log_later"] = np.log1p(d.reviews_d31_365)
        d["log_early"] = np.log1p(d.early_reviews_30d)
        d["price"] = d.launch_price_usd.fillna(0)
        m = smf.ols("log_later ~ early_positive_ratio_30d + log_early + price + C(launch_year) + C(tag)",
                    data=d).fit(cov_type="HC3")
        out += [("q1_ols_log_later_reviews", "all", "coef_early_ratio", m.params["early_positive_ratio_30d"]),
                ("q1_ols_log_later_reviews", "all", "p_early_ratio", m.pvalues["early_positive_ratio_30d"]),
                ("q1_ols_log_later_reviews", "all", "coef_log_early_volume", m.params["log_early"]),
                ("q1_ols_log_later_reviews", "all", "r2", m.rsquared),
                ("q1_ols_log_later_reviews", "all", "n", m.nobs)]
    except Exception as exc:  # noqa: BLE001
        log.warning("Q1 regression skipped: %s", exc)
    return out


def q3_price(df: pd.DataFrame) -> list[tuple]:
    d = df[df.in_analytical_scope & (df.price_bucket_id >= 0)]
    groups = [g.total_reviews.dropna().values for _, g in d.groupby("price_bucket_id") if len(g) >= MIN_N]
    if len(groups) < 2:
        return []
    h, p = stats.kruskal(*groups)
    return [("q3_kruskal_reviews_by_price", "all", "h", h), ("q3_kruskal_reviews_by_price", "all", "p_value", p)]


def q4_engagement(df: pd.DataFrame) -> list[tuple]:
    d = df[df.in_analytical_scope & df.median_playtime_h.notna() & df.est_owners_mid.notna()]
    if len(d) < MIN_N:
        return []
    rho, p = stats.spearmanr(d.median_playtime_h, d.est_owners_mid)
    return [("q4_spearman_playtime_vs_owners", "all", "rho", rho),
            ("q4_spearman_playtime_vs_owners", "all", "p_value", p),
            ("q4_spearman_playtime_vs_owners", "all", "n", len(d))]


def market_concentration(df: pd.DataFrame) -> list[tuple]:
    """How hit-driven is the indie market? (all indie games launched 2018+, any review count)"""
    r = df[(df.launch_year >= 2018)].total_reviews.fillna(0).sort_values().values
    if len(r) < MIN_N or r.sum() == 0:
        return []
    n = len(r)
    gini = (2 * np.arange(1, n + 1) - n - 1).dot(r) / (n * r.sum())
    top1 = np.sort(r)[::-1][: max(1, n // 100)].sum() / r.sum()
    return [("market_concentration", "all", "gini_reviews", gini),
            ("market_concentration", "all", "top1pct_share_of_reviews", top1),
            ("market_concentration", "all", "share_under_10_reviews", float((r < 10).mean())),
            ("market_concentration", "all", "n_games", n)]


def segment_effects(df: pd.DataFrame) -> list[tuple]:
    """Hit rate (>=1k reviews) for Early Access vs not, self-published vs not."""
    d = df[df.in_analytical_scope]
    out = []
    for col in ("was_early_access", "is_self_published"):
        for val, g in d.groupby(d[col].fillna(False)):
            out.append((f"hit_rate_by_{col}", str(bool(val)).lower(), "hit_rate_1k", float(g.is_hit_1k.mean())))
            out.append((f"hit_rate_by_{col}", str(bool(val)).lower(), "n", len(g)))
    return out


def run_all(engine: Engine) -> dict:
    df = _games(engine)
    rows = []
    for fn in (q1_early_sentiment, q3_price, q4_engagement, market_concentration, segment_effects):
        try:
            rows += fn(df)
        except Exception as exc:  # noqa: BLE001 - one failed analysis must not block the others
            log.warning("%s failed: %s", fn.__name__, exc)
    with engine.begin() as c:
        c.execute(text("DELETE FROM mart.analysis_results"))
        if rows:
            c.execute(text("""INSERT INTO mart.analysis_results (analysis, segment, metric, value)
                              VALUES (:a, :s, :m, :v)"""),
                      [{"a": a, "s": s, "m": m, "v": None if v is None or pd.isna(v) else float(v)}
                       for a, s, m, v in rows])
    return {"rows": len(rows), "games": len(df)}
