from datetime import date

import duckdb
import pytest

from finlab.analytics.financial_context import (
    archived_factor_evidence,
    company_events,
    company_history,
    company_peers,
)


def test_company_history_ignores_future_cutoffs_and_keeps_missing_context_explicit():
    comparison = {
        "history": [
            {"as_of": "2020-01-01", "value": 15.0},
            {"as_of": "2020-02-01", "value": 20.0},
            {"as_of": "2021-01-01", "value": 999.0},
        ],
        "current": 25.0,
        "historical_median": 17.5,
        "historical_percentile": 100,
    }
    result = company_history({"trailing_pe": comparison}, date(2020, 3, 1))["trailing_pe"]
    assert result["maximum"] == 20
    assert result["observations"] == 2
    assert result["median_relation"] == "above"
    assert result["recent_change"] == 5
    assert result["observed_level_dispersion"] is None


def test_company_timeline_filters_future_filing_and_verified_time_and_labels_ex_date():
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE security(security_id VARCHAR, cik INTEGER)")
        con.execute("INSERT INTO security VALUES ('A', 1)")
        con.execute("""CREATE TABLE filing(cik INTEGER,form VARCHAR,filed_date DATE,
            accession_number VARCHAR,primary_doc_url VARCHAR,accepted_at_status VARCHAR,accepted_at_utc TIMESTAMPTZ)""")
        con.execute("""INSERT INTO filing VALUES
            (1,'10-Q','2020-06-01','same-day',NULL,'verified_sgml','2020-06-02 01:00:00+00'),
            (1,'8-K','2020-06-01','after-cutoff',NULL,'verified_sgml','2020-06-02 05:00:00+00'),
            (1,'8-K','2020-06-01','unverified',NULL,'unverified_sec_json','2020-06-02 05:00:00+00'),
            (1,'8-K','2020-06-02','future',NULL,'unverified',NULL)""")
        con.execute(
            "CREATE TABLE corporate_action(security_id VARCHAR,ex_date DATE,action_type VARCHAR,source VARCHAR,factor DOUBLE,amount DOUBLE)"
        )
        con.execute(
            "INSERT INTO corporate_action VALUES ('A','2020-05-30','split','yahoo',2,NULL), ('A','2020-06-10','split','yahoo',2,NULL)"
        )
        events = company_events(con, "A", date(2020, 6, 1), {"filing", "corporate_action"})
    assert {event["evidence"] for event in events} == {"same-day", "unverified", None}
    by_evidence = {event["evidence"]: event for event in events}
    assert by_evidence["same-day"]["publication_timestamp"] is not None
    assert by_evidence["unverified"]["publication_timestamp"] is None
    assert by_evidence[None]["timing"] == "ex_date_only"
    assert by_evidence[None]["split_factor"] == pytest.approx(2)


def test_research_archive_with_later_outcomes_is_not_available_at_earlier_cutoff(monkeypatch):
    monkeypatch.setattr(
        "finlab.analytics.validation_view.read_validation",
        lambda _: {"version": "signal-research-v3.0", "end": "2022-12-30"},
    )
    assert archived_factor_evidence(date(2020, 12, 31)) == {
        "status": "outcomes_after_assessment",
        "factors": {},
    }


def test_company_context_survives_a_missing_or_invalid_optional_study(monkeypatch):
    path = "finlab.analytics.validation_view.read_validation"
    cutoff = date(2023, 1, 3)
    unavailable = {"status": "archive_unavailable", "factors": {}}
    monkeypatch.setattr(path, lambda _: None)
    assert archived_factor_evidence(cutoff) == unavailable
    report = {
        "version": "signal-research-v3.0",
        "end": "2022-12-30",
        "summary": {
            "21": {
                "factors": {
                    "trailing_pe": {"ic": {"mean": 0.02, "observations": 35}, "holm_p_value": 0.9},
                }
            }
        },
    }
    monkeypatch.setattr(path, lambda _: report)
    evidence = archived_factor_evidence(cutoff)
    assert evidence["status"] == "retrospective"
    assert evidence["factors"]["trailing_pe"]["observations"] == 35
    assert evidence["factors"]["trailing_pe"]["interval"] is None
    report["summary"] = {}
    assert archived_factor_evidence(cutoff) == unavailable


def test_peers_require_a_current_classification_and_eight_other_valid_companies():
    def issuer(index, pe):
        return dict(
            security_id=str(index),
            eligible=True,
            financial_issuer=False,
            fundamental_factors={"factors": {"trailing_pe": {"value": pe}}},
            market_factors=[],
        )

    subject = issuer(0, 40.0)
    cohort = [subject, *(issuer(i, float(i * 10)) for i in range(1, 9))]
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE price_daily AS SELECT DATE '2020-12-31' AS date")
        con.execute(
            "CREATE TABLE security AS SELECT i::VARCHAR AS security_id, i AS cik FROM range(9) t(i)"
        )
        con.execute("""CREATE TABLE entity AS
                       SELECT i AS cik, CASE WHEN i=8 THEN 'Hardware' ELSE 'Software' END AS industry,
                              'Technology' AS sector FROM range(9) t(i)""")
        assert company_peers(con, subject, cohort, date(2020, 12, 30))["status"] == (
            "historical_classifications_unavailable"
        )
        latest = company_peers(con, subject, cohort, date(2020, 12, 31))
        assert latest["status"] == "current_classifications"
        assert latest["comparisons"]["trailing_pe"] == {
            "cohort": "sector",
            "label": "Technology",
            "count": 8,
            "median": 45,
            "percentile": 43.75,
        }
        cohort[-1]["eligible"] = False
        assert company_peers(con, subject, cohort, date(2020, 12, 31))["status"] == (
            "insufficient_valid_peers"
        )
