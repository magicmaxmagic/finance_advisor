"""End-to-end published-history recommendations with network access forbidden."""

import json
import socket
from datetime import date, timedelta

import polars as pl
import pytest

from finlab.analytics.recommendations import archive_recommendations, recommendation_report
from finlab.db.conn import write_conn
from finlab.engine.prepare import prepare_warehouse
from finlab.ingest.warehouse import publish_table


@pytest.fixture
def historical_warehouse(initialised, monkeypatch):
    profile = initialised.data_dir / "fixture-universe.yaml"
    profile.write_text(
        json.dumps(
            {
                "companies": [
                    {
                        "cik": index,
                        "ticker": ticker,
                        "sector": "Technology",
                        "name": ticker,
                        "security_id": ticker,
                    }
                    for index, ticker in ((1, "AAA"), (2, "BBB"), (3, "NEW"))
                ]
            }
        )
    )
    initialised.universe_path = profile
    monkeypatch.setattr("finlab.config.get_settings", lambda: initialised)
    # Two complete cross-sectional members plus one explicit insufficient member.
    with write_conn(initialised) as con:
        for cik, ticker in ((1, "AAA"), (2, "BBB"), (3, "NEW")):
            con.execute(
                "INSERT INTO entity(cik,name,sector) VALUES (?,?,?)", [cik, ticker, "Technology"]
            )
            con.execute(
                "INSERT INTO security(security_id,cik,ticker,currency,is_active) VALUES (?,?,?,'USD',TRUE)",
                [ticker, cik, ticker],
            )
            for tag, unit, value, filed in (
                ("Revenues", "USD", 1000.0, date(2020, 2, 1)),
                ("NetIncomeLoss", "USD", 100.0, date(2020, 2, 1)),
                (
                    "EarningsPerShareDiluted",
                    "USD/shares",
                    4.0 if cik == 1 else 2.0,
                    date(2020, 2, 1),
                ),
                ("EarningsPerShareDiluted", "USD/shares", 999.0, date(2021, 2, 1)),
            ):
                con.execute(
                    """INSERT INTO fact(fact_key,cik,taxonomy,tag,unit,period_start,period_end,
                    duration_bucket,value,accession_number,filed_date)
                    VALUES (?,?,'us-gaap',?,?,DATE '2019-01-01',DATE '2019-12-31','annual',?,?,?)""",
                    [
                        hash((cik, tag, filed)) % (2**62),
                        cik,
                        tag,
                        unit,
                        value,
                        f"{cik}-{filed}",
                        filed,
                    ],
                )
        con.execute("""INSERT INTO fact(fact_key,cik,taxonomy,tag,unit,period_start,period_end,duration_bucket,value,accession_number,filed_date)
            SELECT 100000+cik,cik,'us-gaap','Revenues','USD',DATE '2018-01-01',DATE '2018-12-31','annual',800.0,'prior-year',DATE '2019-02-01' FROM entity""")
        con.execute(
            "INSERT INTO security(security_id,ticker,currency,is_active) VALUES ('SPY','SPY','USD',TRUE)"
        )
        publish_table(con, source="fact", root=initialised.warehouse_dir / "fact")
        dates = []
        day = date(2019, 12, 1)
        while day <= date(2020, 12, 31):
            if day.weekday() < 5:
                dates.append(day)
            day += timedelta(days=1)
        records = [
            dict(security_id=ticker, date=day, close=100.0 + i * (0.2 if ticker == "AAA" else 0.1))
            for ticker in ("AAA", "BBB")
            for i, day in enumerate(dates)
        ]
        records += [dict(security_id="NEW", date=date(2020, 12, 31), close=100.0)]
        records += [dict(security_id="SPY", date=day, close=100.0) for day in dates]
        frame = pl.DataFrame(records).with_columns(
            pl.col("close").alias("open"),
            pl.col("close").alias("high"),
            pl.col("close").alias("low"),
            pl.lit(100).alias("volume"),
            pl.lit("yahoo").alias("source"),
            pl.col("close").alias("adjusted_close"),
            pl.lit("yahoo_auto_adjust_false").alias("adjustment_basis"),
        )
        con.register("fixture_prices", frame)
        publish_table(con, source="fixture_prices", root=initialised.warehouse_dir / "price_daily")
        con.execute(
            "INSERT INTO price_ingestion SELECT security_id,'yahoo',DATE '2018-01-01',DATE '2020-12-31',DATE '2020-12-31',CURRENT_TIMESTAMP FROM security"
        )
        prepare_warehouse(con, initialised, date(2020, 12, 31))
    return initialised


def test_historical_report_excludes_future_filings_and_is_offline(
    historical_warehouse, monkeypatch
):
    def forbid(*args, **kwargs):
        raise AssertionError("Analytics accessed network")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    report = recommendation_report(date(2020, 12, 31))
    assert len(report["rows"]) == 3
    assert next(row for row in report["rows"] if row["ticker"] == "NEW")["score"] is None
    aaa = next(row for row in report["rows"] if row["ticker"] == "AAA")
    assert aaa["earnings_yield"] == pytest.approx(4.0 / aaa["price"])
    assert aaa["score"] is not None
    assert aaa["factor_scores"]
    assert "999.0" not in json.dumps(report)
    assert report == recommendation_report(date(2020, 12, 31))
    path = archive_recommendations(report, historical_warehouse.reports_dir)
    assert path == archive_recommendations(report, historical_warehouse.reports_dir)
    assert json.loads(path.read_text()) == report


def test_default_weekly_is_warehouse_only(historical_warehouse, monkeypatch):
    from finlab.research.weekly_report import generate, read_brief

    def forbid(*args, **kwargs):
        raise AssertionError("Weekly report accessed network")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    brief = read_brief(generate(historical_warehouse, scheduled=date(2020, 12, 31)))
    assert brief["source"] == "Validated local warehouse"
    assert brief["rows"]


def test_forward_followup_uses_next_open_and_a_matched_benchmark(historical_warehouse):
    from finlab.analytics.evaluation import evaluate_recommendations

    report = {
        "as_of": "2020-12-28",
        "rows": [{"ticker": "AAA", "security_id": "AAA", "score": 60.0}],
    }
    result = evaluate_recommendations(report, 3)
    row = result["rows"][0]
    assert row["status"] == "observed"
    assert row["entry_date"] == "2020-12-29"
    assert row["exit_date"] == "2020-12-31"
    assert row["benchmark_return"] == 0
    assert row["total_return"] > 0
    assert row["net_return"] < row["total_return"]


def test_portfolio_and_strategy_apis_use_local_history(historical_warehouse, monkeypatch):
    from fastapi.testclient import TestClient

    from finlab.api.app import app

    def forbid(*args, **kwargs):
        raise AssertionError("Research API accessed network")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    client = TestClient(app)
    portfolio = client.post(
        "/api/portfolio/analyze",
        json={
            "csv_data": "date,security_id,side,quantity,price,currency,fx_to_base,fee\n2020-01-02,AAA,buy,10,100,USD,1,0\n",
            "as_of": "2020-12-31",
            "scenario_security": "BBB",
            "scenario_amount": 1000,
        },
    )
    assert portfolio.status_code == 200, portfolio.text
    result = portfolio.json()["result"]
    assert result["after"]["market_value"] - result["before"]["market_value"] == pytest.approx(1000)
    strategy = client.post(
        "/api/analytics/backtest",
        json={"start": "2020-02-01", "end": "2020-12-31", "tickers": ["AAA", "BBB"], "top_n": 2},
    )
    assert strategy.status_code == 200, strategy.text
    assert strategy.json()["metrics"]["cagr"] > 0
    invalid = client.post(
        "/api/analytics/backtest",
        json={"start": "2020-12-31", "end": "2020-01-01", "tickers": ["AAA"]},
    )
    assert invalid.status_code == 422


def test_company_api_resolves_canonical_facts_at_the_historical_cutoff(historical_warehouse):
    from fastapi.testclient import TestClient

    from finlab.api.app import app

    response = TestClient(app).get("/api/warehouse/assets/AAA?as_of=2020-12-31")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["as_of"] == "2020-12-31"
    eps = [r for r in payload["facts"] if r["tag"] == "eps_diluted"]
    assert eps[0]["value"] == 4.0
    assert all(row["filed_date"] <= "2020-12-31" for row in payload["facts"])
    assert all(row["date"] <= "2020-12-31" for row in payload["prices"])


def test_eps_is_converted_to_the_vendor_price_share_basis(historical_warehouse):
    from finlab.analytics.rankings import historical_rankings

    original = historical_rankings(date(2020, 12, 31))
    old_epoch = original["warehouse_epoch"]
    old_yield = next(row for row in original["rows"] if row["ticker"] == "AAA")["earnings_yield"]
    with write_conn(historical_warehouse) as con:
        con.execute(
            "INSERT INTO corporate_action(security_id,ex_date,action_type,factor,source) VALUES ('AAA',DATE '2021-06-01','split',2.0,'yahoo')"
        )
        con.execute(
            "UPDATE price_ingestion SET actions_checked_through=DATE '2022-01-01',price_basis_date=DATE '2022-01-01' WHERE security_id='AAA'"
        )
        prepare_warehouse(con, historical_warehouse, date(2020, 12, 31))
    revised = historical_rankings(date(2020, 12, 31))
    aaa = next(row for row in revised["rows"] if row["ticker"] == "AAA")
    assert aaa["eps_share_basis_factor"] == 2.0
    assert aaa["earnings_yield"] == pytest.approx(old_yield / 2)
    assert original == historical_rankings(date(2020, 12, 31), epoch=old_epoch)


def test_walk_forward_selects_on_validation_before_observing_test(historical_warehouse):
    from finlab.research.walk_forward import walk_forward

    calls = []

    def runner(start, end, tickers, *, strategy, epoch):
        calls.append((start, end, strategy, epoch))
        return {"metrics": {"sharpe": 2.0 if strategy == "momentum" else 1.0}}

    report = walk_forward(
        date(2020, 1, 1),
        date(2020, 12, 31),
        ["AAA"],
        train=60,
        validation=30,
        test=30,
        runner=runner,
    )
    assert report["folds"]
    for index, fold in enumerate(report["folds"]):
        assert fold["selected_strategy"] == "momentum"
        validation_calls = calls[index * 3 : index * 3 + 2]
        test_call = calls[index * 3 + 2]
        assert all(call[1] < test_call[0] for call in validation_calls)
        assert test_call[2] == "momentum"
        assert all(call[3] == report["warehouse_epoch"] for call in (*validation_calls, test_call))


def test_failed_preparation_leaves_previous_epoch_readable(historical_warehouse, monkeypatch):
    import finlab.engine.prepare as prepare
    from finlab.analytics.rankings import historical_rankings
    from finlab.analytics.snapshot import require_valid_snapshot

    before = historical_rankings(date(2020, 12, 31))

    def fail(*args, **kwargs):
        raise RuntimeError("Interrupted derivation")

    monkeypatch.setattr(prepare, "compute_metrics", fail)
    with write_conn(historical_warehouse) as con:
        with pytest.raises(RuntimeError, match="Interrupted"):
            prepare.prepare_warehouse(con, historical_warehouse, date(2020, 12, 31))
    assert (
        require_valid_snapshot(historical_warehouse.warehouse_dir)["epoch"]
        == before["warehouse_epoch"]
    )
    assert historical_rankings(date(2020, 12, 31)) == before


def test_company_assessment_api_is_point_in_time_and_explicit_about_coverage(
    historical_warehouse, monkeypatch
):
    from fastapi.testclient import TestClient

    from finlab.api.app import app

    def forbid(*args, **kwargs):
        raise AssertionError("Company assessment accessed network")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    client = TestClient(app)
    payload = client.get("/api/analytics/factors/AAA?as_of=2020-12-31")
    assert payload.status_code == 200, payload.text
    data = payload.json()
    assert data["assessment"]["opportunity_score"] is None
    assert data["assessment"]["universe_status"] == "current_seed_survivorship_bias"
    assert (
        data["comparisons"]["trailing_pe"]["sector_status"]
        == "historical_sector_classifications_unavailable"
    )
    assert "999.0" not in payload.text
    new = client.get("/api/analytics/factors/NEW?as_of=2020-12-31").json()
    assert new["assessment"]["price_history_status"] == "insufficient_or_inconsistent"


def test_discovery_and_history_routes_are_offline_and_point_in_time(
    historical_warehouse, monkeypatch
):
    from fastapi.testclient import TestClient

    from finlab.api.app import app

    def forbid(*args, **kwargs):
        raise AssertionError("Research endpoints accessed the network")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    client = TestClient(app)
    summary = client.get("/api/analytics/discovery?as_of=2020-12-29")
    assert summary.status_code == 200, summary.text
    result = summary.json()
    assert result["as_of"] == "2020-12-29"
    assert all(row["price_date"] <= result["as_of"] for row in result["rows"] if row["price_date"])
    history = client.get("/api/warehouse/history/AAA?as_of=2020-12-29&limit=2")
    assert history.status_code == 200
    prices = history.json()["prices"]
    assert len(prices) == 2 and prices[-1]["date"] == "2020-12-29"
    assert client.get("/api/analytics/model-validation").json()["reason"]
