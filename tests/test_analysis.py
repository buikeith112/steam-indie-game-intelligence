import numpy as np
import pandas as pd
import pytest

pytest.importorskip("scipy")
pytest.importorskip("statsmodels")

from steam_intel import analysis


def _synthetic(n=400, seed=0):
    rng = np.random.default_rng(seed)
    early_ratio = rng.uniform(0.4, 1.0, n)
    early_n = rng.integers(10, 500, n)
    later = np.exp(1.5 * early_ratio + 0.9 * np.log1p(early_n) + rng.normal(0, 0.5, n)).round()
    return pd.DataFrame({
        "app_id": range(n), "launch_year": rng.integers(2018, 2025, n),
        "primary_tag": rng.choice(["Roguelike", "Puzzle", "Horror", "Platformer"], n),
        "price_bucket_id": rng.integers(0, 7, n), "is_free": False, "in_analytical_scope": True,
        "was_early_access": rng.random(n) < 0.3, "is_self_published": rng.random(n) < 0.5,
        "total_reviews": later + early_n, "positive_ratio": early_ratio,
        "early_reviews_30d": early_n, "early_positive_ratio_30d": early_ratio,
        "early_volume_band": np.where(early_n < 50, "10-49", np.where(early_n < 250, "50-249", "250+")),
        "reviews_d31_365": later, "launch_price_usd": rng.uniform(1, 30, n),
        "median_playtime_h": rng.uniform(0.5, 40, n), "est_owners_mid": later * 40,
        "is_hit_1k": (later + early_n) >= 1000,
    })


def test_q1_detects_positive_effect():
    rows = {(a, s, m): v for a, s, m, v in analysis.q1_early_sentiment(_synthetic())}
    assert rows[("q1_spearman_early_vs_later", "all", "rho")] > 0
    assert rows[("q1_ols_log_later_reviews", "all", "coef_early_ratio")] == pytest.approx(1.5, abs=0.4)
    assert rows[("q1_ols_log_later_reviews", "all", "p_early_ratio")] < 0.01


def test_market_concentration_bounds():
    rows = {m: v for _, _, m, v in analysis.market_concentration(_synthetic())}
    assert 0 <= rows["gini_reviews"] <= 1 and 0 < rows["top1pct_share_of_reviews"] < 1


def test_small_sample_is_skipped():
    rows = analysis.q1_early_sentiment(_synthetic(n=10))
    assert rows == [("q1_sample", "all", "n", 10)]
