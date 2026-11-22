"""Deterministic daily-return statistics with explicit zero-denominator handling."""

import numpy as np


def performance_metrics(returns, benchmark=None, risk_free_annual: float = 0.0) -> dict:
    r = np.asarray(returns, dtype=float)
    if r.ndim != 1 or len(r) < 2 or not np.isfinite(r).all() or (r <= -1).any():
        raise ValueError("Require at least two finite daily returns greater than -1")
    wealth = np.r_[1.0, np.cumprod(1 + r)]
    volatility = float(np.std(r, ddof=1) * np.sqrt(252))
    excess = r - ((1 + risk_free_annual) ** (1 / 252) - 1)
    downside = float(np.sqrt(np.mean(np.minimum(excess, 0) ** 2)) * np.sqrt(252))
    cagr = float(wealth[-1] ** (252 / len(r)) - 1)
    drawdown = float(np.min(wealth / np.maximum.accumulate(wealth) - 1))
    metrics = dict(
        cagr=cagr,
        annualized_volatility=volatility,
        max_drawdown=drawdown,
        sharpe=float(np.mean(excess) * 252 / volatility) if volatility > 1e-12 else None,
        sortino=float(np.mean(excess) * 252 / downside) if downside > 1e-12 else None,
        calmar=cagr / abs(drawdown) if abs(drawdown) > 1e-12 else None,
    )
    q = float(np.quantile(r, 0.05))
    metrics.update(var_95=-q, cvar_95=-float(np.mean(r[r <= q])))
    if benchmark is not None:
        b = np.asarray(benchmark, dtype=float)
        if b.shape != r.shape or not np.isfinite(b).all() or (b <= -1).any():
            raise ValueError("Benchmark must have the same finite daily return dates")
        variance = float(np.var(b, ddof=1))
        beta = float(np.cov(r, b, ddof=1)[0, 1] / variance) if variance > 1e-20 else None
        active = r - b
        tracking = float(np.std(active, ddof=1) * np.sqrt(252))
        metrics.update(
            beta=beta,
            alpha=float(
                np.mean(excess) * 252
                - beta * np.mean(b - ((1 + risk_free_annual) ** (1 / 252) - 1)) * 252
            )
            if beta is not None
            else None,
            information_ratio=float(np.mean(active) * 252 / tracking) if tracking > 1e-12 else None,
        )
    return metrics
