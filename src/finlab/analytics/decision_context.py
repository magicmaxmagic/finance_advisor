"""Company-level research conditions and horizon-specific validation evidence."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite

from finlab.research.decision_evidence import RULES, decision_flags
from finlab.research.signal_statistics import holm

HORIZONS = {"tactical": 21, "medium": 63, "long": 252}
DIMENSIONS = {
    "business_quality": "business_quality",
    "valuation": "valuation",
    "growth": "growth",
    "cash_flow": "cash_flow_quality",
}


def _evidence(report: dict | None) -> dict:
    summary = (report or {}).get("summary", {})
    results = {
        (str(window), state): summary.get(str(window), {})
        .get("decision_evidence", {})
        .get(state, {})
        .get("windows", {})
        .get("net_difference", {})
        for window in HORIZONS.values()
        for state in RULES
    }
    adjusted = holm({key: result.get("p_value") for key, result in results.items()})
    rows = {}
    for (window, state), statistics in results.items():
        interval = statistics.get("confidence_95")
        observations = statistics.get("observations", 0)
        p_value = adjusted.get((window, state))
        effective = statistics.get("effective_n") or 0
        status = "insufficient"
        if observations >= 12 and effective >= 8 and interval and len(interval) == 2:
            status = "inconclusive"
            if p_value is not None and p_value < 0.05 and interval[0] * interval[1] > 0:
                status = "higher_returns" if interval[0] > 0 else "lower_returns"
        rows.setdefault(window, {})[state] = {
            "status": status,
            "windows": observations,
            "effective_windows": effective or None,
            "interval": interval,
            "net_mean_difference": statistics.get("mean"),
            "adjusted_p": p_value,
            "study_version": (report or {}).get("version"),
            "study_end": (report or {}).get("end"),
        }
    return rows


def decision_context(row: dict, raw: Mapping[str, float | None], report: dict | None) -> dict:
    assessment = row["assessment"]
    trend = raw.get("price_to_sma200") if row["market_ready"] else None
    fundamental = assessment["fundamental_score"]
    margin = raw.get("margin_change")
    flags = decision_flags(fundamental, trend, margin)
    metrics = {"fundamental_score": fundamental, "price_to_sma200": trend, "margin_change": margin}
    change = {
        "entry": "A fundamental score of at least 65 and a close above the 200-session average satisfy this rule.",
        "exit_review": "A close below the 200-session average together with declining annual net margin prompts this research review.",
    }
    evidence = _evidence(report)
    conditions = {}
    for key, flag in flags.items():
        inputs = (
            ("fundamental_score", "price_to_sma200")
            if key == "entry"
            else ("price_to_sma200", "margin_change")
        )
        missing = [name for name in inputs if metrics[name] is None]
        conditions[key] = {
            "rule": RULES[key],
            "observed": flag,
            "missing": missing,
            "inputs": {name: metrics[name] for name in inputs},
            "watch": change[key],
            "history": {name: evidence[str(window)][key] for name, window in HORIZONS.items()},
        }
    groups = assessment["score_groups"]["fundamental"]
    views = {label: groups[key] for label, key in DIMENSIONS.items()}
    views |= {
        "market_timing": assessment["technical_score"],
        "market_risk_resilience": assessment["market_risk_score"],
        "financial_risk_resilience": assessment["financial_risk_score"],
    }
    return {
        "version": "decision-context-v8.0",
        "dimensions": views,
        "conditions": conditions,
        "horizons": HORIZONS,
        "contradictions": assessment["contradictions"],
        "risk": {
            key: raw[key]
            for key in ("annualized_volatility", "max_drawdown", "beta")
            if raw.get(key) is not None and isfinite(raw[key])
        },
        "holding_review": "A thesis review condition is present."
        if flags["exit_review"] is True
        else "A thesis review condition could not be evaluated."
        if flags["exit_review"] is None
        else "The tested deterioration condition is absent. Review the holding against the original thesis and portfolio exposure.",
    }
