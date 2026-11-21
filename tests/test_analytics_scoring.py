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
    assert all(0 <= score <= 100 for score in result["overall_score"])
    assert result.filter(pl.col("security_id") == "US:B")["data_completeness"][0] == 0.5
