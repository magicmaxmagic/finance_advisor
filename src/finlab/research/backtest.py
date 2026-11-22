"""Lagged daily portfolio simulation over explicit total-return price indices.

Signals dated t execute at the NEXT supplied session's open. All held names must
have observations every session. Missing/delisted valuations fail rather than
silently liquidating at a fabricated price. Input open/close must share a total
return adjustment basis and currency; raw provider OHLC is not accepted.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import polars as pl

from finlab.research.stats import performance_metrics
from finlab.research.trials import research_trial


@dataclass(frozen=True)
class BacktestResult:
    equity: list[dict]
    trades: list[dict]
    metrics: dict
    caveats: tuple[str, ...]


def _simulate(
    prices: pl.DataFrame,
    targets: dict[date, dict[str, float]],
    *,
    price_basis: str,
    cost_bps: float = 5,
    slippage_bps: float = 5,
    initial_capital: float = 10000,
) -> BacktestResult:
    if price_basis != "total_return_common_currency":
        raise ValueError("Supply total-return adjusted open/close in a common currency")
    if (
        not all(np.isfinite(x) and x >= 0 for x in (cost_bps, slippage_bps))
        or not np.isfinite(initial_capital)
        or initial_capital <= 0
    ):
        raise ValueError("Invalid capital or costs")
    required = {"date", "security_id", "open", "close"}
    if not required.issubset(prices.columns):
        raise ValueError("Missing price columns")
    if prices.select(pl.struct("date", "security_id").is_duplicated().any()).item():
        raise ValueError("Duplicate price observations")
    if prices.filter(
        pl.any_horizontal(
            [
                pl.col(c).is_null() | ~pl.col(c).is_finite() | (pl.col(c) <= 0)
                for c in ("open", "close")
            ]
        )
    ).height:
        raise ValueError("Invalid prices")
    sessions = sorted(prices["date"].unique().to_list())
    if len(sessions) < 3:
        raise ValueError("At least three sessions required")
    for decision, weights in targets.items():
        if decision not in sessions:
            raise ValueError("Signal date must be an observed session")
        if (
            any(not np.isfinite(w) or w < 0 for w in weights.values())
            or sum(weights.values()) > 1 + 1e-12
        ):
            raise ValueError("Long-only target weights must sum to at most one")
    rows = {(r["date"], r["security_id"]): r for r in prices.to_dicts()}
    holdings = {}
    cash = initial_capital
    equity, trades = [], []
    rate = (cost_bps + slippage_bps) / 10000
    if rate >= 1:
        raise ValueError("Costs must be below 100%")
    for index, session in enumerate(sessions):
        # A missing held observation deliberately raises instead of inventing a return.
        opening = cash + sum(
            shares * rows[(session, sid)]["open"] for sid, shares in holdings.items()
        )
        weights = targets.get(sessions[index - 1]) if index else None
        turnover = 0.0
        if weights is not None:
            ids = set(holdings) | set(weights)
            current = {sid: holdings.get(sid, 0) * rows[(session, sid)]["open"] for sid in ids}
            # Solve post-cost NAV = pre-cost NAV - rate * traded notional.
            nav = opening
            for _ in range(100):
                next_nav = opening - rate * sum(
                    abs(weights.get(sid, 0) * nav - current[sid]) for sid in ids
                )
                if abs(next_nav - nav) < 1e-9:
                    nav = next_nav
                    break
                nav = next_nav
            total = sum(abs(weights.get(sid, 0) * nav - current[sid]) for sid in ids)
            turnover = total / opening
            trades.append(
                dict(
                    signal_date=sessions[index - 1],
                    execution_date=session,
                    notional=total,
                    cost=rate * total,
                    turnover=turnover,
                )
            )
            holdings = {
                sid: w * nav / rows[(session, sid)]["open"] for sid, w in weights.items() if w > 0
            }
            cash = nav * (1 - sum(weights.values()))
        equity.append(
            dict(
                date=session,
                value=cash
                + sum(shares * rows[(session, sid)]["close"] for sid, shares in holdings.items()),
                turnover=turnover,
            )
        )
    levels = np.array([r["value"] for r in equity])
    returns = levels[1:] / levels[:-1] - 1
    metrics = performance_metrics(returns)
    metrics["turnover"] = sum(r["turnover"] for r in trades)
    return BacktestResult(
        equity,
        trades,
        metrics,
        (
            "Caller must supply historically available targets and universe membership.",
            "Total-return indices assume dividend reinvestment; no tax, liquidity or borrowing model.",
        ),
    )


def walk_forward_windows(sessions: list[date], train: int, validation: int, test: int):
    """Non-overlapping test folds with expanding training and separate validation."""
    if min(train, validation, test) < 1 or sessions != sorted(set(sessions)):
        raise ValueError("Positive lengths and unique ordered sessions required")
    end_train = train
    while end_train + validation + test <= len(sessions):
        yield {
            "training": sessions[:end_train],
            "validation": sessions[end_train : end_train + validation],
            "out_of_sample": sessions[end_train + validation : end_train + validation + test],
        }
        end_train += test


def run_backtest(prices, targets, *, trial_store: Path, **parameters) -> BacktestResult:
    """Persist a research trial BEFORE simulation, including failed trials."""
    inputs_hash = hashlib.sha256(prices.hash_rows().to_numpy().tobytes()).hexdigest()
    config = json.dumps(
        {
            "engine_version": "lagged-open-v1",
            "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "parameters": parameters,
            "targets": [(str(day), weights) for day, weights in sorted(targets.items())],
        },
        sort_keys=True,
        allow_nan=False,
    )
    with research_trial(trial_store, inputs_hash, config) as summary:
        result = _simulate(prices, targets, **parameters)
        summary.update(result.metrics)
    return result
