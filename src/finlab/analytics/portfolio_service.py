"""Portfolio input handling with published local valuations only."""

import csv
import io
from dataclasses import replace
from datetime import date

import polars as pl

from finlab.analytics.portfolio import (
    Transaction,
    additional_investment,
    analyze_portfolio,
    portfolio_risk,
)
from finlab.analytics.snapshot import published_read_conn


def portfolio_from_csv(
    text: str,
    *,
    as_of: date,
    base_currency: str,
    foreign_fx: float | None = None,
    scenario_security: str | None = None,
    scenario_amount: float | None = None,
) -> dict:
    try:
        transactions = [
            Transaction(
                date.fromisoformat(r["date"]),
                r["security_id"],
                r["side"],
                float(r["quantity"]),
                float(r["price"]),
                r["currency"],
                float(r["fx_to_base"]),
                float(r.get("fee") or 0),
            )
            for r in csv.DictReader(io.StringIO(text))
        ]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "CSV requires date,security_id,side,quantity,price,currency,fx_to_base,fee"
        ) from exc
    if not transactions:
        raise ValueError("Supply at least one transaction")
    transactions = [tx for tx in transactions if tx.date <= as_of]
    if not transactions:
        raise ValueError("No transactions existed by the requested historical date")
    quotes = {}
    actions = []
    price_frame = None
    normalized = []
    with published_read_conn() as con:
        ids = {t.security_id for t in transactions} | (
            {scenario_security} if scenario_security else set()
        )
        resolved = {}
        for identity in ids:
            matches = con.execute(
                "SELECT s.security_id FROM security s WHERE s.security_id=? OR (s.ticker=? AND EXISTS(SELECT 1 FROM price_daily p WHERE p.security_id=s.security_id))",
                [identity, identity.upper()],
            ).fetchall()
            if len(matches) != 1:
                raise ValueError(
                    f"Unknown or ambiguous security: {identity}; use stable security ID"
                )
            sid = matches[0][0]
            resolved[identity] = sid
            quote = con.execute(
                "SELECT p.date,p.close,s.currency,e.sector FROM price_daily p JOIN security s USING(security_id) LEFT JOIN entity e ON e.cik=s.cik WHERE p.security_id=? AND p.date<=? ORDER BY p.date DESC LIMIT 1",
                [sid, as_of],
            ).fetchone()
            if quote:
                if "price_ingestion" not in {
                    row[0] for row in con.execute("SHOW TABLES").fetchall()
                }:
                    raise ValueError("Price share-basis metadata is unavailable")
                coverage = con.execute(
                    "SELECT actions_checked_from,actions_checked_through,price_basis_date,source FROM price_ingestion WHERE security_id=?",
                    [sid],
                ).fetchone()
                if (
                    not coverage
                    or coverage[0] > min(tx.date for tx in transactions)
                    or coverage[1] < coverage[2]
                    or coverage[3] != "yahoo"
                ):
                    raise ValueError(f"Incomplete corporate-action coverage for {identity}")
                future_factor = con.execute(
                    "SELECT coalesce(product(factor),1.0) FROM corporate_action WHERE security_id=? AND action_type='split' AND ex_date>? AND ex_date<=?",
                    [sid, as_of, coverage[2]],
                ).fetchone()[0]
                for action_date, factor in con.execute(
                    "SELECT ex_date,factor FROM corporate_action WHERE security_id=? AND action_type='split' AND ex_date<=? ORDER BY ex_date",
                    [sid, as_of],
                ).fetchall():
                    actions.append(
                        dict(date=action_date, security_id=sid, action_type="split", factor=factor)
                    )
                fx = 1.0 if quote[2] == base_currency else foreign_fx
                if fx is None:
                    raise ValueError("Supply valuation FX for foreign-currency holdings")
                quotes[sid] = dict(
                    date=quote[0],
                    price=quote[1] * future_factor,
                    currency=quote[2],
                    sector=quote[3],
                    fx_to_base=fx,
                )
        price_frame = con.execute(
            "SELECT security_id,date,adjusted_close FROM price_daily WHERE security_id IN (SELECT unnest(?)) AND date<=? QUALIFY row_number() OVER(PARTITION BY security_id ORDER BY date DESC)<=253",
            [list(resolved.values()), as_of],
        ).pl()
        normalized = [replace(tx, security_id=resolved[tx.security_id]) for tx in transactions]
    if scenario_security:
        if scenario_amount is None:
            raise ValueError("Scenario amount is required")
        result = additional_investment(
            normalized,
            quotes,
            security_id=resolved[scenario_security],
            amount_base=scenario_amount,
            as_of=as_of,
            base_currency=base_currency,
            corporate_actions=actions,
        )
    else:
        result = analyze_portfolio(
            normalized, quotes, as_of=as_of, base_currency=base_currency, corporate_actions=actions
        )
    states = [result["before"], result["after"]] if scenario_security else [result]
    ids = sorted({h["security_id"] for state in states for h in state["holdings"]})
    aligned = (
        price_frame.filter(pl.col("security_id").is_in(ids))
        .pivot(on="security_id", index="date", values="adjusted_close")
        .sort("date")
        if ids
        else pl.DataFrame()
    )
    if ids and set(ids).issubset(aligned.columns):
        aligned = aligned.select("date", *ids).drop_nulls()
    for state in states:
        state["risk"] = {"status": "insufficient_data"}
        if (
            state["market_value"] > 0
            and aligned.height >= 61
            and set(ids).issubset(aligned.columns)
        ):
            matrix = aligned.select(ids).to_numpy()
            returns = matrix[1:] / matrix[:-1] - 1
            weights_by_id = {h["security_id"]: h["weight"] for h in state["holdings"]}
            risk = portfolio_risk(returns, [weights_by_id.get(sid, 0.0) for sid in ids])
            state["risk"] = {
                **risk,
                "status": "historical_current_weight_estimate",
                "observations": len(returns),
                "security_order": ids,
                "fx_assumption": "Constant FX; FX volatility is not included",
            }
    return {
        "result": result,
        "assumptions": [
            "FIFO accounting; supplied transaction FX and fees.",
            "Valuation uses last stored close on or before the requested date.",
            "Splits adjust quantities and unit cost basis. Historical quotes are restored to their valuation-date share basis. Dividend cash and account cash balances are not included in holdings market value.",
        ],
    }
