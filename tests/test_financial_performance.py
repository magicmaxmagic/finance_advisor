from datetime import date

import duckdb
import pytest
from fastapi.testclient import TestClient

from finlab.analytics.financial_performance import _read_statements, reconcile_statement
from finlab.api.app import app
from finlab.engine.snapshot import REQUIRED_DATASETS, publish_snapshot
from finlab.ingest.financial_events import stage_financial_events, validate_event

QUARTER = {
    "Revenues": [100],
    "CostOfRevenue": [40],
    "ResearchAndDevelopmentExpense": [7],
    "SellingGeneralAndAdministrativeExpense": [13],
    "CostsAndExpenses": [60],
    "OperatingIncomeLoss": [40],
    "NonoperatingIncomeExpense": [-10],
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": [
        30
    ],
    "IncomeTaxExpenseBenefit": [5],
    "ProfitLoss": [25],
    "NetIncomeLoss": [23],
}
EVENT = {
    "cik": 7,
    "kind": "earnings_call",
    "label": "Q3 earnings call",
    "event_date": "2025-10-28",
    "announced_on": "2025-10-06",
    "status": "confirmed",
    "source": "Issuer IR",
    "source_url": "https://example.com/calendar",
    "event_at": "2025-10-28T13:30:00-07:00",
    "timezone": "America/Los_Angeles",
    "occurred_on": "2025-10-28",
    "occurrence_url": "https://example.com/recording",
    "statement_period_end": "2025-09-30",
}


def test_statement_uses_consolidated_profit_and_reconciled_flows():
    result = reconcile_statement(QUARTER)
    assert result["chart_status"] == "available"
    assert result["values"]["net_income"]["value"] == 25
    assert result["values"]["gross_profit"]["value"] == 60
    assert result["margins"]["operating_income"] == pytest.approx(0.40)
    assert result["expense_components"] == ["research", "selling_general_admin"]
    assert all(row["difference"] in {0, None} for row in result["checks"])
    assert {
        "source": "operating_income",
        "target": "nonoperating_loss",
        "amount": 10,
        "tone": "cost",
    } in result["links"]


def test_incomplete_inconsistent_and_loss_statements_keep_signed_values():
    missing = reconcile_statement(
        {key: value for key, value in QUARTER.items() if key != "CostOfRevenue"}
    )
    assert missing["chart_status"] == "incomplete"
    assert "cost_of_revenue" in missing["missing"]
    assert missing["links"] == []
    mismatch = reconcile_statement({**QUARTER, "IncomeTaxExpenseBenefit": [15]})
    assert mismatch["chart_status"] == "unreconciled"
    assert mismatch["links"] == []
    loss = reconcile_statement({**QUARTER, "IncomeTaxExpenseBenefit": [35], "ProfitLoss": [-5]})
    assert loss["chart_status"] == "signed_statement"
    assert loss["values"]["net_income"]["value"] == -5


def test_future_event_requires_attributed_source_and_timestamp_zone():
    raw = {**EVENT, "timezone": None}
    with pytest.raises(ValueError, match="timezone"):
        validate_event(raw)
    with pytest.raises(ValueError, match="HTTPS"):
        validate_event({**EVENT, "source_url": "http://example.com/calendar"})
    with pytest.raises(ValueError, match="Occurrence"):
        validate_event({**EVENT, "occurred_on": "2025-10-26"})


def test_offline_api_events_and_filings_respect_publication_cutoff(settings, monkeypatch):
    import socket

    cfg = settings
    monkeypatch.setattr("finlab.config.get_settings", lambda: cfg)

    def deny(*args, **kwargs):
        raise AssertionError("Analytics attempted external network access")

    monkeypatch.setattr(socket.socket, "connect", deny)
    with duckdb.connect() as con:
        con.execute("CREATE TABLE entity AS SELECT 7 AS cik")
        con.execute(
            "CREATE TABLE security AS SELECT 7 AS cik, 'TEST' AS ticker, 'TEST:7' AS security_id"
        )
        con.execute("CREATE TABLE price_daily AS SELECT 'TEST:7' AS security_id")
        con.execute("""CREATE TABLE filing AS SELECT 7 AS cik, 'original' AS accession_number, '10-Q' AS form,
            DATE '2025-10-29' AS filed_date, DATE '2025-09-30' AS period_of_report,
            'https://example.com/10q' AS primary_doc_url,
            'verified_sgml' AS accepted_at_status, TIMESTAMPTZ '2025-10-30 01:30:00+00' AS accepted_at_utc""")
        con.execute("""CREATE TABLE fact (cik INTEGER, period_start DATE, period_end DATE, duration_bucket VARCHAR,
            accession_number VARCHAR, filed_date DATE, taxonomy VARCHAR, unit VARCHAR, tag VARCHAR, value DOUBLE)""")
        con.executemany(
            """INSERT INTO fact VALUES (7, DATE '2025-07-01', DATE '2025-09-30', 'quarter',
            'original', DATE '2025-10-29', 'us-gaap', 'USD', ?, ?)""",
            [(tag, values[0]) for tag, values in QUARTER.items()],
        )
        # Other starts with no revenue (such as nine-month capex) cannot be mixed.
        con.execute("""INSERT INTO fact VALUES (7, DATE '2025-01-01', DATE '2025-09-30', 'annual',
            'original', DATE '2025-10-29', 'us-gaap', 'USD', 'CostOfRevenue', 900)""")
        for name in (*REQUIRED_DATASETS, "fact", "price_daily"):
            root = cfg.warehouse_dir / name
            root.mkdir(parents=True)
            source = (
                f"TABLE {name}"
                if name in {"entity", "security", "filing", "fact", "price_daily"}
                else "SELECT 1"
            )
            con.execute(f"COPY ({source}) TO '{root}/part.parquet' (FORMAT PARQUET)")
        count = stage_financial_events(con, cfg, [EVENT])
        assert count == 1
        stage_financial_events(con, cfg, [EVENT])
        assert len(_read_statements(con, 7, date(2025, 10, 29), {"filing", "fact"})) == 1
    publication = publish_snapshot(warehouse_dir=cfg.warehouse_dir, schema_version=5)
    client = TestClient(app)
    endpoint = "/api/analytics/financial-performance/TEST"
    scheduled = client.get(
        endpoint, params={"as_of": "2025-10-07", "epoch": publication.epoch}
    ).json()
    assert scheduled["statements"] == []
    assert scheduled["events"][0]["status"] == "confirmed"
    assert scheduled["events"][0]["source_url"] == EVENT["source_url"]
    before_announcement = client.get(endpoint, params={"as_of": "2025-10-05"}).json()
    assert before_announcement["events"] == []
    before_filing = client.get(endpoint, params={"as_of": "2025-10-28"}).json()
    assert before_filing["statements"] == []
    assert before_filing["events"][0]["status"] == "historical"
    assert before_filing["events"][0]["statement_id"] is None
    response = client.get(endpoint, params={"as_of": "2025-10-29"})
    assert response.status_code == 200
    report = response.json()
    assert report["statements"][0]["chart_status"] == "available"
    assert report["events"][-1]["statement_id"] == "quarter:2025-09-30"
