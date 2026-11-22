"""Explainable cross-sectional multifactor scoring."""

from __future__ import annotations

import math
from statistics import median

import polars as pl

SCORING_VERSION = "multifactor-v2"


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
    center = median(ordered)
    absolute = sorted(abs(value - center) for value in finite)
    mad = median(absolute)
    if mad == 0:
        return [
            0.0 if value is not None and math.isfinite(float(value)) else float("nan")
            for value in values
        ]
    return [
        (float(value) - center) / (1.4826 * mad)
        if value is not None and math.isfinite(float(value))
        else float("nan")
        for value in values
    ]


def score_universe(
    factor_frame: pl.DataFrame,
    weights: dict[str, float],
    as_of,
    *,
    sector_neutral: bool = False,
) -> pl.DataFrame:
    """Produce a 0-100 explainable score from complete factor inputs."""
    weights = {factor: weight for factor, weight in weights.items() if weight != 0}
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("factor weights must sum to 1")
    if any(weight < 0 or not math.isfinite(weight) for weight in weights.values()):
        raise ValueError("factor weights must be finite and non-negative")

    required = {"security_id", "factor", "value"}
    if not required.issubset(factor_frame.columns):
        raise ValueError(f"factor frame requires columns {sorted(required)}")

    if "as_of" in factor_frame.columns:
        factor_frame = factor_frame.filter(pl.col("as_of") == as_of)
    if factor_frame.select(pl.struct("security_id", "factor").is_duplicated().any()).item():
        raise ValueError("Duplicate security/factor inputs; select one as-of vintage")

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
        normalization = {}
        for factor, weight in weights.items():
            raw = factor_values.get(factor)
            if raw is None or not math.isfinite(float(raw)):
                factor_scores[factor] = float("nan")
                continue
            present += 1
            cohort = factor_frame.filter(pl.col("factor") == factor)
            method = "global"
            if sector_neutral and "sector" in factor_frame.columns:
                sector = available["sector"][0]
                sector_cohort = (
                    cohort.filter(pl.col("sector") == sector)
                    if sector not in (None, "", "Unknown", "Unclassified")
                    else cohort.head(0)
                )
                if (
                    sector_cohort.filter(
                        pl.col("value").is_not_null() & pl.col("value").is_finite()
                    ).height
                    >= 3
                ):
                    cohort = sector_cohort
                    method = f"sector:{sector}"
            normalization[factor] = method
            universe_values = cohort["value"].to_list()
            normalized = robust_zscore(winsorize(universe_values))
            ids = cohort["security_id"].to_list()
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
                "overall_score": weighted if completeness == 1.0 else None,
                "data_status": "complete" if completeness == 1.0 else "insufficient_data",
                "data_completeness": completeness,
                "factor_scores": factor_scores,
                "normalization": normalization,
                "weights": weights,
                "calculation_version": SCORING_VERSION,
            }
        )

    return pl.DataFrame(rows)
