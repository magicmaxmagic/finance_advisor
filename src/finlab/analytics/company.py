"""Company factor details from one published epoch, with share-basis provenance."""

from datetime import date, timedelta
from math import isfinite, prod

from finlab.analytics.factors import MARKET_DEFINITIONS, calculate_market_factors
from finlab.analytics.fundamentals import calculate_fundamentals
from finlab.ingest.calendar import is_session


def read_company(
    con,
    identity: str,
    as_of: date,
    *,
    observations=None,
    filing_cutoff=None,
    include_market=True,
    prices=None,
    benchmark=None,
) -> dict:
    matches = con.execute(
        "SELECT s.security_id,s.cik,s.ticker,s.currency,e.sector,e.sic FROM security s LEFT JOIN entity e USING(cik) WHERE s.security_id=? OR (s.ticker=? AND EXISTS(SELECT 1 FROM price_daily p WHERE p.security_id=s.security_id))",
        [identity, identity.upper()],
    ).fetchall()
    if len(matches) != 1:
        raise ValueError("Unknown or ambiguous security; supply its stable identifier")
    sid, cik, ticker, currency, sector, sic = matches[0]
    epoch = con.execute("SELECT epoch FROM _warehouse_publication").fetchone()[0]
    if observations is None and "fact" not in {
        row[0] for row in con.execute("SHOW TABLES").fetchall()
    }:
        observations = []
    if observations is None:
        observations = (
            con.execute(
                "SELECT canonical,period_start,period_end,duration_bucket,value,filed_date,accession_number,source_tag FROM fundamentals_asof(?) WHERE cik=? ORDER BY canonical,period_end,filed_date",
                [filing_cutoff or as_of, cik],
            )
            .pl()
            .to_dicts()
        )
    shares = (
        con.execute(
            "SELECT shares,period_end,filed_date,accession_number FROM common_shares_pit(?) WHERE cik=?",
            [filing_cutoff or as_of, cik],
        ).fetchone()
        if observations
        else None
    )
    price = con.execute(
        "SELECT close,date,adjustment_basis,source FROM price_daily WHERE security_id=? AND date<=? ORDER BY date DESC LIMIT 1",
        [sid, as_of],
    ).fetchone()
    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    coverage = (
        con.execute(
            "SELECT actions_checked_from,actions_checked_through,price_basis_date,source FROM price_ingestion WHERE security_id=?",
            [sid],
        ).fetchone()
        if {"price_ingestion", "corporate_action"} <= tables
        else None
    )
    market_cap = None
    market_cap_inputs = None
    if (
        "-" not in ticker
        and con.execute(
            "SELECT count(DISTINCT ticker) FROM security WHERE cik=? AND ticker IS NOT NULL", [cik]
        ).fetchone()[0]
        == 1
        and currency == "USD"
        and shares
        and price
        and coverage
        and price[2:] == ("yahoo_auto_adjust_false", "yahoo")
        and shares[0] > 0
        and (as_of - shares[1]).days <= 180
        and (as_of - price[1]).days <= 7
        and coverage[0] <= shares[1]
        and coverage[2] >= price[1]
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
    financial = bool(
        (sector and sector.casefold() in {"financials", "financial services"})
        or (sic and 6000 <= sic <= 6799)
    )
    fundamentals = calculate_fundamentals(
        observations, as_of, market_cap, financial_issuer=financial
    )
    technical = []
    market_ready = False
    if include_market:
        if prices is None:
            prices = con.execute(
                """SELECT security_id,date,close,high,low,volume,adjustment_basis,source FROM price_daily
                WHERE security_id=? AND date<=? ORDER BY date DESC LIMIT 253""",
                [sid, as_of],
            ).pl()
        if benchmark is None:
            benchmark = load_benchmark(con, as_of)
        technical = calculate_market_factors(prices, benchmark, as_of).to_dicts()
        benchmark_aligned = prices["date"].sort().to_list() == benchmark["date"].sort().to_list()
        for metric in technical:
            if metric["factor"] in {"relative_momentum_12m", "beta"} and not benchmark_aligned:
                metric["value"] = None
                metric["data_status"] = "unmatched_benchmark_sessions"
            metric["definition"] = MARKET_DEFINITIONS[metric["factor"]]
            metric["source_date"] = str(price[1]) if price else None
            metric["source"] = price[3] if price else None
        if prices.height >= 253:
            start = prices["date"].min()
            expected = {
                start + timedelta(days=i)
                for i in range((as_of - start).days + 1)
                if is_session(start + timedelta(days=i))
            }
            market_ready = (
                set(prices["date"]) == expected
                and set(prices["adjustment_basis"]) == {"yahoo_auto_adjust_false"}
                and set(prices["source"]) == {"yahoo"}
            )
    return dict(
        security_id=sid,
        ticker=ticker,
        as_of=str(as_of),
        warehouse_epoch=epoch,
        fundamental_factors=fundamentals,
        reported_share_market_cap=market_cap,
        market_cap_inputs=market_cap_inputs,
        market_factors=technical,
        market_ready=market_ready,
        sector=sector,
        financial_issuer=financial,
        source_date=str(price[1]) if price else None,
        latest_price=price[0] if price else None,
    )


def load_benchmark(con, as_of):
    return con.execute(
        """SELECT p.security_id,p.date,p.close FROM price_daily p JOIN security s USING(security_id)
        WHERE s.ticker='SPY' AND p.date<=? AND p.adjustment_basis='yahoo_auto_adjust_false'
        ORDER BY p.date DESC LIMIT 253""",
        [as_of],
    ).pl()


def company_factors(identity: str, as_of: date) -> dict:
    from finlab.analytics.assessment import company_assessment

    return company_assessment(identity, as_of)
