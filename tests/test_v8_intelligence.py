"""Point-in-time calendar, offline sentiment, and held-out decision contracts."""

from contextlib import contextmanager
from datetime import date, timedelta

import duckdb
import pytest
from fastapi.testclient import TestClient

from finlab.analytics import event_calendar
from finlab.analytics.decision_context import decision_context
from finlab.analytics.sentiment import classify_tone, sentiment_context
from finlab.api.app import app
from finlab.ingest.financial_text import earnings_text
from finlab.ingest.universe import Company
from finlab.research.protocol_v8 import historical_ttm_percentile, operating_ebitda_growth


def test_research_factors_use_strictly_earlier_and_same_filing_inputs():
    at = date(2026, 1, 1)
    history = [
        (at - timedelta(days=index * 30), value)
        for index, value in enumerate(range(10, 18), start=1)
    ]
    assert historical_ttm_percentile(
        14, [*history, (at, 500), (at + timedelta(1), 900)], at
    ) == pytest.approx(4.5 / 8)
    assert historical_ttm_percentile(14, history[:-1], at) is None

    filing_rows = [
        {
            "canonical": tag,
            "accession_number": accession,
            "period_start": f"{year}-01-01",
            "period_end": f"{year}-12-31",
            "value": value,
        }
        for year, accession, income, depreciation in [(2024, "old", 40, 10), (2025, "new", 45, 15)]
        for tag, value in [
            ("operating_income", income),
            ("depreciation_amortization", depreciation),
        ]
    ]
    assert operating_ebitda_growth(filing_rows, "2025-12-31") == pytest.approx(0.2)
    filing_rows[-1]["accession_number"] = "different"
    assert operating_ebitda_growth(filing_rows, "2025-12-31") is None


def test_decision_research_keeps_directions_and_unknowns_separate():
    firm = {
        "market_ready": True,
        "assessment": {
            "fundamental_score": 74,
            "technical_score": 55,
            "market_risk_score": 44,
            "financial_risk_score": 60,
            "contradictions": [],
            "score_groups": {
                "fundamental": {
                    "business_quality": 42,
                    "valuation": 72,
                    "growth": 82,
                    "cash_flow_quality": 53,
                }
            },
        },
    }
    result = decision_context(firm, {"price_to_sma200": -0.3, "margin_change": -0.1}, None)
    assert result["conditions"]["entry"]["observed"] is False
    assert result["conditions"]["exit_review"]["observed"] is True
    assert result["conditions"]["entry"]["history"]["long"]["status"] == "insufficient"
    assert result["dimensions"]["business_quality"] != result["dimensions"]["valuation"]
    firm["market_ready"] = False
    result = decision_context(firm, {"price_to_sma200": 0.3, "margin_change": -0.1}, None)
    assert result["conditions"]["entry"]["observed"] is None
    assert result["conditions"]["entry"]["missing"] == ["price_to_sma200"]


def test_calendar_confirmation_and_statement_link_follow_availability(monkeypatch):
    con = duckdb.connect()
    con.execute("""CREATE TABLE filing AS SELECT 1 AS cik, 'filing' AS accession_number,
        DATE '2026-10-29' AS filed_date, DATE '2026-09-30' AS period_of_report,
        '10-Q' AS form, 'https://www.sec.gov/10q' AS primary_doc_url""")
    con.execute("""CREATE TABLE fact AS SELECT 1 AS cik, 'filing' AS accession_number,
        DATE '2026-10-29' AS filed_date, DATE '2026-09-30' AS period_end, 'quarter' AS duration_bucket""")
    con.execute("""CREATE TABLE financial_event (event_id VARCHAR,cik INT,kind VARCHAR,label VARCHAR,
        event_date VARCHAR,event_at VARCHAR,timezone VARCHAR,source VARCHAR,source_url VARCHAR,status VARCHAR,
        occurred_on VARCHAR,occurrence_url VARCHAR,statement_period_end VARCHAR,announced_on VARCHAR)""")
    con.execute("""INSERT INTO financial_event VALUES
        ('q3',1,'earnings_call','Call','2026-10-28','2026-10-28T13:30:00-07:00','America/Los_Angeles',
         'Issuer','https://example.com/scheduled','confirmed','2026-10-28','https://example.com/call','2026-09-30','2026-10-02'),
        ('est',1,'investor_event','Investor day','2026-10-25',NULL,NULL,'Issuer','https://example.com/event',
         'estimated',NULL,NULL,NULL,'2026-10-12')""")

    @contextmanager
    def published():
        yield con

    monkeypatch.setattr(event_calendar, "published_read_conn", published)
    monkeypatch.setattr(
        event_calendar, "load_poc_universe", lambda: (Company(1, "ABC", "Tech", "Issuer"),)
    )
    client = TestClient(app)
    parameters = {
        "start": "2026-10-01",
        "end": "2026-10-31",
        "ticker": "ABC",
        "kind": "earnings_call",
    }

    def calendar(cutoff):
        response = client.get("/api/analytics/calendar", params={**parameters, "as_of": cutoff})
        assert response.status_code == 200, response.text
        return response.json()["events"]

    assert calendar("2026-10-01") == []
    (scheduled,) = calendar("2026-10-07")
    assert scheduled["status"] == "confirmed" and scheduled["timezone"] == "America/Los_Angeles"
    (held,) = calendar("2026-10-28")
    assert held["status"] == "historical" and held["statement_period_end"] is None
    (linked,) = calendar("2026-10-29")
    assert linked["statement_period_end"] == "2026-09-30"
    assert (
        client.get("/api/analytics/calendar", params={**parameters, "ticker": "BAD"}).status_code
        == 422
    )
    con.close()


def test_local_sentiment_is_scoped_to_text_and_cutoff():
    narrative = earnings_text(b"""<p>Management reported improved profitability in the quarter and commented that the company
        strengthened its operating performance.</p><p>Forward-looking statements</p><p>Significant impairment.</p>""")
    assert classify_tone(narrative)["tone"] == "positive"
    assert (
        classify_tone("The company did not improve. It was not profitable.")["tone"] == "negative"
    )
    assert classify_tone("Management reported results.")["tone"] == "insufficient"
    con = duckdb.connect()
    con.execute("CREATE TABLE security AS SELECT 1 AS cik, 'ABC' AS security_id")
    con.execute(
        """CREATE TABLE financial_text AS SELECT 1 AS cik,'2026-10-28' AS available_on,
        'SEC EDGAR' AS source, 'https://www.sec.gov/p' AS source_url, 'Earnings' AS title,
        'earnings_release' AS event_kind,'file' AS document_sha256,'body' AS text_sha256,
        'v1' AS version, ? AS text""",
        [narrative],
    )
    assert (
        sentiment_context(con, "ABC", date(2026, 10, 27), {"financial_text"})["status"]
        == "unavailable"
    )
    observed = sentiment_context(con, "ABC", date(2026, 11, 20), {"financial_text"})
    assert observed["attention_30d"] == 1 and observed["balance_change"] is None
    assert observed["documents"][0]["tone"] == "positive"
    con.close()
