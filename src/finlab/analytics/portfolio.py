"""FIFO portfolio accounting using explicit local quotes and transaction FX.

Amounts are converted into the selected base currency at transaction time;
quotes use valuation-time FX. No provider access and no order execution.
"""

import csv
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Transaction:
    date: date
    security_id: str
    side: str
    quantity: float
    price: float
    currency: str
    fx_to_base: float
    fee: float = 0.0

    def __post_init__(self):
        if (
            self.side not in {"buy", "sell"}
            or self.currency not in {"USD", "CAD"}
            or not self.security_id
        ):
            raise ValueError("Invalid transaction identity, side or currency")
        if (
            not all(
                math.isfinite(x) and x > 0 for x in (self.quantity, self.price, self.fx_to_base)
            )
            or not math.isfinite(self.fee)
            or self.fee < 0
        ):
            raise ValueError("Invalid transaction amount")


def import_transactions(path: Path) -> list[Transaction]:
    with path.open(newline="") as handle:
        return [
            Transaction(
                date=date.fromisoformat(r["date"]),
                security_id=r["security_id"],
                side=r["side"],
                quantity=float(r["quantity"]),
                price=float(r["price"]),
                currency=r["currency"],
                fx_to_base=float(r["fx_to_base"]),
                fee=float(r.get("fee") or 0),
            )
            for r in csv.DictReader(handle)
        ]


def analyze_portfolio(
    transactions: list[Transaction],
    quotes: dict[str, dict],
    *,
    as_of: date,
    base_currency: str,
    corporate_actions: list[dict] | None = None,
) -> dict:
    if base_currency not in {"USD", "CAD"}:
        raise ValueError("Unsupported base currency")
    lots, currencies = {}, {}
    realized = 0.0
    events = [(tx.date, 1, index, tx) for index, tx in enumerate(transactions) if tx.date <= as_of]
    for index, action in enumerate(corporate_actions or []):
        if action["date"] <= as_of and action["action_type"] == "split":
            events.append((action["date"], 0, index, action))
    for _, kind, _, event in sorted(events, key=lambda item: (item[0], item[1], item[2])):
        if kind == 0:
            factor = event["factor"]
            if not math.isfinite(factor) or factor <= 0:
                raise ValueError("Invalid stock split factor")
            for lot in lots.get(event["security_id"], []):
                lot[0] *= factor
                lot[1] /= factor
            continue
        tx = event
        if tx.currency == base_currency and tx.fx_to_base != 1:
            raise ValueError("Base currency transactions must use FX=1")
        if tx.security_id in currencies and currencies[tx.security_id] != tx.currency:
            raise ValueError("One security must have one native currency")
        currencies[tx.security_id] = tx.currency
        queue = lots.setdefault(tx.security_id, [])
        if tx.side == "buy":
            queue.append(
                [tx.quantity, (tx.quantity * tx.price + tx.fee) * tx.fx_to_base / tx.quantity]
            )
        else:
            if tx.quantity > sum(lot[0] for lot in queue) + 1e-10:
                raise ValueError("Sale exceeds held quantity")
            remaining = tx.quantity
            basis = 0.0
            while remaining > 1e-10:
                count = min(remaining, queue[0][0])
                basis += count * queue[0][1]
                remaining -= count
                queue[0][0] -= count
                if queue[0][0] < 1e-10:
                    queue.pop(0)
            realized += (tx.quantity * tx.price - tx.fee) * tx.fx_to_base - basis
    holdings = []
    for sid, queue in lots.items():
        quantity = sum(lot[0] for lot in queue)
        if quantity <= 1e-10:
            continue
        quote = quotes.get(sid)
        if quote is None or quote["date"] > as_of:
            raise ValueError(f"Missing historical valuation for {sid}")
        if quote["currency"] != currencies[sid]:
            raise ValueError("Quote currency does not match holdings")
        if not all(math.isfinite(quote[k]) and quote[k] > 0 for k in ("price", "fx_to_base")):
            raise ValueError("Invalid valuation")
        if quote["currency"] == base_currency and quote["fx_to_base"] != 1:
            raise ValueError("Base quote FX must equal one")
        value = quantity * quote["price"] * quote["fx_to_base"]
        basis = sum(q * cost for q, cost in queue)
        holdings.append(
            dict(
                security_id=sid,
                quantity=quantity,
                currency=currencies[sid],
                sector=quote.get("sector", "Unknown"),
                market_value=value,
                cost_basis=basis,
                unrealized_pnl=value - basis,
                price_date=str(quote["date"]),
            )
        )
    total = sum(h["market_value"] for h in holdings)
    exposure = {}
    sectors = {}
    for h in holdings:
        h["weight"] = h["market_value"] / total if total else 0
        exposure[h["currency"]] = exposure.get(h["currency"], 0) + h["weight"]
        sectors[h["sector"]] = sectors.get(h["sector"], 0) + h["weight"]
    return dict(
        as_of=str(as_of),
        base_currency=base_currency,
        market_value=total,
        realized_pnl=realized,
        unrealized_pnl=sum(h["unrealized_pnl"] for h in holdings),
        holdings=holdings,
        currency_exposure=exposure,
        sector_allocation=sectors,
    )


def additional_investment(
    transactions, quotes, *, security_id, amount_base, as_of, base_currency, corporate_actions=None
):
    if not math.isfinite(amount_base) or amount_base <= 0:
        raise ValueError("Investment amount must be positive")
    quote = quotes[security_id]
    tx = Transaction(
        as_of,
        security_id,
        "buy",
        amount_base / (quote["price"] * quote["fx_to_base"]),
        quote["price"],
        quote["currency"],
        quote["fx_to_base"],
    )
    return dict(
        before=analyze_portfolio(
            transactions,
            quotes,
            as_of=as_of,
            base_currency=base_currency,
            corporate_actions=corporate_actions,
        ),
        after=analyze_portfolio(
            [*transactions, tx],
            quotes,
            as_of=as_of,
            base_currency=base_currency,
            corporate_actions=corporate_actions,
        ),
        assumption="Additional external cash invested at supplied quote; no fees or execution.",
    )


def portfolio_risk(returns, weights):
    """Sample covariance risk and Euler contributions on aligned base returns."""
    import numpy as np

    matrix = np.asarray(returns, dtype=float)
    w = np.asarray(weights, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] < 3 or matrix.shape[1] != len(w):
        raise ValueError("Aligned return matrix and matching weights required")
    if (
        not np.isfinite(matrix).all()
        or not np.isfinite(w).all()
        or (w < 0).any()
        or not np.isclose(w.sum(), 1)
    ):
        raise ValueError("Finite returns and fully invested nonnegative weights required")
    covariance = np.atleast_2d(np.cov(matrix, rowvar=False, ddof=1)) * 252
    variance = float(w @ covariance @ w)
    vol = float(np.sqrt(max(variance, 0)))
    contributions = w * (covariance @ w) / vol if vol else np.zeros_like(w)
    with np.errstate(invalid="ignore", divide="ignore"):
        correlations = np.atleast_2d(np.corrcoef(matrix, rowvar=False))
    return {
        "annualized_volatility": vol,
        "volatility_contributions": contributions.tolist(),
        "correlations": [
            [float(value) if np.isfinite(value) else None for value in row] for row in correlations
        ],
    }
