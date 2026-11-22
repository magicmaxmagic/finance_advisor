"""Company factor details from one published epoch, with share-basis provenance."""

from datetime import date
from math import isfinite, prod

from finlab.analytics.factors import calculate_market_factors
from finlab.analytics.fundamentals import calculate_fundamentals
from finlab.analytics.snapshot import published_read_conn
from finlab.core.pit import install_macros


def company_factors(identity: str, as_of: date) -> dict:
    with published_read_conn() as con:
        matches = con.execute(
            "SELECT s.security_id,s.cik,s.ticker,s.currency FROM security s WHERE s.security_id=? OR (s.ticker=? AND EXISTS(SELECT 1 FROM price_daily p WHERE p.security_id=s.security_id))",
            [identity, identity.upper()],
        ).fetchall()
        if len(matches) != 1:
            raise ValueError("Unknown or ambiguous security; supply its stable identifier")
        sid, cik, ticker, currency = matches[0]
        epoch = con.execute("SELECT epoch FROM _warehouse_publication").fetchone()[0]
        install_macros(con)
        observations = (
            con.execute(
                "SELECT canonical,period_start,period_end,duration_bucket,value,filed_date,accession_number,source_tag FROM fundamentals_asof(?) WHERE cik=? ORDER BY canonical,period_end,filed_date",
                [as_of, cik],
            )
            .pl()
            .to_dicts()
        )
        shares = con.execute(
            "SELECT shares,period_end,filed_date,accession_number FROM common_shares_pit(?) WHERE cik=?",
            [as_of, cik],
        ).fetchone()
        price = con.execute(
            "SELECT close,date FROM price_daily WHERE security_id=? AND date<=? ORDER BY date DESC LIMIT 1",
            [sid, as_of],
        ).fetchone()
        coverage = con.execute(
            "SELECT actions_checked_from,actions_checked_through,price_basis_date,source FROM price_ingestion WHERE security_id=?",
            [sid],
        ).fetchone()
        market_cap = None
        market_cap_inputs = None
        if (
            "-" not in ticker
            and currency == "USD"
            and shares
            and price
            and coverage
            and (as_of - shares[1]).days <= 180
            and (as_of - price[1]).days <= 7
            and coverage[0] <= shares[1]
            and coverage[1] >= coverage[2]
            and coverage[3] == "yahoo"
        ):
            actions = con.execute(
                "SELECT ex_date,factor FROM corporate_action WHERE security_id=? AND action_type='split' AND ex_date>=? AND ex_date<=?",
                [sid, shares[1], coverage[2]],
            ).fetchall()
            if not any(
                day == shares[1] or factor is None or not isfinite(factor) or factor <= 0
                for day, factor in actions
            ):
                factor = prod(factor for _, factor in actions)
                market_cap = price[0] * shares[0] * factor
                market_cap_inputs = dict(
                    reported_common_shares=shares[0],
                    shares_period=str(shares[1]),
                    filed_date=str(shares[2]),
                    accession_number=shares[3],
                    split_factor=factor,
                    price_date=str(price[1]),
                    price=price[0],
                )
        fundamentals = calculate_fundamentals(observations, as_of, market_cap)
        prices = con.execute(
            "SELECT security_id,date,close FROM price_daily WHERE security_id=? AND date<=? ORDER BY date DESC LIMIT 253",
            [sid, as_of],
        ).pl()
        benchmark = con.execute(
            "SELECT p.security_id,p.date,p.close FROM price_daily p JOIN security s USING(security_id) WHERE s.ticker='SPY' AND p.date<=? ORDER BY p.date DESC LIMIT 253",
            [as_of],
        ).pl()
        technical = calculate_market_factors(prices, benchmark, as_of)
        tech = {r["factor"]: r["value"] for r in technical.to_dicts()}
        fundamental = {key: item["value"] for key, item in fundamentals["factors"].items()}

        # Transparent monotonic transforms into 0-100 domain scores. These
        # are rule-based starting scores; no predictive calibration is claimed.
        def transform(key, value):
            if value is None:
                return None
            if key in {"earnings_yield", "fcf_yield", "cash_to_debt"}:
                return 50 + 500 * value
            if key in {
                "price_to_sales",
                "price_to_book",
                "ev_to_operating_ebitda",
                "ev_to_ebit",
                "ev_to_revenue",
                "trailing_pe",
                "price_to_fcf",
            }:
                return 100 - 10 * max(0, value)
            if key in {"net_debt_to_ebitda", "debt_to_equity"}:
                return 100 - 20 * max(0, value)
            if key in {"current_ratio", "interest_coverage"}:
                return min(100, value * 25)
            if key in {"annualized_volatility", "downside_volatility"}:
                return 100 - 150 * value
            if key in {"max_drawdown", "var_95", "cvar_95"}:
                return 100 + 100 * value
            if key == "beta":
                return 100 - 20 * abs(value - 1)
            if key == "rsi":
                return 100 - abs(value - 55) * 1.5
            if key == "golden_cross":
                return 75 if value else 25
            if key == "relative_volume20":
                return 50 + 20 * max(-2, min(2, value - 1))
            return 50 + 100 * value

        def average(keys, source, minimum=2):
            vals = [transform(key, source.get(key)) for key in keys]
            vals = [
                max(0, min(100, float(v)))
                for v in vals
                if v is not None and isinstance(v, (int, float))
            ]
            return round(sum(vals) / len(vals), 1) if len(vals) >= minimum else None

        fundamental_groups = {
            "valuation": average(
                [
                    "earnings_yield",
                    "fcf_yield",
                    "price_to_sales",
                    "price_to_book",
                    "ev_to_operating_ebitda",
                ],
                fundamental,
            ),
            "profitability": average(
                [
                    "roic",
                    "roe",
                    "roa",
                    "gross_margin",
                    "operating_margin",
                    "net_margin",
                    "ebitda_margin",
                    "fcf_margin",
                ],
                fundamental,
            ),
            "growth": average(
                ["revenue_growth", "earnings_growth", "fcf_growth", "growth_acceleration"],
                fundamental,
            ),
            "financial_health": average(
                [
                    "net_debt_to_ebitda",
                    "debt_to_equity",
                    "cash_to_debt",
                    "current_ratio",
                    "interest_coverage",
                ],
                fundamental,
            ),
            "cash_flow_quality": average(["cash_flow_quality", "fcf_margin"], fundamental, 1),
        }
        technical_groups = {
            "trend": average(["price_to_sma200", "sma200_slope", "golden_cross"], tech),
            "momentum": average(
                ["return_3m", "return_6m", "return_12m", "relative_momentum_12m"], tech
            ),
            "confirmation": average(["rsi", "macd", "adx14", "relative_volume20"], tech),
        }
        risk_groups = {
            "market_risk": average(
                [
                    "annualized_volatility",
                    "downside_volatility",
                    "max_drawdown",
                    "beta",
                    "var_95",
                    "cvar_95",
                ],
                tech,
            ),
            "balance_sheet": average(
                ["net_debt_to_ebitda", "debt_to_equity", "current_ratio"], fundamental
            ),
        }

        def dimension(groups, minimum):
            present = [v for v in groups.values() if v is not None]
            return round(sum(present) / len(present), 1) if len(present) >= minimum else None

        dimension_scores = {
            "fundamental": dimension(fundamental_groups, 3),
            "technical": dimension(technical_groups, 2),
            "risk": dimension(risk_groups, 2),
        }
        score_groups = {
            "fundamental": fundamental_groups,
            "technical": technical_groups,
            "risk": risk_groups,
        }
        weights = {"fundamental": 0.60, "technical": 0.25, "risk": 0.15}
        opportunity = (
            round(sum(dimension_scores[k] * w for k, w in weights.items()), 1)
            if all(v is not None for v in dimension_scores.values())
            else None
        )
        technical_score, fundamental_score = (
            dimension_scores["technical"],
            dimension_scores["fundamental"],
        )
        contradictions = []
        if fundamental_score is not None and technical_score is not None:
            if fundamental_score >= 60 and technical_score < 40:
                contradictions.append("Strong fundamentals / weak technical trend")
            if fundamental_score < 40 and (tech.get("return_12m") or 0) > 0.15:
                contradictions.append("Weak fundamentals / strong momentum")
        if (
            fundamental.get("earnings_yield")
            and fundamental["earnings_yield"] > 0.08
            and fundamental.get("net_margin") is not None
            and fundamental["net_margin"] < 0
        ):
            contradictions.append("Attractive earnings yield with deteriorating profitability")
        statements = []
        if (
            tech.get("price_to_sma200") is not None
            and tech["price_to_sma200"] < 0
            and tech.get("sma200_slope") is not None
            and tech["sma200_slope"] < 0
        ):
            statements.append("Price is below a declining 200-day average.")
        if fundamental_score is not None and fundamental_score >= 60:
            statements.append(
                "Available fundamental groups are relatively favorable under the initial rules."
            )
        if (
            tech.get("return_12m") is not None
            and tech["return_12m"] > 0
            and (tech.get("relative_momentum_12m") or 0) > 0
        ):
            statements.append("Twelve-month momentum is positive and ahead of SPY.")
        return dict(
            security_id=sid,
            ticker=ticker,
            as_of=str(as_of),
            warehouse_epoch=epoch,
            fundamental_factors=fundamentals,
            reported_share_market_cap=market_cap,
            market_cap_inputs=market_cap_inputs,
            market_factors=technical.to_dicts(),
            assessment={
                "model_version": "hierarchical-equity-assessment-v1",
                "opportunity_score": opportunity,
                "fundamental_score": dimension_scores["fundamental"],
                "technical_score": dimension_scores["technical"],
                "risk_resilience_score": dimension_scores["risk"],
                "weights": weights,
                "score_groups": score_groups,
                "coverage": {
                    k: sum(v is not None for v in g.values()) / len(g)
                    for k, g in score_groups.items()
                },
                "contradictions": contradictions,
                "explanations": statements,
                "options_context": {
                    "status": "unavailable",
                    "reason": "No options snapshots in the local warehouse; excluded from scores.",
                },
                "weight_status": "Unvalidated starting weights; not a probability of return.",
            },
        )
