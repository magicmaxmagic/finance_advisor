"""Weekly cutoffs, price basis, PIT fundamentals, data ingestion and UI integration."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from finlab.daemon.main import HANDLERS
from finlab.daemon.scheduler import build_scheduler
from finlab.ingest import companyfacts
from finlab.ingest.universe import Company
from finlab.ingest.weekly_prices import download, read_csv
from finlab.research import weekly_report
from finlab.research.weekly import Bar, Filing, completed_friday, screen
from finlab.research.weekly_example import example_brief
from finlab.research.weekly_report import archive_report, read_brief

WEEK = date(2026, 11, 20)
FILER = Company(1, "AAA", "Industrials", "Alpha")
FY = date(2025, 12, 31)
APP = Path(__file__).resolve().parents[1] / "src/finlab/ui/app.py"


@pytest.fixture
def inputs():
    prices = {"SPY": {}, "AAA": {}}
    for i in range(5 * 364 + 1):
        day = WEEK - timedelta(days=5 * 364 - i)
        if day.weekday() < 5:
            for symbol, close in (("AAA", 50 + i * 0.02), ("SPY", 100 + i * 0.01)):
                prices[symbol][day] = Bar(close, close, close)
    facts = {
        1: {
            "revenue": [
                Filing(FY, 120, date(2026, 2, 20), "0000000001-26-000001"),
                Filing(date(2024, 12, 31), 100, date(2025, 2, 20), "previous"),
            ],
            "diluted_eps": [Filing(FY, 4, date(2026, 2, 20), "0000000001-26-000001")],
        }
    }
    return prices, facts


def test_earnings_price_ceiling_and_history(inputs):
    prices, facts = inputs
    (row,) = screen([FILER], prices, facts, WEEK)
    assert row["status"] == "Buy candidate"
    assert row["growth"] == pytest.approx(0.20)
    assert row["yield_price_ceiling"] == 100
    assert row["earnings_yield"] == pytest.approx(4 / prices["AAA"][WEEK].raw)
    assert row["history"]["wins"] == row["history"]["windows"] == 20
    assert row["chart"][0]["Stock"] == row["chart"][0]["SPY"] == 100
    assert len(row["chart"]) == 53
    assert all(g["passed"] for g in row["tests"])
    assert row["evidence"]["Diluted EPS"]["url"].endswith("0000000001-26-000001-index.htm")


def test_future_inputs_do_not_change_recommendation(inputs):
    prices, facts = inputs
    before = screen([FILER], prices, facts, WEEK)
    prices["AAA"][WEEK + timedelta(days=3)] = Bar(1, 1, 1)
    facts[1]["diluted_eps"].append(Filing(FY, 1000, WEEK + timedelta(days=1), "future"))
    assert screen([FILER], prices, facts, WEEK) == before


def test_failed_valuation_has_a_specific_reason(inputs):
    prices, facts = inputs
    facts[1]["diluted_eps"] = [Filing(FY, 1, date(2026, 2, 20), "fixture")]
    (row,) = screen([FILER], prices, facts, WEEK)
    assert row["status"] == "Watch"
    assert "earnings yield" in row["reason"]


def test_split_or_missing_session_prevents_a_buy(inputs):
    prices, facts = inputs
    day = WEEK - timedelta(days=14)
    prices["AAA"][day] = Bar(120, 60, 60)
    (row,) = screen([FILER], prices, facts, WEEK)
    assert row["status"] == "Insufficient data" and "split" in row["reason"]
    del prices["AAA"][day]
    (row,) = screen([FILER], prices, facts, WEEK)
    assert row["status"] == "Insufficient data" and "weekly" in row["reason"]


def test_reject_old_benchmark_and_stale_fundamentals(inputs):
    prices, facts = inputs
    with pytest.raises(ValueError, match="benchmark"):
        screen([FILER], prices, facts, WEEK + timedelta(weeks=1))
    facts[1]["revenue"] = []
    assert screen([FILER], prices, facts, WEEK)[0]["status"] == "Insufficient data"


def test_new_york_cutoff_and_utc_daemon():
    assert completed_friday(datetime(2026, 11, 21, 3, tzinfo=UTC)) == date(2026, 11, 13)
    assert completed_friday(datetime(2026, 11, 21, 5, tzinfo=UTC)) == WEEK
    job = build_scheduler(lambda *_: None).get_job("weekly_brief")
    assert job.id in HANDLERS
    assert str(job.trigger.timezone) == "UTC"


def test_archive_is_immutable_and_csv_is_reusable(tmp_path, inputs):
    bars, _ = inputs
    path = archive_report(tmp_path, {"week": str(WEEK), "value": 1}, bars)
    assert archive_report(tmp_path, {"week": str(WEEK), "value": 2}, {}) == path
    assert read_brief(path)["value"] == 1
    with zipfile.ZipFile(path) as bundle:
        csv = Path(bundle.extract("prices.csv", tmp_path))
    assert read_csv(csv, WEEK) == bars
    with csv.open("a") as handle:
        handle.write(f"AAA,{WEEK},1,1,1\n")
    with pytest.raises(ValueError, match="repeated"):
        read_csv(csv, WEEK)


def test_alpaca_all_pages_and_adjustments():
    def respond(request):
        params = request.url.params
        assert params["feed"] == "iex" and params["asof"] == str(WEEK)
        second = "page_token" in params
        symbol = "AAA" if second else "SPY"
        close = {"raw": 12, "split": 6, "all": 5}[params["adjustment"]]
        return httpx.Response(
            200,
            json={
                "bars": {symbol: [{"t": "2026-11-20T05:00:00Z", "c": close}]},
                "next_page_token": None if second else "abc",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        bars = download(client, ["AAA", "SPY"], WEEK)
    assert set(bars) == {"AAA", "SPY"}
    assert bars["AAA"][WEEK] == Bar(12, 6, 5)


def test_weekly_pipeline_with_sec_wire_format(settings, monkeypatch, tmp_path, inputs):
    from finlab.ingest import universe

    bars, _ = inputs
    monkeypatch.setattr(universe, "load_poc_universe", lambda: (FILER,))
    monkeypatch.setattr(weekly_report, "completed_friday", lambda _: WEEK)

    def observation(year, value):
        return {
            "start": f"{year}-01-01",
            "end": f"{year}-12-31",
            "form": "10-K",
            "filed": f"{year + 1}-02-20",
            "val": value,
            "accn": f"annual-{year}",
        }

    rev = [observation(2024, 100), observation(2025, 120)]
    eps = [observation(2025, 4), observation(2025, 999) | {"filed": "2026-11-21", "accn": "future"}]
    document = {
        "cik": 1,
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": rev}},
                "EarningsPerShareDiluted": {
                    "units": {"USD/shares": eps, "EUR/shares": [observation(2025, 199)]}
                },
            }
        },
    }

    async def fetch(companies, cfg, *, force):
        assert force and companies == (FILER,)
        return companyfacts.FetchReport(downloaded=[1])

    monkeypatch.setattr(companyfacts, "fetch_poc_universe", fetch)
    monkeypatch.setattr(companyfacts, "load_manifest", lambda _: {"1": {"sha256": "fixture"}})
    monkeypatch.setattr(companyfacts, "read_raw", lambda *_: document)
    with zipfile.ZipFile(archive_report(tmp_path, {"week": str(WEEK)}, bars)) as bundle:
        csv = Path(bundle.extract("prices.csv", tmp_path))
    result = weekly_report.generate(settings, prices=csv)
    brief = read_brief(result)
    (row,) = brief["rows"]
    assert row["status"] == "Buy candidate"
    assert row["evidence"]["Diluted EPS"]["value"] == 4
    assert row["growth"] == pytest.approx(0.2)
    assert brief["sec_document_hashes"]["AAA"] == "fixture"
    assert weekly_report.generate(settings) == result
    assert "future" not in json.dumps(brief)


def test_weekly_ui_from_blank_checkout(settings, monkeypatch):
    monkeypatch.setenv("FINLAB_DATA_DIR", str(settings.data_dir))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception and app.sidebar.radio[0].value == "Weekly brief"
    app.toggle[0].set_value(True).run()
    assert not app.exception
    assert any("First to research" in h.value for h in app.subheader)
    app.selectbox[0].set_value("EXAMPLE-B").run()
    assert not app.exception
    assert any("Watch" in h.value for h in app.subheader)


def test_archived_ui_selects_real_edition(settings, monkeypatch):
    monkeypatch.setenv("FINLAB_DATA_DIR", str(settings.data_dir))
    data = example_brief()
    archive_report(settings.reports_dir / "weekly", data, {})
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert app.selectbox[0].value.stem == data["week"]
    assert app.get("download_button")


def test_thursday_quote_only_accepted_on_actual_friday_holiday():
    from finlab.research.weekly import weekly_sessions

    good_friday = date(2026, 4, 3)
    thursday = date(2026, 4, 2)
    ordinary_thursday = date(2026, 11, 19)
    series = {d: Bar(100, 100, 100) for d in (thursday, ordinary_thursday)}
    assert weekly_sessions(series, WEEK) == {good_friday: thursday}
