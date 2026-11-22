"""Offline tests for robust multifactor scoring."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from finlab.analytics.scoring import robust_zscore, score_universe, winsorize


def test_winsorize_and_robust_zscore_are_bounded():
    values = [0.0, 1.0, 2.0, 100.0]
    capped = winsorize(values)
    assert max(capped) == 100.0
    assert len(robust_zscore(capped)) == 4


def test_score_universe_rejects_invalid_weights():
    frame = pl.DataFrame(
        {
            "security_id": ["US:A", "US:B"],
            "factor": ["return_12m", "return_12m"],
            "value": [0.1, 0.2],
        }
    )
    with pytest.raises(ValueError, match="sum to 1"):
        score_universe(frame, {"return_12m": 0.5}, date(2026, 11, 21))


def test_score_universe_preserves_missing_factors_and_explanation():
    frame = pl.DataFrame(
        {
            "security_id": ["US:A", "US:A", "US:B", "US:B"],
            "factor": ["momentum", "quality", "momentum", "quality"],
            "value": [0.2, 0.8, 0.1, None],
        }
    )
    result = score_universe(
        frame,
        {"momentum": 0.6, "quality": 0.4},
        date(2026, 11, 21),
    )

    assert set(result["security_id"]) == {"US:A", "US:B"}
    assert result.filter(pl.col("security_id") == "US:A")["overall_score"][0] is not None
    assert result.filter(pl.col("security_id") == "US:B")["overall_score"][0] is None
    assert result.filter(pl.col("security_id") == "US:B")["data_status"][0] == "insufficient_data"
    assert result.filter(pl.col("security_id") == "US:B")["data_completeness"][0] == 0.5


def test_even_sample_median_and_duplicate_vintages():
    assert robust_zscore([1.0, 3.0]) == pytest.approx([-1 / 1.4826, 1 / 1.4826])
    frame = pl.DataFrame({"security_id": ["A", "A"], "factor": ["x", "x"], "value": [1.0, 2.0]})
    with pytest.raises(ValueError, match="Duplicate"):
        score_universe(frame, {"x": 1.0}, date(2024, 1, 1))


def test_sector_normalization_does_not_confuse_sector_levels_with_stock_rank():
    frame = pl.DataFrame(
        {
            "security_id": ["A", "B", "C", "D", "E", "F"],
            "sector": ["Tech"] * 3 + ["Banks"] * 3,
            "factor": ["quality"] * 6,
            "value": [1.0, 2.0, 3.0, 10.0, 20.0, 30.0],
        }
    )
    scored = score_universe(frame, {"quality": 1.0}, date(2020, 1, 1), sector_neutral=True)
    a = scored.filter(pl.col("security_id") == "A")["overall_score"][0]
    d = scored.filter(pl.col("security_id") == "D")["overall_score"][0]
    assert a == pytest.approx(d)
