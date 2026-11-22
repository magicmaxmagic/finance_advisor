"""Reproducible cohort scoring over a single warehouse publication."""

from collections import defaultdict
from datetime import date
from functools import lru_cache
from itertools import combinations
from math import isfinite
from statistics import correlation, median

from finlab import config
from finlab.analytics.company import load_benchmark, read_company
from finlab.analytics.snapshot import attached_views, published_read_conn, require_valid_snapshot
from finlab.core.pit import install_macros
from finlab.ingest.universe import load_poc_universe

VERSION = "hierarchical-equity-assessment-v2"
DEFAULT_WEIGHTS = {"fundamental": 0.60, "technical": 0.25, "risk": 0.15}
MIN_COHORT = 8
# Each tuple is one fixed evidence slot. Alternatives are tried in order and
# always compared against observations of the same metric.
GROUPS = {
    "fundamental": {
        "valuation": (("earnings_yield",),),
        "business_quality": (("roa", "net_margin"),),
        "growth": (("revenue_growth",),),
        "cash_flow_quality": (("fcf_margin",),),
    },
    "technical": {
        "trend": (("price_to_sma200",),),
        "relative_momentum": (("relative_momentum_12m",),),
    },
    "risk": {
        "market_risk": (("annualized_volatility",), ("max_drawdown",)),
        "financial_risk": (
            ("debt_to_assets", "reported_debt_to_assets", "interest_coverage"),
            ("current_ratio", "cash_to_debt"),
        ),
    },
}
LOWER = {
    "annualized_volatility",
    "debt_to_assets",
    "reported_debt_to_assets",
    "trailing_pe",
    "price_to_fcf",
    "ev_to_operating_ebitda",
    "net_debt_to_ebitda",
    "var_95",
    "cvar_95",
    "downside_volatility",
}
OPTIONAL_RISK = {
    "market_risk": ("beta", "downside_volatility", "var_95", "cvar_95", "relative_volume20"),
    "financial_risk": ("net_debt_to_ebitda", "debt_to_equity", "earnings_variability"),
}
COMPARISONS = (
    "trailing_pe",
    "ttm_pe",
    "ev_to_operating_ebitda",
    "price_to_fcf",
    "fcf_yield",
    "roa",
    "net_margin",
    "revenue_growth",
    "debt_to_assets",
    "reported_debt_to_assets",
)


def percentile(value, sample):
    """Midrank percentile. Bounded and insensitive to outlier magnitude."""
    return (
        100 * (sum(x < value for x in sample) + 0.5 * sum(x == value for x in sample)) / len(sample)
    )


def values(row):
    return {
        **{k: v["value"] for k, v in row["fundamental_factors"]["factors"].items()},
        **{r["factor"]: r["value"] for r in row["market_factors"]},
    }


def assess(row, cohort, weights=None):
    weights = DEFAULT_WEIGHTS if weights is None else weights
    if (
        set(weights) != set(DEFAULT_WEIGHTS)
        or any(not isfinite(w) or w < 0 for w in weights.values())
        or abs(sum(weights.values()) - 1) > 1e-9
    ):
        raise ValueError("Dimension weights must be finite, nonnegative and sum to one")
    raw = values(row)
    samples = defaultdict(list)
    for peer in cohort:
        if not peer["eligible"] or peer["financial_issuer"] != row["financial_issuer"]:
            continue
        for key, value in values(peer).items():
            if value is not None and isfinite(value):
                if key not in peer["fundamental_factors"]["factors"] and not peer["market_ready"]:
                    continue
                samples[key].append(value)

    policy = {dimension: groups.copy() for dimension, groups in GROUPS.items()}
    if row["financial_issuer"]:
        policy["fundamental"].pop("cash_flow_quality")
        # Book equity/assets is a separate non-regulatory capital proxy.
        policy["risk"]["financial_risk"] = (("equity_to_assets",),)
    breakdown = {}
    groups = {}
    dimensions = {}
    contributions = []
    for dimension, rules in policy.items():
        breakdown[dimension] = {}
        groups[dimension] = {}
        for name, slots in rules.items():
            evidence = []
            for alternatives in slots:
                chosen = None
                for key in alternatives:
                    value = raw.get(key)
                    sample = samples[key]
                    if value is None or not isfinite(value) or len(sample) < MIN_COHORT:
                        continue
                    if key not in row["fundamental_factors"]["factors"] and not row["market_ready"]:
                        continue
                    rank = percentile(value, sample)
                    chosen = dict(
                        metric=key,
                        raw=value,
                        score=100 - rank if key in LOWER else rank,
                        cohort_median=median(sample),
                        cohort_count=len(sample),
                        direction="lower" if key in LOWER else "higher",
                    )
                    break
                evidence.append(dict(required_one_of=alternatives, selected=chosen))
            selected = [slot["selected"] for slot in evidence if slot["selected"] is not None]
            score = (
                sum(item["score"] for item in selected) / len(slots)
                if len(selected) == len(slots)
                else None
            )
            tracked = {key for slot in slots for key in slot} | set(OPTIONAL_RISK.get(name, ()))
            breakdown[dimension][name] = dict(
                score=score,
                slots=evidence,
                coverage=len(selected) / len(slots),
                available=sorted(key for key in tracked if raw.get(key) is not None),
                missing=sorted(key for key in tracked if raw.get(key) is None),
                optional_metrics=OPTIONAL_RISK.get(name, ()),
                reason=None
                if score is not None
                else "Required evidence, comparable observations, or complete price history unavailable.",
            )
            groups[dimension][name] = score
        group_scores = groups[dimension].values()
        dimensions[dimension] = (
            sum(group_scores) / len(rules)
            if all(score is not None for score in group_scores)
            else None
        )
    active = [key for key, weight in weights.items() if weight > 0]
    overall = (
        sum(dimensions[key] * weights[key] for key in active)
        if row["eligible"]
        and row["market_ready"]
        and all(dimensions[k] is not None for k in active)
        else None
    )
    for dimension, details in breakdown.items():
        if dimensions[dimension] is None:
            continue
        for group, item in details.items():
            weight = weights[dimension] / len(details) / len(item["slots"])
            for slot in item["slots"]:
                metric = slot["selected"]
                contributions.append(
                    dict(
                        **metric,
                        group=group,
                        dimension=dimension,
                        weight=weight,
                        points_from_neutral=(metric["score"] - 50) * weight,
                    )
                )
    positive = sorted(
        (v for v in contributions if v["points_from_neutral"] > 0),
        key=lambda v: -v["points_from_neutral"],
    )[:3]
    negative = sorted(
        (v for v in contributions if v["points_from_neutral"] < 0),
        key=lambda v: v["points_from_neutral"],
    )[:3]
    contradictions = []
    fundamental, technical = dimensions["fundamental"], dimensions["technical"]
    if fundamental is not None and technical is not None:
        if fundamental >= 65 and technical <= 35:
            contradictions.append("Strong relative fundamentals with weak technical conditions.")
        if fundamental <= 35 and technical >= 65:
            contradictions.append("Weak relative fundamentals with strong technical conditions.")
    valuation = groups["fundamental"]["valuation"]
    if (
        valuation is not None
        and valuation >= 65
        and raw.get("margin_change") is not None
        and raw["margin_change"] < 0
    ):
        contradictions.append("Relatively high earnings yield with declining annual net margin.")
    growth = groups["fundamental"]["growth"]
    if (
        growth is not None
        and growth >= 65
        and raw.get("fcf_margin") is not None
        and raw["fcf_margin"] < 0
    ):
        contradictions.append("Strong relative revenue growth with negative free cash flow margin.")
    return dict(
        model_version=VERSION,
        opportunity_score=overall,
        fundamental_score=fundamental,
        technical_score=technical,
        risk_resilience_score=dimensions["risk"],
        weights=weights,
        score_groups=groups,
        breakdown=breakdown,
        contributions=contributions,
        positive_contributors=positive,
        negative_contributors=negative,
        contradictions=contradictions,
        explanations=[
            f"{v['metric']}: {v['score']:.1f}/100 against {v['cohort_count']} comparable observations."
            for v in positive
        ],
        coverage={k: sum(v is not None for v in g.values()) / len(g) for k, g in groups.items()},
        unavailable_dimensions=[k for k, v in dimensions.items() if v is None],
        eligibility_status="eligible" if row["eligible"] else "outside_assessment_universe",
        price_history_status="complete" if row["market_ready"] else "insufficient_or_inconsistent",
        options_context=dict(status="unavailable", reason="No local options observations."),
        weight_status="Unvalidated starting weights.",
        normalization="Midrank empirical percentile. Configured cohort, separated by financial business model. No historical sector classification.",
    )


def observations_for(con, as_of):
    records = defaultdict(list)
    if "fact" in attached_views(con):
        install_macros(con)
        result = con.execute(
            """SELECT cik,canonical,period_start,period_end,duration_bucket,value,filed_date,
            accession_number,source_tag FROM fundamentals_asof(?)""",
            [as_of],
        ).pl()
        for entry in result.to_dicts():
            records[entry.pop("cik")].append(entry)
    return records


@lru_cache(maxsize=2)
def cohort_at(epoch, as_of, universe_ids, warehouse_dir, filing_cutoff=None):
    cfg = config.get_settings()
    if cfg.warehouse_dir != warehouse_dir:
        raise ValueError("Warehouse changed during assessment")
    with published_read_conn(cfg, epoch=epoch) as con:
        obs = observations_for(con, filing_cutoff or as_of)
        membership = None
        if "universe_snapshot" in attached_views(con):
            stamp = con.execute(
                "SELECT max(snapshot_date) FROM universe_snapshot WHERE snapshot_date<=?", [as_of]
            ).fetchone()[0]
            if stamp:
                membership = {
                    sid
                    for (sid,) in con.execute(
                        "SELECT security_id FROM universe_snapshot WHERE snapshot_date=?", [stamp]
                    ).fetchall()
                }
        market = con.execute(
            """SELECT security_id,date,close,high,low,volume,adjustment_basis,source FROM price_daily
            WHERE security_id IN (SELECT unnest(?)) AND date<=?
            QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY date DESC)<=253""",
            [list(universe_ids), as_of],
        ).pl()
        price_frames = market.partition_by("security_id", as_dict=True)
        benchmark = load_benchmark(con, as_of)
        cohort = []
        for sid, cik in con.execute(
            "SELECT security_id,cik FROM security WHERE security_id IN (SELECT unnest(?)) ORDER BY security_id",
            [list(universe_ids)],
        ).fetchall():
            row = read_company(
                con,
                sid,
                as_of,
                observations=obs[cik],
                filing_cutoff=filing_cutoff,
                prices=price_frames.get((sid,), market.head(0)),
                benchmark=benchmark,
            )
            row["eligible"] = membership is None or sid in membership
            cohort.append(row)
        return cohort, membership is not None


def assessment_cohort(as_of, epoch=None, *, filing_cutoff=None):
    cfg = config.get_settings()
    if epoch is None:
        epoch = require_valid_snapshot(cfg.warehouse_dir)["epoch"]
    ids = tuple(c.security_id for c in load_poc_universe(cfg.universe_path))
    rows, captured = cohort_at(epoch, as_of, ids, cfg.warehouse_dir, filing_cutoff)
    return epoch, rows, captured


def company_assessment(identity: str, as_of: date, *, weights=None, comparisons=True):
    epoch, cohort, captured = assessment_cohort(as_of)
    matches = [
        r
        for r in cohort
        if identity in (r["security_id"], r["ticker"].upper()) or identity.upper() == r["ticker"]
    ]
    with published_read_conn(epoch=epoch) as con:
        if len(matches) != 1:
            if "fact" in attached_views(con):
                install_macros(con)
            row = read_company(con, identity, as_of)
            row["eligible"] = False
        else:
            row = dict(matches[0])
        row["assessment"] = assess(
            row, cohort, config.get_settings().assessment_weights if weights is None else weights
        )
        row["assessment"]["universe_status"] = (
            "captured_membership" if captured else "current_seed_survivorship_bias"
        )
        row["comparisons"] = historical_comparisons(con, row, as_of) if comparisons else {}
        row["correlation_diagnostics"] = redundancy_diagnostics(cohort, row["financial_issuer"])
        row["published_at"] = con.execute(
            "SELECT published_at FROM _warehouse_publication"
        ).fetchone()[0]
    return row


def historical_comparisons(con, row, as_of):
    from datetime import timedelta

    if "fact" not in attached_views(con):
        return {}
    install_macros(con)
    samples = defaultdict(list)
    for index in range(1, 21):
        cutoff = as_of - timedelta(days=91 * index)
        earlier = read_company(con, row["security_id"], cutoff, include_market=False)
        for key in COMPARISONS:
            value = values(earlier).get(key)
            if value is not None and isfinite(value):
                samples[key].append(dict(as_of=str(cutoff), value=value))
    raw = values(row)
    comparisons = {}
    for key in COMPARISONS:
        history = samples[key]
        sample = [point["value"] for point in history]
        current = raw.get(key)
        valid = current is not None and len(sample) >= 8
        comparisons[key] = dict(
            current=current,
            history=history,
            historical_count=len(sample),
            historical_median=median(sample) if valid else None,
            historical_percentile=percentile(current, sample) if valid else None,
            historical_status="available" if valid else "insufficient_comparable_observations",
            sector_median=None,
            sector_percentile=None,
            valid_peer_count=0,
            sector_status="historical_sector_classifications_unavailable",
        )
    return comparisons


def redundancy_diagnostics(cohort, financial_issuer):
    """Measure rank correlation on paired finite observations within a business model."""
    metrics = (
        "trailing_pe",
        "earnings_yield",
        "price_to_fcf",
        "fcf_yield",
        "price_to_sma200",
        "relative_momentum_12m",
        "annualized_volatility",
        "downside_volatility",
        "max_drawdown",
        "debt_to_assets",
        "reported_debt_to_assets",
    )
    observations = [
        values(row)
        for row in cohort
        if row["eligible"] and row["market_ready"] and row["financial_issuer"] == financial_issuer
    ]
    results = []
    for left, right in combinations(metrics, 2):
        pairs = [(row.get(left), row.get(right)) for row in observations]
        matched = [
            (x, y)
            for x, y in pairs
            if x is not None and y is not None and isfinite(x) and isfinite(y)
        ]
        if len(matched) < MIN_COHORT:
            continue
        x, y = map(list, zip(*matched, strict=True))
        if len(set(x)) < 2 or len(set(y)) < 2:
            continue
        coefficient = correlation([percentile(v, x) for v in x], [percentile(v, y) for v in y])
        if abs(coefficient) >= 0.75:
            results.append(dict(metrics=[left, right], spearman=coefficient, sample_size=len(x)))
    return sorted(results, key=lambda row: -abs(row["spearman"]))
