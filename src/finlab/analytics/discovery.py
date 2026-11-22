"""Compact local research summaries for the overview and screener."""

from finlab import config
from finlab.analytics.assessment import VERSION, assess, assessment_cohort, values
from finlab.analytics.research_context import market_context
from finlab.analytics.snapshot import published_read_conn
from finlab.analytics.status import latest_research_date


def discovery(as_of=None):
    cutoff = as_of or latest_research_date()
    cfg = config.get_settings()
    epoch, cohort, captured = assessment_cohort(cutoff)
    with published_read_conn(epoch=epoch) as con:
        metadata = {
            sid: (name, currency)
            for sid, name, currency in con.execute(
                "SELECT s.security_id,e.name,s.currency FROM security s JOIN entity e USING(cik)"
            ).fetchall()
        }
    rows = []
    for row in cohort:
        evidence = assess(row, cohort, cfg.assessment_weights)
        raw = values(row)
        sid = row["security_id"]
        name, currency = metadata.get(sid, (row["ticker"], None))
        rows.append(
            dict(
                ticker=row["ticker"],
                name=name,
                sector=row["sector"],
                currency=currency,
                price=row["latest_price"],
                price_date=row["source_date"],
                opportunity=evidence["opportunity_score"],
                fundamental=evidence["fundamental_score"],
                technical=evidence["technical_score"],
                risk=evidence["risk_resilience_score"],
                pe=raw.get("trailing_pe"),
                ev_ebitda=raw.get("ev_to_operating_ebitda"),
                trend=raw.get("price_to_sma200"),
                momentum=raw.get("return_3m"),
                volatility=raw.get("annualized_volatility"),
                valuation=raw.get("earnings_yield"),
                profitability=raw.get("roa"),
                strengths=evidence["positive_contributors"][:1],
                concerns=evidence["negative_contributors"][:1],
                contradictions=evidence["contradictions"],
                eligible=row["eligible"],
            )
        )
    return dict(
        as_of=str(cutoff),
        model=VERSION,
        epoch=epoch,
        captured_membership=captured,
        market=market_context(cutoff, epoch),
        rows=rows,
    )
