from datetime import date

import pytest

from finlab.research.signal_statistics import holm, summarize
from finlab.research.validation_v3 import CANDIDATES, choose_weights, eps_growth


def test_bootstrap_reports_reproducible_interval_and_missing_sample_guard():
    values = [0.02 + (i % 7 - 3) * 0.01 for i in range(35)]
    output = summarize(values)
    assert output == summarize(values)
    assert output["observations"] == 35
    assert 0 < output["effective_n"] <= 35
    assert output["confidence_95"][0] < output["mean"] < output["confidence_95"][1]
    assert summarize(values[:11])["confidence_95"] is None
    assert summarize([None] * 18 + values[:20])["confidence_95"] is None
    assert holm({"a": 0.01, "b": 0.04, "c": None}) == {
        "a": pytest.approx(0.03),
        "b": pytest.approx(0.08),
        "c": None,
    }


def test_walk_forward_uses_only_mature_disjoint_labels_and_defaults_to_simple_model():
    folds = []
    for i in range(35):
        start = i * 21
        folds.append(
            dict(
                signal_index=start,
                exit_index=start + 21,
                signal_date=str(start),
                exit_date=str(start + 21),
                models={
                    key: {"top_excess_net": (i % 4) / 1000} for key in (*CANDIDATES, "fundamental")
                },
            )
        )
    assert (
        choose_weights(folds, 300, train=12, validation=12, embargo=1, rolling=False)["selected"]
        is None
    )
    result = choose_weights(folds, 600, train=12, validation=12, embargo=2, rolling=True)
    assert result["selected"] == "fundamental"
    assert result["train_windows"] == result["validation_windows"] == 12
    assert int(result["train_end"]) + 2 < int(result["validation_start"])
    assert int(result["validation_end"]) + 2 < 600


def test_eps_growth_requires_comparable_reported_share_basis():
    facts = [
        dict(
            canonical="eps_diluted",
            value=value,
            period_start=str(date(year, 1, 1)),
            period_end=str(date(year, 12, 31)),
            accession_number="same",
        )
        for value, year in ((4, 2021), (2, 2020))
    ]
    row = {"fundamental_factors": {"filing_evidence": facts}}
    assert eps_growth(row) == 1
    facts[0]["accession_number"] = "different"
    assert eps_growth(row) is None
