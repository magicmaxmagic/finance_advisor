"""Dependence-aware descriptive inference for ordered research windows."""

from math import ceil, sqrt
from statistics import median

import numpy as np


def summarize(samples, *, seed=20261122, draws=3000):
    """Circular block bootstrap of the mean. Retain missing window positions."""
    x = np.array([np.nan if v is None else v for v in samples], dtype=float)
    observed = x[np.isfinite(x)]
    n = len(observed)
    result = dict(
        windows=len(x),
        observations=n,
        mean=float(observed.mean()) if n else None,
        median=median(observed.tolist()) if n else None,
        positive_fraction=float(np.mean(observed > 0)) if n else None,
        standard_deviation=float(observed.std(ddof=1)) if n > 1 else None,
        effective_n=None,
        confidence_95=None,
        p_value=None,
        block_length=None,
    )
    if n < 12 or n < 0.75 * len(x) or np.std(observed) <= 1e-12:
        return result
    lag_sum = 0.0
    block = ceil(sqrt(len(x)))
    for lag in range(1, block):
        left, right = x[:-lag], x[lag:]
        valid = np.isfinite(left) & np.isfinite(right)
        if valid.sum() < 4 or not np.std(left[valid]) or not np.std(right[valid]):
            break
        rho = float(np.corrcoef(left[valid], right[valid])[0, 1])
        if rho <= 0:
            break
        lag_sum += rho
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(x), size=(draws, ceil(len(x) / block), 1))
    indices = ((starts + np.arange(block)) % len(x)).reshape(draws, -1)[:, : len(x)]
    sampled = x[indices]
    count = np.isfinite(sampled).sum(axis=1)
    means = np.nansum(sampled[count > 0], axis=1) / count[count > 0]
    observed_mean = observed.mean()
    p = (1 + np.sum(np.abs(means - observed_mean) >= abs(observed_mean))) / (1 + len(means))
    result.update(
        effective_n=float(n / (1 + 2 * lag_sum)),
        block_length=block,
        confidence_95=np.quantile(means, [0.025, 0.975]).tolist(),
        p_value=float(p),
    )
    return result


def holm(p_values, family_size=None):
    """Holm step-down adjustment, including prespecified unavailable tests."""
    adjusted = dict.fromkeys(p_values)
    ordered = sorted((p, key) for key, p in p_values.items() if p is not None)
    size = len(p_values) if family_size is None else family_size
    previous = 0.0
    for index, (p, key) in enumerate(ordered):
        previous = min(1.0, max(previous, (size - index) * p))
        adjusted[key] = previous
    return adjusted
