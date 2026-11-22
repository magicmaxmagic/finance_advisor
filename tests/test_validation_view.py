from copy import deepcopy
from pathlib import Path

import pytest

from finlab.analytics.validation_view import evidence_state, summarize_validation, validation_series


@pytest.mark.parametrize(
    ("count", "interval", "p", "expected"),
    [
        (9, [0.1, 0.3], 0.001, "insufficient"),
        (35, [-0.1, 0.3], 0.001, "inconclusive"),
        (35, [0.1, 0.3], 0.1, "inconclusive"),
        (35, [0.1, 0.3], 0.01, "positive"),
        (35, [-0.3, -0.1], 0.01, "negative"),
    ],
)
def test_evidence_state_requires_both_interval_and_family_adjustment(count, interval, p, expected):
    ic = {"observations": count, "effective_n": count, "confidence_95": interval}
    result = evidence_state(ic, p, walk_forward=True)
    assert result["status"] == expected
    if expected in {"positive", "negative"}:
        assert "out-of-sample" in result["label"]


def test_summary_uses_calculated_evidence_for_each_horizon():
    study = {
        "universe_ids": ["A", "B"],
        "evidence_status": "hardcoded conclusion to ignore",
        "summary": {
            "21": {
                "models": {
                    "opportunity": {
                        "ic": {
                            "observations": 30,
                            "effective_n": 22,
                            "confidence_95": [-0.01, 0.08],
                            "p_value": 0.4,
                        }
                    }
                }
            },
            "252": {"models": {"opportunity": {"ic": {"observations": 2}}}},
        },
    }
    result = summarize_validation(study)
    assert result["headline"]["status"] == "inconclusive"
    assert result["model_evidence"]["252"]["opportunity"]["status"] == "insufficient"
    assert result["universe_count"] == 2
    assert "evidence_status" not in result


@pytest.fixture
def archived_report():
    return {
        "version": "signal-research-v3.0",
        "summary": {"21": {"models": {"opportunity": {}}}},
        "folds": {
            "21": [
                {
                    "signal_date": "2020-01-02",
                    "entry_date": "2020-01-03",
                    "exit_date": "2020-01-06",
                    "models": {
                        "opportunity": {"rank_ic": 0.2, "daily": [0.10, -0.10], "top_turnover": 2.0}
                    },
                    "spy_daily": [0.01, 0.01],
                }
            ]
        },
    }


def test_timeseries_archives_daily_benchmark_and_refuses_missing_sessions(archived_report):
    result = validation_series(archived_report, 21, "opportunity")
    assert [row["date"] for row in result["portfolio"]] == ["2020-01-03", "2020-01-06"]
    assert result["portfolio"][-1]["wealth"] == pytest.approx(99)
    assert result["portfolio"][-1]["drawdown"] == pytest.approx(-10)
    assert result["portfolio"][-1]["benchmark"] == pytest.approx(102.01)
    incomplete = deepcopy(archived_report)
    incomplete["folds"]["21"][0]["spy_daily"].pop()
    assert validation_series(incomplete, 21, "opportunity")["portfolio"] == []


def test_local_api_series_projects_only_requested_archive(archived_report, monkeypatch, tmp_path):
    import json

    from fastapi.testclient import TestClient

    from finlab.api.app import app

    monkeypatch.setattr(
        "finlab.config.get_settings", lambda: type("Settings", (), {"reports_dir": tmp_path})()
    )
    directory: Path = tmp_path / "research"
    directory.mkdir()
    (directory / "validation-v3-latest.json").write_text(json.dumps(archived_report))
    client = TestClient(app)
    assert "folds" not in client.get("/api/analytics/model-validation").json()
    response = client.get("/api/analytics/model-validation/series?horizon=21")
    assert response.status_code == 200, response.text
    assert response.json()["windows"][0]["ic"] == 0.2
    assert (
        client.get("/api/analytics/model-validation/series?horizon=21&series=unknown").status_code
        == 404
    )
    assert "reason" in client.get("/api/analytics/model-validation?study=v6").json()
    holdout = {
        **deepcopy(archived_report),
        "version": "signal-research-v6.1",
        "test_start": "2020-01-02",
    }
    holdout["folds"]["21"][0]["models"]["opportunity"]["rank_ic"] = -0.15
    (directory / "validation-v6-latest.json").write_text(json.dumps(holdout))
    assert (
        client.get("/api/analytics/model-validation?study=v6").json()["test_start"] == "2020-01-02"
    )
    assert (
        client.get("/api/analytics/model-validation/series?study=v6").json()["windows"][0]["ic"]
        == -0.15
    )
    assert client.get("/api/analytics/model-validation/series").json()["windows"][0]["ic"] == 0.2
