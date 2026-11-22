"""Published-warehouse historical research rankings; no provider access."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from math import isfinite, prod, sqrt
from statistics import stdev

import polars as pl

from finlab import config
from finlab.analytics.scoring import score_universe
from finlab.analytics.snapshot import attached_views, published_read_conn
from finlab.core.pit import install_macros
from finlab.ingest.calendar import NY, is_session, last_completed_session
from finlab.ingest.universe import load_poc_universe

MODEL_VERSION = "five-family-ranking-v5"


def _number(value):
    return float(value) if value is not None and isfinite(float(value)) else None


def _market(records, cutoff):
    output = dict(
        price=None,
        price_date=None,
        momentum_1m=None,
        momentum_3m=None,
        momentum_12m=None,
        volatility=None,
        max_drawdown=None,
        price_status="insufficient_data",
    )
    if not records:
        return output
    ordered = list(reversed(records))
    output["price_date"] = str(ordered[-1][0])
    closes = [_number(row[1]) for row in ordered]
    if any(value is None or value <= 0 for value in closes):
        output["price_status"] = "invalid_price"
        return output
    output["price"] = closes[-1]
    for name, window in (("momentum_1m", 21), ("momentum_3m", 63), ("momentum_12m", 252)):
        if len(closes) > window:
            output[name] = closes[-1] / closes[-window - 1] - 1
    daily = [current / previous - 1 for previous, current in pairwise(closes)]
    output["volatility"] = stdev(daily) * sqrt(252) if len(daily) >= 60 else None
    peak = closes[0]
    drawdown = 0.0
    for close in closes:
        peak = max(peak, close)
        drawdown = min(drawdown, close / peak - 1)
    output["max_drawdown"] = drawdown
    expected_end = last_completed_session(datetime.combine(cutoff, time(23, 59), NY))
    dates = {row[0] for row in records}
    day = min(dates)
    missing = []
    while day <= expected_end:
        if is_session(day) and day not in dates:
            missing.append(str(day))
        day += timedelta(days=1)
    output["missing_sessions"] = missing
    output["price_status"] = (
        "stale"
        if (expected_end - max(dates)).days > 7
        else "missing_sessions"
        if missing
        else "complete"
    )
    return output


def historical_rankings(
    as_of: date,
    *,
    epoch: str | None = None,
    universe_ids: list[str] | None = None,
    factor_weights: dict[str, float] | None = None,
    settings=None,
) -> dict:
    """End-of-day ranks using filed facts, completed closes and covered share bases."""
    cfg = settings or config.get_settings()
    weights = factor_weights if factor_weights is not None else cfg.factor_weights
    if set(weights) - {"value", "quality", "growth", "momentum", "risk"}:
        raise ValueError("Unknown factor family")
    rows = []
    with published_read_conn(cfg, epoch=epoch) as con:
        attached = set(attached_views(con))
        publication = con.execute(
            "SELECT epoch,published_at FROM _warehouse_publication"
        ).fetchone()
        if not {"security", "entity", "price_daily"}.issubset(attached):
            return dict(
                as_of=str(as_of), rows=[], reason="Warehouse price/security data unavailable"
            )
        configured_ids = (
            universe_ids
            if universe_ids is not None
            else [company.security_id for company in load_poc_universe(cfg.universe_path)]
        )
        definition_ids = list(configured_ids)
        universe_status = "current_configured_research_seed"
        universe_date = None
        if "universe_snapshot" in attached:
            universe_date = con.execute(
                "SELECT max(snapshot_date) FROM universe_snapshot WHERE snapshot_date<=?", [as_of]
            ).fetchone()[0]
        eligible_ids = None
        if universe_date is not None:
            eligible_ids = {
                row[0]
                for row in con.execute(
                    "SELECT security_id FROM universe_snapshot WHERE snapshot_date=? AND security_id IN (SELECT unnest(?))",
                    [universe_date, configured_ids],
                ).fetchall()
            }
            universe_status = "captured_membership"
        securities = con.execute(
            "SELECT s.security_id,s.ticker,e.name,e.sector,s.currency,s.cik FROM security s JOIN entity e ON e.cik=s.cik WHERE s.security_id IN (SELECT unnest(?)) ORDER BY s.ticker",
            [configured_ids],
        ).fetchall()
        by_security = defaultdict(list)
        prices = con.execute(
            "SELECT security_id,date,close FROM price_daily WHERE date<=? AND date>=?::DATE-INTERVAL '500 days' QUALIFY row_number() OVER(PARTITION BY security_id ORDER BY date DESC)<=253 ORDER BY security_id,date DESC",
            [as_of, as_of],
        ).fetchall()
        for sid, day, close in prices:
            by_security[sid].append((day, close))
        facts = defaultdict(list)
        if "fact" in attached:
            install_macros(con)
            observations = con.execute(
                "SELECT cik,canonical,value,period_end,filed_date,accession_number FROM fundamentals_asof(?) WHERE duration_bucket='annual' AND canonical IN ('revenue','eps_diluted','net_income') ORDER BY cik,period_end DESC,filed_date DESC,canonical",
                [as_of],
            ).fetchall()
            for cik, *record in observations:
                facts[cik].append(tuple(record))
        coverage = {}
        splits = defaultdict(list)
        if "price_ingestion" in attached:
            coverage = {
                row[0]: row[1:]
                for row in con.execute(
                    "SELECT security_id,actions_checked_from,actions_checked_through,price_basis_date,source FROM price_ingestion"
                ).fetchall()
            }
        if "corporate_action" in attached:
            for sid, ex_date, factor in con.execute(
                "SELECT security_id,ex_date,factor FROM corporate_action WHERE action_type='split'"
            ).fetchall():
                splits[sid].append((ex_date, factor))
        benchmark_id = con.execute(
            "SELECT s.security_id FROM security s WHERE s.ticker='SPY' AND EXISTS(SELECT 1 FROM price_daily p WHERE p.security_id=s.security_id) ORDER BY s.security_id LIMIT 1"
        ).fetchone()
        benchmark_records = by_security.get(benchmark_id[0], []) if benchmark_id else []
        benchmark = _market(benchmark_records, as_of)
        for sid, ticker, name, sector, currency, cik in securities:
            market = _market(by_security[sid], as_of)
            annual = facts[cik]
            revenues = [r for r in annual if r[0] == "revenue"]
            eps = next((r for r in annual if r[0] == "eps_diluted"), None)
            margin = growth = earnings_yield = basis_factor = None
            revenue = _number(revenues[0][1]) if revenues else None
            if revenues and (as_of - revenues[0][2]).days <= 550:
                income = next(
                    (r for r in annual if r[0] == "net_income" and r[2] == revenues[0][2]), None
                )
                if income is not None and revenue is not None and revenue > 0:
                    value = _number(income[1])
                    margin = value / revenue if value is not None else None
                if len(revenues) > 1 and 300 <= (revenues[0][2] - revenues[1][2]).days <= 430:
                    previous = _number(revenues[1][1])
                    if previous is not None and previous > 0 and revenue is not None:
                        growth = revenue / previous - 1
            meta = coverage.get(sid)
            if (
                eps
                and (as_of - eps[2]).days <= 550
                and currency == "USD"
                and "-" not in ticker
                and meta
                and meta[0] <= eps[3]
                and meta[1] >= meta[2]
                and meta[3] == "yahoo"
            ):
                actions = [(day, factor) for day, factor in splits[sid] if eps[3] <= day <= meta[2]]
                if not any(
                    day == eps[3] or _number(factor) is None or factor <= 0
                    for day, factor in actions
                ):
                    basis_factor = prod(factor for _, factor in actions)
                    eps_value = _number(eps[1])
                    if eps_value is not None and market["price"] is not None and basis_factor > 0:
                        earnings_yield = eps_value / basis_factor / market["price"]
            benchmark_return = None
            own = by_security[sid]
            if (
                len(own) == 253
                and len(benchmark_records) == 253
                and own[0][0] == benchmark_records[0][0]
                and own[-1][0] == benchmark_records[-1][0]
                and benchmark["price_status"] == "complete"
            ):
                benchmark_return = benchmark["momentum_12m"]
            rows.append(
                dict(
                    security_id=sid,
                    ticker=ticker,
                    name=name,
                    sector=sector,
                    currency=currency,
                    **market,
                    universe_eligible=eligible_ids is None or sid in eligible_ids,
                    earnings_yield_status="available"
                    if earnings_yield is not None
                    else "unresolved_share_class"
                    if "-" in ticker
                    else "insufficient_fundamentals_or_share_basis",
                    earnings_yield=earnings_yield,
                    eps_share_basis_factor=basis_factor,
                    net_margin=margin,
                    revenue_growth=growth,
                    revenue=revenue,
                    benchmark_12m=benchmark_return,
                    relative_momentum_12m=market["momentum_12m"] - benchmark_return
                    if market["momentum_12m"] is not None and benchmark_return is not None
                    else None,
                    fundamental_evidence=[
                        dict(
                            concept=r[0],
                            value=_number(r[1]),
                            period_end=str(r[2]),
                            filed_date=str(r[3]),
                            accession_number=r[4],
                        )
                        for r in annual[:12]
                    ],
                )
            )
    if rows:
        inputs = []
        for row in rows:
            factors = dict(
                value=row["earnings_yield"],
                quality=row["net_margin"],
                growth=row["revenue_growth"],
                momentum=row["momentum_12m"],
                risk=-row["volatility"] if row["volatility"] is not None else None,
            )
            if not row["universe_eligible"]:
                factors = {key: None for key in factors}
            for factor, value in factors.items():
                inputs.append(
                    dict(
                        security_id=row["security_id"],
                        factor=factor,
                        value=value,
                        sector=row["sector"],
                    )
                )
        scores = score_universe(pl.DataFrame(inputs), weights, as_of, sector_neutral=True)
        lookup = {row["security_id"]: row for row in scores.to_dicts()}
        for row in rows:
            scored = lookup[row["security_id"]]
            row.update(
                score=scored["overall_score"],
                data_status=scored["data_status"],
                model_version=MODEL_VERSION,
                factor_weights=weights,
                normalization=scored["normalization"],
                factor_scores={
                    key: _number(value) for key, value in scored["factor_scores"].items()
                },
            )
            if not row["universe_eligible"]:
                row.update(score=None, data_status="unresolved_universe_membership")
            if row["price_status"] != "complete":
                row.update(score=None, data_status=row["price_status"])
            row["category"] = (
                "Insufficient data"
                if row["score"] is None
                else "Research candidate"
                if row["score"] >= 60
                else "Neutral"
            )
    rows.sort(key=lambda row: (row["score"] is None, -(row["score"] or 0), row["ticker"]))
    return dict(
        as_of=str(as_of),
        warehouse_epoch=publication[0],
        published_at=publication[1],
        model_version=MODEL_VERSION,
        benchmark_security_id=benchmark_id[0] if benchmark_id else None,
        configured_universe_ids=definition_ids,
        universe_status=universe_status,
        universe_snapshot_date=str(universe_date) if universe_date else None,
        model_parameters={
            "factor_weights": weights,
            "sector_neutral": True,
            "minimum_volatility_observations": 60,
            "maximum_annual_age_days": 550,
        },
        method="Configured weighted annual earnings yield, net margin, revenue growth, 12-month price momentum and negative volatility; sector normalized with explicit global fallback.",
        rows=rows,
    )
