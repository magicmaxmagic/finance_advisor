"""Validation-only strategy selection followed by untouched, disjoint test folds.

Strategies are deterministic and have no fitted coefficients. Training dates are
reserved warmup; validation selects among declared strategies. Each selected
strategy is evaluated once on the following test segment. Trial ledger records
all underlying strategy and benchmark simulations.
"""

from datetime import date

from finlab.analytics.snapshot import published_read_conn
from finlab.research.backtest import walk_forward_windows
from finlab.research.warehouse_backtest import backtest_warehouse


def walk_forward(
    start: date,
    end: date,
    tickers: list[str],
    *,
    train: int = 504,
    validation: int = 126,
    test: int = 126,
    strategies: tuple[str, ...] = ("equal_weight", "momentum"),
    runner=None,
) -> dict:
    with published_read_conn() as con:
        epoch = con.execute("SELECT epoch FROM _warehouse_publication").fetchone()[0]
        days = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT p.date FROM price_daily p JOIN security s USING(security_id) WHERE s.ticker='SPY' AND p.date BETWEEN ? AND ? ORDER BY p.date",
                [start, end],
            ).fetchall()
        ]
    if not strategies:
        raise ValueError("Declare at least one strategy before evaluation")
    run = runner or backtest_warehouse
    folds = []
    for window in walk_forward_windows(days, train, validation, test):
        scores = {}
        for strategy in strategies:
            result = run(
                window["validation"][0],
                window["validation"][-1],
                tickers,
                strategy=strategy,
                epoch=epoch,
            )
            score = result["metrics"]["sharpe"]
            if score is not None:
                scores[strategy] = score
        if not scores:
            raise ValueError("No strategy has a defined validation Sharpe")
        selected = sorted(scores, key=lambda key: (-scores[key], key))[0]
        outcome = run(
            window["out_of_sample"][0],
            window["out_of_sample"][-1],
            tickers,
            strategy=selected,
            epoch=epoch,
        )
        folds.append(
            {
                "training": [str(window["training"][0]), str(window["training"][-1])],
                "validation": [str(window["validation"][0]), str(window["validation"][-1])],
                "test": [str(window["out_of_sample"][0]), str(window["out_of_sample"][-1])],
                "validation_sharpes": scores,
                "selected_strategy": selected,
                "out_of_sample": outcome,
            }
        )
    if not folds:
        raise ValueError("Insufficient history for one complete train/validation/test fold")
    return {
        "warehouse_epoch": epoch,
        "folds": folds,
        "limitations": [
            "Training is reserved warmup; these strategies have no fitted coefficients.",
            "Strategy selection uses validation only; test folds are disjoint.",
            "Multiple trials, current-universe selection and revised vendor prices remain sources of uncertainty.",
            "Results are not a statistical proof of profitability.",
        ],
    }
