from datetime import date, timedelta

import polars as pl
import pytest

from finlab.research.backtest import run_backtest, walk_forward_windows
from finlab.research.stats import performance_metrics


def prices():
    return pl.DataFrame(
        dict(
            security_id=["A"] * 3,
            date=[date(2020, 1, 1) + timedelta(days=i) for i in range(3)],
            open=[10.0, 100.0, 110.0],
            close=[100.0, 110.0, 121.0],
        )
    )


def test_signal_cannot_earn_same_bar_or_next_overnight_return(tmp_path):
    result = run_backtest(
        prices(),
        {date(2020, 1, 1): {"A": 1.0}},
        trial_store=tmp_path / "trials.sqlite",
        price_basis="total_return_common_currency",
        cost_bps=0,
        slippage_bps=0,
    )
    assert [r["value"] for r in result.equity] == pytest.approx([10000, 11000, 12100])
    assert result.trades[0]["execution_date"] == date(2020, 1, 2)


def test_costs_are_charged_on_notional_and_leave_nonnegative_cash(tmp_path):
    result = run_backtest(
        prices(),
        {date(2020, 1, 1): {"A": 1.0}},
        trial_store=tmp_path / "trials.sqlite",
        price_basis="total_return_common_currency",
        cost_bps=100,
        slippage_bps=0,
    )
    nav = 10000 / 1.01
    assert result.trades[0]["cost"] == pytest.approx(nav * 0.01)
    assert result.equity[1]["value"] == pytest.approx(nav * 1.1)


def test_unknown_adjustment_basis_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="total-return"):
        run_backtest(prices(), {}, trial_store=tmp_path / "trials.sqlite", price_basis="raw")


def test_drawdown_includes_initial_capital_and_undefined_ratios_are_null():
    metrics = performance_metrics([-0.1, 0.0])
    assert metrics["max_drawdown"] == pytest.approx(-0.1)
    flat = performance_metrics([0.0, 0.0])
    assert flat["sharpe"] is None
    assert flat["sortino"] is None


def test_walk_forward_has_separate_dates_and_disjoint_test_folds():
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(20)]
    folds = list(walk_forward_windows(days, 5, 3, 4))
    assert len(folds) == 3
    seen = set()
    for fold in folds:
        assert max(fold["training"]) < min(fold["validation"])
        assert max(fold["validation"]) < min(fold["out_of_sample"])
        assert not seen.intersection(fold["out_of_sample"])
        seen.update(fold["out_of_sample"])


def test_failed_trials_are_persisted_before_validation(tmp_path):
    import sqlite3

    path = tmp_path / "trials.sqlite"
    with pytest.raises(ValueError):
        run_backtest(prices(), {}, trial_store=path, price_basis="raw")
    with sqlite3.connect(path) as ledger:
        assert ledger.execute("SELECT state FROM research_trial").fetchall() == [("failed",)]


def test_constant_return_roundoff_does_not_create_an_enormous_sharpe():
    metrics = performance_metrics([0.01] * 10, [0.01] * 10)
    assert metrics["sharpe"] is None
    assert metrics["beta"] is None
    assert metrics["information_ratio"] is None
