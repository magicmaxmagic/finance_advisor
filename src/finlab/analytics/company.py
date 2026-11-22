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
        return dict(
            security_id=sid,
            ticker=ticker,
            as_of=str(as_of),
            warehouse_epoch=epoch,
            fundamental_factors=fundamentals,
            reported_share_market_cap=market_cap,
            market_cap_inputs=market_cap_inputs,
            market_factors=technical.to_dicts(),
        )
