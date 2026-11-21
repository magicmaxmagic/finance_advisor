"""Deterministic live decisions: threshold changes, stale trades and share basis."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from finlab.ingest.universe import Company
from finlab.research.live import assess_live
from finlab.research.weekly import Bar, Filing, assess, reprice, screen, weekly_sessions

WEEK = date(2026, 11, 20)
NOW = datetime(2026, 11, 23, 16, tzinfo=UTC)
COMPANY = Company(1, "AAA", "Industrials", "Alpha")


@pytest.fixture
def decision_inputs():
    bars = {"SPY": {}, "AAA": {}}
    for i in range(5 * 364 + 1):
        day = WEEK - timedelta(days=5 * 364 - i)
        if day.weekday() < 5:
            for symbol, close in (("AAA", 50 + i * 0.02), ("SPY", 100 + i * 0.01)):
                bars[symbol][day] = Bar(close, close, close)
    facts = {
        "revenue": [
            Filing(date(2025, 12, 31), 120, date(2026, 2, 20), "current"),
            Filing(date(2024, 12, 31), 100, date(2025, 2, 20), "prior"),
        ],
        "diluted_eps": [Filing(date(2025, 12, 31), 4, date(2026, 2, 20), "current")],
    }
    return bars, facts


def session(*, is_open=True):
    return SimpleNamespace(
        is_open=is_open,
        trading_date=NOW.date() if is_open else WEEK,
        last_close_date=WEEK,
    )


def quote(price=99, timestamp=NOW):
    return SimpleNamespace(price=price, timestamp=timestamp)


def base(inputs):
    bars, facts = inputs
    return assess(
        "AAA",
        1,
        "Industrials",
        bars["AAA"],
        bars["SPY"],
        weekly_sessions(bars["SPY"], WEEK),
        WEEK,
        facts,
    )


def live(inputs, trade, **kwargs):
    bars, facts = inputs
    return assess_live(
        COMPANY,
        bars["AAA"],
        bars["SPY"],
        facts,
        WEEK,
        trade,
        kwargs.pop("session", session()),
        NOW,
        **kwargs,
    )


def test_eps_four_live_crossing_changes_the_recommendation(decision_inputs):
    below = live(decision_inputs, quote(99))
    above = live(decision_inputs, quote(101))
    assert below["status"] == "Buy candidate"
    assert above["status"] == "Watch"
    assert below["yield_price_ceiling"] == above["yield_price_ceiling"] == 100
    assert below["earnings_yield"] == pytest.approx(4 / 99)
    assert above["tests"][0]["passed"] is False
    assert "earnings yield" in above["reason"]
    assert reprice(base(decision_inputs), 100, price_date=str(WEEK))["tests"][0]["passed"]


def test_weekly_and_live_share_the_same_implementation(decision_inputs):
    bars, facts = decision_inputs
    weekly = screen([COMPANY], bars, {1: facts}, WEEK)[0]
    updated = reprice(weekly, weekly["price"], price_date=weekly["quote_date"])
    assert weekly == updated
    for criterion in weekly["tests"]:
        assert criterion["threshold"]
        assert criterion["reference"] != "Unavailable"


def test_twenty_percent_eps_stress_has_eighty_dollar_ceiling(decision_inputs):
    row = live(decision_inputs, quote(99))
    assert row["stressed_price_ceiling"] == 80
    assert row["stressed_yield"] == pytest.approx(3.2 / 99)
    assert row["ceiling_gap"] == pytest.approx(100 / 99 - 1)


@pytest.mark.parametrize("age,expected", [(90, "Buy candidate"), (91, "Insufficient data")])
def test_stale_trade_cannot_validate_an_entry(decision_inputs, age, expected):
    row = live(decision_inputs, quote(timestamp=NOW - timedelta(seconds=age)))
    assert row["status"] == expected
    assert row["quote_age_seconds"] == age
    if age > 90:
        assert row["tests"][0]["passed"] is None
        assert "91 seconds" in row["reason"]


def test_missing_trade_partial_response_never_uses_old_price(decision_inputs):
    row = live(decision_inputs, None)
    assert row["status"] == "Insufficient data"
    assert row["price"] is None
    assert "partial response" in row["reason"]


def test_closed_session_uses_dated_close_not_latest_trade(decision_inputs):
    row = live(
        decision_inputs,
        quote(1),
        session=session(is_open=False),
        close_quote=quote(101, datetime(2026, 11, 20, 21, tzinfo=UTC)),
    )
    assert row["price"] == 101
    assert row["status"] == "Watch"
    assert row["analysis_date"] == "2026-11-20"
    assert row["price_kind"] == "Last completed session close"
    assert row["quote_age_seconds"] is None


def test_closed_session_rejects_wrong_close_date(decision_inputs):
    row = live(decision_inputs, None, session=session(is_open=False), close_quote=quote(99, NOW))
    assert row["status"] == "Insufficient data"
    assert "last completed session" in row["reason"]


def test_split_makes_eps_price_comparison_unknown(decision_inputs):
    bars, _ = decision_inputs
    day = WEEK - timedelta(days=14)
    bars["AAA"][day] = Bar(120, 60, 60)
    row = live(decision_inputs, quote())
    assert row["status"] == "Insufficient data"
    assert row["tests"][0]["passed"] is None
    assert row["yield_price_ceiling"] is None
    assert "split" in row["reason"]


def test_split_since_last_raw_daily_close_is_detected(decision_inputs):
    bars, _ = decision_inputs
    # Vendor history was adjusted into today's shares but today's raw bar is
    # not final yet. Constant historical ratios alone would miss this split.
    for day, value in list(bars["AAA"].items()):
        bars["AAA"][day] = Bar(value.raw, value.split / 2, value.adjusted / 2)
    row = live(decision_inputs, quote(49.5))
    assert row["status"] == "Insufficient data"
    assert "recent split" in row["reason"]


def test_missing_history_keeps_four_visible_unknown_criteria(decision_inputs):
    bars, _ = decision_inputs
    del bars["AAA"][WEEK - timedelta(weeks=3)]
    row = live(decision_inputs, quote())
    assert len(row["tests"]) == 4
    assert row["status"] == "Insufficient data"
    assert row["tests"][2]["passed"] is None
    assert row["tests"][3]["passed"] is None


def test_live_fundamentals_update_without_changing_completed_momentum(decision_inputs):
    _bars, facts = decision_inputs
    before = base(decision_inputs)
    facts["diluted_eps"].append(Filing(date(2025, 12, 31), 3, date(2026, 11, 21), "amended"))
    row = live(decision_inputs, quote(99))
    assert row["status"] == "Watch"
    assert row["eps"] == 3
    assert row["excess"] == before["excess"]
    assert row["drawdown"] == before["drawdown"]
    assert "2026-11-21" in row["references"]["eps"]
    assert base(decision_inputs)["eps"] == 4


def test_no_historical_outcomes_is_valid_and_reported_explicitly(decision_inputs):
    bars, _ = decision_inputs
    bars["AAA"].clear()
    row = live(decision_inputs, quote())
    assert row["history"]["windows"] == 0
    assert row["history"]["win_frequency"] is None
    assert row["history"]["median_excess"] is None
    assert row["history"]["worst_excess"] is None


def test_disjoint_thirteen_week_windows_have_correct_frequency(decision_inputs):
    row = base(decision_inputs)
    assert row["history"]["windows"] == 20
    assert row["history"]["wins"] == 20
    assert row["history"]["win_frequency"] == 1
    assert row["history"]["worst_excess"] > 0
