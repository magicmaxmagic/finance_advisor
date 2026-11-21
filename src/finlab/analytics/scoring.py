"""Explainable cross-sectional multifactor scoring."""

from __future__ import annotations

import math

import polars as pl

SCORING_VERSION = "multifactor-v1"


def winsorize(values: list[float], limits: tuple[float, float] = (0.05, 0.95)) -> list[float]:
    """Cap tails at configured quantiles; missing values are preserved."""
    finite = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not finite:
        return values
    finite_sorted = sorted(finite)
    low_index = max(0, int(limits[0] * len(finite_sorted)))
    high_index = min(len(finite_sorted) - 1, int(limits[1] * len(finite_sorted)))
    low = finite_sorted[low_index]
    high = finite_sorted[high_index]
    return [
        max(low, min(high, float(value)))
        if value is not None and math.isfinite(float(value))
        else value
        for value in values
    ]


def robust_zscore(values: list[float]) -> list[float]:
    """Median/MAD normalized score with missing-value preservation."""
    finite = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if len(finite) < 2:
        return [
            0.0 if value is not None and math.isfinite(float(value)) else float("nan")
            for value in values
        ]
    ordered = sorted(finite)
    median = ordered[len(ordered) // 2]
    absolute = sorted(abs(value - median) for value in finite)
    mad = absolute[len(absolute) // 2]
    if mad == 0:
        return [
            0.0 if value is not None and math.isfinite(float(value)) else float("nan")
            for value in values
        ]
    return [
        (float(value) - median) / (1.4826 * mad)
        if value is not None and math.isfinite(float(value))
        else float("nan")
        for value in values
    ]


def score_universe(
    factor_frame: pl.DataFrame,
    weights: dict[str, float],
    as_of,
) -> pl.DataFrame:
    """Produce a 0-100 explainable score from complete factor inputs."""
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("factor weights must sum to 1")
    if any(weight < 0 or not math.isfinite(weight) for weight in weights.values()):
        raise ValueError("factor weights must be finite and non-negative")

    required = {"security_id", "factor", "value"}
    if not required.issubset(factor_frame.columns):
        raise ValueError(f"factor frame requires columns {sorted(required)}")

    securities = factor_frame["security_id"].unique().sort().to_list()
    rows: list[dict] = []
    for security_id in securities:
        available = factor_frame.filter(pl.col("security_id") == security_id)
        factor_values = dict(
            zip(available["factor"].to_list(), available["value"].to_list(), strict=False)
        )

        weighted = 0.0
        factor_scores: dict[str, float] = {}
        present = 0
        for factor, weight in weights.items():
            raw = factor_values.get(factor)
            if raw is None or not math.isfinite(float(raw)):
                factor_scores[factor] = float("nan")
                continue
            present += 1
            universe_values = factor_frame.filter(pl.col("factor") == factor)["value"].to_list()
            normalized = robust_zscore(winsorize(universe_values))
            ids = factor_frame.filter(pl.col("factor") == factor)["security_id"].to_list()
            index = ids.index(security_id)
            z = normalized[index]
            score = max(0.0, min(100.0, 50.0 + 15.0 * z))
            factor_scores[factor] = score
            weighted += weight * score

        completeness = present / len(weights) if weights else 0.0
        rows.append(
            {
                "security_id": security_id,
                "as_of": as_of,
                "overall_score": weighted,
                "data_completeness": completeness,
                "factor_scores": factor_scores,
                "weights": weights,
                "calculation_version": SCORING_VERSION,
            }
        )

    return pl.DataFrame(rows)
