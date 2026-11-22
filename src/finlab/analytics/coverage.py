"""Measure configured-universe metric coverage against a pinned publication."""

from collections import Counter
from datetime import date

from finlab.analytics.assessment import INPUT_VERSION, VERSION, assess, assessment_cohort
from finlab.analytics.factors import MARKET_DEFINITIONS
from finlab.analytics.fundamentals import DEFINITIONS

SCORE_FIELDS = (
    "fundamental_score",
    "technical_score",
    "market_risk_score",
    "financial_risk_score",
    "risk_resilience_score",
    "opportunity_score",
)
INPUT_FIELDS = ("annual_statement", "reported_shares", "reported_market_cap")
STRUCTURAL = {"NOT_APPLICABLE", "INVALID_DENOMINATOR"}


def _reason(diagnostic):
    diagnostic = diagnostic or {}
    code = diagnostic.get("detail_code") or diagnostic.get("code", "SOURCE_MISSING")
    return "PERIOD_MISMATCH" if code == "INVALID_FISCAL_ALIGNMENT" else code


def _metrics(row, score):
    annual = row["fundamental_factors"]["annual_period"]
    observed = date.fromisoformat(row["as_of"])
    fresh_annual = annual and (observed - date.fromisoformat(annual)).days <= 550
    yield (
        "annual_statement",
        annual if fresh_annual else None,
        "STALE_DATA" if annual else "SOURCE_MISSING",
    )
    shares = row.get("share_observation")
    yield (
        "reported_shares",
        shares if shares and shares["fresh"] else None,
        "STALE_DATA" if shares else "SHARES_UNAVAILABLE",
    )
    yield (
        "reported_market_cap",
        row["reported_share_market_cap"],
        _reason(row.get("market_cap_diagnostic")),
    )
    for key in DEFINITIONS:
        metric = row["fundamental_factors"]["factors"].get(key, {})
        yield (
            key,
            metric.get("value"),
            _reason(metric.get("diagnostic")),
        )
    market = {metric["factor"]: metric for metric in row["market_factors"]}
    for key in MARKET_DEFINITIONS:
        metric = market.get(key, {})
        yield (
            key,
            metric.get("value"),
            (metric.get("diagnostic") or {}).get("code", "INSUFFICIENT_HISTORY"),
        )
    for key in SCORE_FIELDS:
        yield key, score[key], "INCOMPLETE_SCORE_GROUP"


def coverage_report(start: date, end: date, epoch: str | None = None):
    """Sample each calendar year-end in range, plus the terminal cutoff."""
    if start > end:
        raise ValueError("Coverage end must be on or after start")
    cutoffs = [date(year, 12, 31) for year in range(start.year, end.year)] + [end]
    samples = {}
    for cutoff in cutoffs:
        epoch, rows, captured = assessment_cohort(cutoff, epoch)
        scored = [(row, assess(row, rows)) for row in rows if row["eligible"]]
        partitions = {}
        for label, financial in (("all", None), ("financial", True), ("nonfinancial", False)):
            group = [
                (row, score)
                for row, score in scored
                if financial is None or row["financial_issuer"] == financial
            ]
            counts = {
                metric: Counter()
                for metric in (*INPUT_FIELDS, *DEFINITIONS, *MARKET_DEFINITIONS, *SCORE_FIELDS)
            }
            missing = {metric: [] for metric in counts}
            for row, score in group:
                for key, value, reason in _metrics(row, score):
                    counts[key]["AVAILABLE" if value is not None else reason] += 1
                    if value is None:
                        missing[key].append(
                            {
                                "security_id": row["security_id"],
                                "ticker": row["ticker"],
                                "reason": reason,
                                "structural_at_cutoff": reason in STRUCTURAL,
                            }
                        )
            partitions[label] = {"eligible": len(group), "metric_coverage": {}}
            for key, count in counts.items():
                available = count.pop("AVAILABLE", 0)
                structural = sum(count.get(reason, 0) for reason in STRUCTURAL)
                partitions[label]["metric_coverage"][key] = {
                    "available": available,
                    "coverage_pct": round(100 * available / len(group), 2) if group else None,
                    "missing_reasons": dict(count),
                    "main_missing_reason": count.most_common(1)[0][0] if count else None,
                    "structurally_unavailable_at_cutoff": structural,
                    "unresolved_or_conditionally_recoverable": sum(count.values()) - structural,
                    "affected_securities": missing[key],
                    "source": (
                        "Published daily prices"
                        if key in MARKET_DEFINITIONS
                        else "Versioned assessment"
                        if key in SCORE_FIELDS
                        else "Published SEC filings and split-aligned prices"
                        if key == "reported_market_cap"
                        else "Published SEC facts and aligned price where applicable"
                    ),
                }
        samples[str(cutoff)] = {
            "epoch": epoch,
            "captured_membership": captured,
            "business_models": partitions,
        }
    return {"model_version": VERSION, "input_version": INPUT_VERSION, "samples": samples}


def score_dependencies(as_of: date, epoch: str | None = None):
    """Trace failures through the production evidence slots without changing their policy."""
    from finlab.config import get_settings

    epoch, cohort, captured = assessment_cohort(as_of, epoch)
    weights = get_settings().assessment_weights
    rows, causes, single_slot = [], Counter(), Counter()
    eligible = [row for row in cohort if row["eligible"]]
    for row in eligible:
        result = assess(row, cohort, weights)
        if result["opportunity_score"] is not None:
            continue
        slots, found = [], set()
        if not row["market_ready"]:
            found.add("PRICE_WINDOW")
        for dimension, groups in result["breakdown"].items():
            if not weights[dimension]:
                continue
            for group, detail in groups.items():
                for slot in detail["slots"]:
                    if slot["selected"] is not None:
                        continue
                    reasons = {
                        key: {
                            **blocker,
                            "root": _reason(blocker),
                            "raw_available": key in detail["available"],
                            "repairability": "structural_at_cutoff"
                            if _reason(blocker) in STRUCTURAL
                            else "requires_verified_data",
                        }
                        for key, blocker in slot["blockers"].items()
                    }
                    found.update(item["root"] for item in reasons.values())
                    slot_name = f"{dimension}/{group}"
                    slots.append(
                        {
                            "slot": slot_name,
                            "coverage": detail["coverage"],
                            "alternatives": reasons,
                            "rule": "At least one listed input must be valid and have eight comparable "
                            "observations in the issuer's business-model cohort.",
                        }
                    )
        if len(slots) == 1 and row["market_ready"]:
            single_slot[slots[0]["slot"]] += 1
        causes.update(found)
        rows.append(
            {
                "security_id": row["security_id"],
                "ticker": row["ticker"],
                "missing_slots": slots,
                "unavailable_dimensions": result["unavailable_dimensions"],
                "price_window_complete": row["market_ready"],
                "available_groups": {
                    key: [name for name, score in groups.items() if score is not None]
                    for key, groups in result["score_groups"].items()
                },
                "market_cap_diagnostic": row.get("market_cap_diagnostic"),
            }
        )
    return {
        "as_of": str(as_of),
        "epoch": epoch,
        "model_version": VERSION,
        "input_version": INPUT_VERSION,
        "weights": weights,
        "captured_membership": captured,
        "eligible": len(eligible),
        "available": len(eligible) - len(rows),
        "blocked": rows,
        "companies_by_root": dict(causes.most_common()),
        "single_slot_recovery_ceiling": dict(single_slot.most_common()),
        "limitation": "Root totals overlap. A recovery ceiling counts a sole blocking slot, not evidence that it can be repaired.",
    }
