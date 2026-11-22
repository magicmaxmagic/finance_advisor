"""Company factor details from one published epoch, with share-basis provenance."""

from datetime import date, timedelta
from math import isfinite, prod

from finlab.analytics.factors import MARKET_DEFINITIONS, calculate_market_factors, sma200_slope
from finlab.analytics.fundamentals import calculate_fundamentals, diagnostic
from finlab.ingest.calendar import is_session


def _complete_price_window(prices, as_of, observations):
    if prices.height != observations:
        return False
    start = prices["date"].min()
    expected = {
        day for i in range((as_of - start).days + 1) if is_session(day := start + timedelta(days=i))
    }
    return (
        set(prices["date"]) == expected
        and set(prices["adjustment_basis"]) == {"yahoo_auto_adjust_false"}
        and set(prices["source"]) == {"yahoo"}
    )


def _reported_market_value(con, sid, cik, ticker, currency, shares, price, coverage, as_of):
    if (
        "-" in ticker
        or con.execute(
            "SELECT count(DISTINCT ticker) FROM security WHERE cik=? AND ticker IS NOT NULL", [cik]
        ).fetchone()[0]
        != 1
    ):
        return (
            None,
            None,
            diagnostic(
                "UNSUPPORTED",
                "The security catalogue cannot establish one common share class for this issuer. "
                "Valuation requires filing-backed shares by economic class and compatible class prices.",
            )
            | {"detail_code": "MULTI_CLASS_SECURITY"},
        )
    return _value_on_reported_basis(con, sid, currency, shares, price, coverage, as_of)


def _value_on_reported_basis(con, sid, currency, shares, price, coverage, as_of):
    if currency != "USD":
        return None, None, diagnostic("UNSUPPORTED", "Market value requires a supported USD quote.")
    if not shares or not price or not coverage:
        return (
            None,
            None,
            diagnostic(
                "SOURCE_MISSING",
                "A reported share count, closing quote, or split coverage is missing.",
            )
            | ({"detail_code": "SHARES_UNAVAILABLE"} if not shares else {}),
        )
    if (as_of - shares[1]).days > 180 or (as_of - price[1]).days > 7:
        return (
            None,
            None,
            diagnostic("STALE_DATA", "The reported shares or closing quote is stale."),
        )
    if (
        price[2:] != ("yahoo_auto_adjust_false", "yahoo")
        or not isfinite(shares[0])
        or shares[0] <= 0
        or not isfinite(price[0])
        or price[0] <= 0
        or coverage[0] > shares[1]
        or coverage[2] < price[1]
        or coverage[1] < coverage[2]
        or coverage[3] != "yahoo"
    ):
        return (
            None,
            None,
            diagnostic(
                "UNSUPPORTED",
                "The share count and closing quote cannot be aligned on a verified split basis.",
            ),
        )
    actions = con.execute(
        "SELECT ex_date,factor FROM corporate_action WHERE security_id=? AND action_type='split' AND ex_date>=? AND ex_date<=?",
        [sid, shares[1], coverage[2]],
    ).fetchall()
    if any(
        day == shares[1] or factor is None or not isfinite(factor) or factor <= 0
        for day, factor in actions
    ):
        return (
            None,
            None,
            diagnostic("UNSUPPORTED", "The share basis at a reported split is ambiguous."),
        )
    factor = prod(factor for _, factor in actions)
    market_cap = price[0] * shares[0] * factor
    return (
        market_cap,
        dict(
            reported_common_shares=shares[0],
            shares_period=str(shares[1]),
            filed_date=str(shares[2]),
            accession_number=shares[3],
            share_source_tag=shares[4],
            split_factor=factor,
            price_date=str(price[1]),
            price=price[0],
        ),
        None,
    )


def _class_market_value(con, cik, cutoff, as_of, price_date):
    """Use one reconciled class snapshot and one shared market observation date."""
    cursor = con.execute(
        """SELECT class_member,listed_symbol,shares,period_end,filed_date,accession_number,
                  source_url,source_sha256,issuer_common_shares
           FROM share_class_fact WHERE cik=? AND period_end<=? AND filed_date<=?
           QUALIFY dense_rank() OVER (ORDER BY period_end DESC,filed_date DESC,accession_number DESC)=1
           ORDER BY class_member""",
        [cik, cutoff, cutoff],
    )
    keys = [column[0] for column in cursor.description]
    rows = [dict(zip(keys, item, strict=True)) for item in cursor.fetchall()]
    if not rows:
        return None, None, None
    unresolved = []
    used_securities = set()
    for row in rows:
        row["value"] = None
        if row["shares"] == 0:
            row["value"] = 0
            continue
        if not row["listed_symbol"]:
            row["reason"] = "UNPRICED_ECONOMIC_CLASS"
        else:
            prices = con.execute(
                """SELECT s.security_id,s.currency,p.close,p.date,p.adjustment_basis,p.source,
                          i.actions_checked_from,i.actions_checked_through,i.price_basis_date,i.source
                   FROM security s JOIN price_daily p USING(security_id)
                   JOIN price_ingestion i USING(security_id)
                   WHERE s.cik=? AND s.ticker=? AND p.date=?""",
                [cik, row["listed_symbol"], price_date],
            ).fetchall()
            if len(prices) != 1 or prices[0][0] in used_securities:
                row["reason"] = "CLASS_PRICE_UNAVAILABLE"
            else:
                sid, currency, *quote = prices[0]
                amount, inputs, failure = _value_on_reported_basis(
                    con,
                    sid,
                    currency,
                    (
                        row["shares"],
                        row["period_end"],
                        row["filed_date"],
                        row["accession_number"],
                        "us-gaap:CommonStockSharesOutstanding (class)",
                    ),
                    tuple(quote[:4]),
                    tuple(quote[4:]),
                    as_of,
                )
                if failure:
                    row["reason"] = failure["code"]
                else:
                    row.update(
                        value=amount,
                        security_id=sid,
                        price=inputs["price"],
                        price_date=inputs["price_date"],
                        split_factor=inputs["split_factor"],
                    )
                    used_securities.add(sid)
        if row["value"] is None:
            unresolved.append(row["reason"])
    head = rows[0]
    if unresolved:
        return (
            None,
            None,
            {
                **diagnostic(
                    "UNSUPPORTED",
                    "Issuer value needs a verified price for each outstanding economic class. "
                    "See class evidence for the unresolved inputs.",
                ),
                "detail_code": "MULTI_CLASS_SECURITY",
                "share_class_evidence": rows,
            },
        )
    return (
        sum(row["value"] for row in rows),
        {
            "class_count": len(rows),
            "shares_period": str(head["period_end"]),
            "filed_date": str(head["filed_date"]),
            "accession_number": head["accession_number"],
            "price_date": str(price_date),
            "share_source_tag": "SEC Inline XBRL reconciled common classes",
        },
        {"share_class_evidence": rows},
    )


def _reported_common_shares(con, cik, cutoff, as_of, tables):
    shares = con.execute(
        """SELECT shares,period_end,filed_date,accession_number,'dei:EntityCommonStockSharesOutstanding'
           FROM common_shares_pit(?) WHERE cik=?""",
        [cutoff, cik],
    ).fetchone()
    if shares and 0 <= (as_of - shares[1]).days <= 180:
        return shares
    if "filing" not in tables:
        return shares
    # Legacy DEI provides the preferred cover count. A U.S. GAAP balance-sheet
    # observation is a fallback only at the matching filing's fiscal close.
    # The caller still rejects issuers with ambiguous economic classes.
    fiscal = con.execute(
        """SELECT max(f.value), f.period_end, f.filed_date, f.accession_number,
                  'us-gaap:CommonStockSharesOutstanding'
           FROM fact f JOIN filing l USING(cik,accession_number)
           WHERE f.cik=? AND f.taxonomy='us-gaap' AND f.tag='CommonStockSharesOutstanding'
             AND f.unit='shares' AND f.period_start IS NULL AND f.period_end=l.period_of_report
             AND f.period_end<=? AND f.filed_date=l.filed_date AND l.filed_date<=?
             AND l.form IN ('10-K','10-Q','10-K/A','10-Q/A','10-KT','10-QT','20-F','20-F/A','40-F','40-F/A')
           GROUP BY f.period_end,f.filed_date,f.accession_number
           ORDER BY f.period_end DESC,f.filed_date DESC,f.accession_number DESC LIMIT 1
           """,
        [cik, cutoff, cutoff],
    ).fetchone()
    if fiscal and (not shares or fiscal[1] > shares[1]) and 0 <= (as_of - fiscal[1]).days <= 180:
        # Check the selected vintage, so inconsistent rows never fall back to
        # an earlier vintage masquerading as a current observation.
        verified = con.execute(
            """SELECT count(DISTINCT value)=1 AND bool_and(isfinite(value) AND value>0)
               FROM fact WHERE cik=? AND accession_number=? AND period_end=? AND period_start IS NULL
                 AND taxonomy='us-gaap' AND tag='CommonStockSharesOutstanding' AND unit='shares'""",
            [cik, fiscal[3], fiscal[1]],
        ).fetchone()[0]
        if verified:
            return fiscal
    return shares


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
    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    shares = (
        _reported_common_shares(con, cik, filing_cutoff or as_of, as_of, tables)
        if observations
        else None
    )
    price = con.execute(
        "SELECT close,date,adjustment_basis,source FROM price_daily WHERE security_id=? AND date<=? ORDER BY date DESC LIMIT 1",
        [sid, as_of],
    ).fetchone()
    coverage = (
        con.execute(
            "SELECT actions_checked_from,actions_checked_through,price_basis_date,source FROM price_ingestion WHERE security_id=?",
            [sid],
        ).fetchone()
        if {"price_ingestion", "corporate_action"} <= tables
        else None
    )
    market_cap = market_cap_inputs = market_cap_diagnostic = share_class_evidence = None
    if {"share_class_fact", "price_ingestion", "corporate_action"} <= tables:
        market_cap, market_cap_inputs, class_evidence = _class_market_value(
            con,
            cik,
            filing_cutoff or as_of,
            as_of,
            price[1] if price else None,
        )
        if class_evidence:
            share_class_evidence = class_evidence.pop("share_class_evidence")
            market_cap_diagnostic = class_evidence or None
    if share_class_evidence is None:
        market_cap, market_cap_inputs, market_cap_diagnostic = _reported_market_value(
            con, sid, cik, ticker, currency, shares, price, coverage, as_of
        )
    financial = bool(
        (sector and sector.casefold() in {"financials", "financial services"})
        or (sic and 6000 <= sic <= 6799)
    )
    fundamentals = calculate_fundamentals(
        observations,
        as_of,
        market_cap,
        financial_issuer=financial,
        market_cap_diagnostic=market_cap_diagnostic,
    )
    technical = []
    market_ready = False
    if include_market:
        if prices is None:
            prices = con.execute(
                """SELECT security_id,date,close,high,low,volume,adjustment_basis,source FROM price_daily
                WHERE security_id=? AND date<=? ORDER BY date DESC LIMIT 400""",
                [sid, as_of],
            ).pl()
        if benchmark is None:
            benchmark = load_benchmark(con, as_of)
        ordered = prices.sort("date")
        recent = ordered.tail(253)
        technical = calculate_market_factors(recent, benchmark, as_of).to_dicts()
        benchmark_aligned = recent["date"].to_list() == benchmark["date"].sort().to_list()
        market_ready = _complete_price_window(recent, as_of, 253)
        slope_window = ordered.tail(400)
        slope = (
            sma200_slope(slope_window["close"])
            if _complete_price_window(slope_window, as_of, 400)
            else None
        )
        for metric in technical:
            key = metric["factor"]
            if key in {"relative_momentum_12m", "beta"} and not benchmark_aligned:
                metric["value"] = None
                metric["data_status"] = "unmatched_benchmark_sessions"
                metric["diagnostic"] = diagnostic(
                    "INSUFFICIENT_HISTORY", "Asset and SPY sessions do not match."
                )
            if key == "sma200_slope":
                if slope is not None:
                    metric.update(value=slope, data_status="complete")
                else:
                    metric["diagnostic"] = diagnostic(
                        "INSUFFICIENT_HISTORY",
                        "Two contiguous 200-session blocks on the same price basis are required.",
                    )
            if metric["value"] is None and "diagnostic" not in metric:
                metric["diagnostic"] = diagnostic(
                    "INSUFFICIENT_HISTORY" if recent.height < 253 else "INVALID_DENOMINATOR",
                    "The available price or volume series cannot support the required window or denominator.",
                )
            metric["definition"] = MARKET_DEFINITIONS[key]
            metric["source_date"] = str(price[1]) if price else None
            metric["source"] = price[3] if price else None
    return dict(
        security_id=sid,
        ticker=ticker,
        as_of=str(as_of),
        warehouse_epoch=epoch,
        fundamental_factors=fundamentals,
        reported_share_market_cap=market_cap,
        market_cap_inputs=market_cap_inputs,
        market_cap_diagnostic=market_cap_diagnostic,
        share_class_evidence=share_class_evidence,
        share_observation=(
            dict(
                period_end=str(shares[1]),
                filed_date=str(shares[2]),
                accession_number=shares[3],
                source_tag=shares[4],
                fresh=0 <= (as_of - shares[1]).days <= 180,
            )
            if shares
            else None
        ),
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
