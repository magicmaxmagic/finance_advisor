"""Offline strategies over published Yahoo adjusted-close histories."""

from datetime import date

import numpy as np
import polars as pl

from finlab import config
from finlab.analytics.snapshot import published_read_conn
from finlab.research.backtest import run_backtest
from finlab.research.stats import performance_metrics


def backtest_warehouse(
    start: date,
    end: date,
    tickers: list[str],
    *,
    strategy: str = "equal_weight",
    top_n: int = 5,
    epoch: str | None = None,
):
    if (
        strategy not in {"equal_weight", "buy_hold", "momentum", "value", "quality", "multifactor"}
        or top_n < 1
        or start >= end
        or not tickers
    ):
        raise ValueError("Invalid strategy, dates or selection")
    with published_read_conn(epoch=epoch) as con:
        epoch = con.execute("SELECT epoch FROM _warehouse_publication").fetchone()[0]
        views = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        membership = {}
        if "universe_snapshot" in views:
            for snapshot_day, sid in con.execute(
                "SELECT snapshot_date,security_id FROM universe_snapshot WHERE snapshot_date<=?",
                [end],
            ).fetchall():
                membership.setdefault(snapshot_day, set()).add(sid)
        # Materialize only the requested panel and one year of signal warmup.
        records = con.execute(
            """SELECT p.security_id,s.ticker,p.date,p.open,p.close,p.adjusted_close,
            p.adjustment_basis,s.currency FROM price_daily p JOIN security s USING(security_id)
            WHERE s.ticker IN (SELECT unnest(?)) AND p.date BETWEEN (?::DATE-INTERVAL '400 days') AND ?
            ORDER BY p.date,p.security_id""",
            [sorted(set(tickers) | {"SPY"}), start, end],
        ).pl()
    if records.is_empty() or not set(tickers).issubset(set(records["ticker"].unique())):
        raise ValueError("Requested securities are missing from published history")
    if set(records["currency"].unique()) != {"USD"}:
        raise ValueError(
            "This strategy runner requires USD securities; no implicit currency conversion"
        )
    if records.filter(
        pl.col("adjusted_close").is_null()
        | (pl.col("adjustment_basis") != "yahoo_auto_adjust_false")
    ).height:
        raise ValueError(
            "Complete documented adjusted-close history is required; refresh with Data Engine"
        )
    panel = records.with_columns(
        (pl.col("open") * pl.col("adjusted_close") / pl.col("close")).alias("open"),
        pl.col("adjusted_close").alias("close"),
    )
    candidates = panel.filter(pl.col("ticker").is_in(tickers))
    test = candidates.filter(pl.col("date") >= start)
    sessions = sorted(test["date"].unique().to_list())
    targets = {}
    for index, day in enumerate(sessions[:-1]):
        if index and (
            strategy == "buy_hold"
            or (day.year, day.month) == (sessions[index - 1].year, sessions[index - 1].month)
        ):
            continue
        captured = [stamp for stamp in membership if stamp <= day]
        eligible = membership[max(captured)] if captured else None
        available = []
        fundamental_ranks = {}
        if strategy in {"value", "quality", "multifactor"}:
            from finlab.analytics.rankings import historical_rankings

            fundamental_ranks = {
                row.get("security_id"): row for row in historical_rankings(day, epoch=epoch)["rows"]
            }

        for sid in candidates["security_id"].unique().sort():
            if eligible is not None and sid not in eligible:
                continue
            history = candidates.filter(
                (pl.col("security_id") == sid) & (pl.col("date") <= day)
            ).sort("date")
            if history.is_empty() or history["date"][-1] != day:
                continue
            if strategy == "momentum":
                if history.height < 253:
                    continue
                score = history["close"][-22] / history["close"][-253] - 1
            elif strategy in {"value", "quality", "multifactor"}:
                field = {
                    "value": "earnings_yield",
                    "quality": "net_margin",
                    "multifactor": "score",
                }[strategy]
                historical_row = fundamental_ranks.get(sid, {})
                score = (
                    historical_row.get(field)
                    if historical_row.get("universe_eligible", True)
                    else None
                )
                if score is None:
                    continue
            else:
                score = 0.0
            available.append((float(score), sid))
        selected = [
            sid
            for _, sid in sorted(available, key=lambda pair: (-pair[0], pair[1]))[
                : top_n if strategy not in {"equal_weight", "buy_hold"} else len(available)
            ]
        ]
        if selected:
            targets[day] = {sid: 1 / len(selected) for sid in selected}
    if not targets:
        raise ValueError("No eligible historical signal dates")
    result = run_backtest(
        test.select("security_id", "date", "open", "close"),
        targets,
        trial_store=config.get_settings().reports_dir / "research" / "trials.sqlite",
        price_basis="total_return_common_currency",
    )
    benchmark_panel = panel.filter(
        (pl.col("ticker") == "SPY") & pl.col("date").is_in(sessions)
    ).sort("date")
    if benchmark_panel.height != len(sessions):
        raise ValueError("SPY is missing one or more matching benchmark sessions")
    benchmark_id = benchmark_panel["security_id"][0]
    benchmark = run_backtest(
        benchmark_panel.select("security_id", "date", "open", "close"),
        {sessions[0]: {benchmark_id: 1.0}},
        trial_store=config.get_settings().reports_dir / "research" / "trials.sqlite",
        price_basis="total_return_common_currency",
    )
    strategy_values = np.array([row["value"] for row in result.equity])
    benchmark_values = np.array([row["value"] for row in benchmark.equity])
    metrics = {
        **result.metrics,
        **performance_metrics(
            strategy_values[1:] / strategy_values[:-1] - 1,
            benchmark_values[1:] / benchmark_values[:-1] - 1,
        ),
    }
    return {
        "strategy": strategy,
        "warehouse_epoch": epoch,
        "start": str(start),
        "end": str(end),
        "tickers": tickers,
        "metrics": metrics,
        "benchmark_metrics": benchmark.metrics,
        "benchmark_equity": benchmark.equity,
        "equity": result.equity,
        "trades": result.trades,
        "caveats": [
            *result.caveats,
            "Selection is restricted to captured membership where available, using the latest snapshot on or before each signal. Before the first capture, current selection is survivorship biased; missing historical listings cannot be reconstructed.",
            "Yahoo adjusted data includes vendor corrections known at ingestion; historical vendor vintages are unavailable.",
        ],
    }
