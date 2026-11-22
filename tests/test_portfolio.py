from datetime import date

import pytest

from finlab.analytics.portfolio import Transaction, additional_investment, analyze_portfolio

DAY = date(2020, 1, 1)


def test_fifo_fees_and_transaction_fx_are_not_revalued():
    tx = [
        Transaction(DAY, "A", "buy", 10, 10, "USD", 1.3, 1),
        Transaction(DAY, "A", "buy", 10, 20, "USD", 1.4),
        Transaction(DAY, "A", "sell", 15, 30, "USD", 1.5, 2),
    ]
    quotes = {"A": dict(date=DAY, price=40, fx_to_base=1.2, currency="USD")}
    result = analyze_portfolio(tx, quotes, as_of=DAY, base_currency="CAD")
    assert result["realized_pnl"] == pytest.approx((450 - 2) * 1.5 - (101 * 1.3 + 5 * 20 * 1.4))
    assert result["market_value"] == 240
    assert result["unrealized_pnl"] == 100


def test_scenario_adds_external_cash_without_mutating_transactions():
    tx = [Transaction(DAY, "A", "buy", 10, 10, "USD", 1)]
    quotes = {
        "A": dict(date=DAY, price=10, fx_to_base=1, currency="USD"),
        "B": dict(date=DAY, price=20, fx_to_base=1, currency="USD"),
    }
    result = additional_investment(
        tx, quotes, security_id="B", amount_base=100, as_of=DAY, base_currency="USD"
    )
    assert result["after"]["market_value"] == 200
    assert [h["weight"] for h in result["after"]["holdings"]] == [0.5, 0.5]
    assert len(tx) == 1


def test_oversells_and_future_quotes_are_rejected():
    with pytest.raises(ValueError, match="exceeds"):
        analyze_portfolio(
            [Transaction(DAY, "A", "sell", 1, 10, "USD", 1)], {}, as_of=DAY, base_currency="USD"
        )


def test_risk_contributions_sum_to_portfolio_volatility():
    from finlab.analytics.portfolio import portfolio_risk

    result = portfolio_risk([[0.01, 0.02], [-0.01, 0.01], [0.03, -0.01], [0.0, 0.0]], [0.5, 0.5])
    assert sum(result["volatility_contributions"]) == pytest.approx(result["annualized_volatility"])


def test_split_changes_quantity_without_creating_profit_or_cost_basis():
    from datetime import timedelta

    tx = [Transaction(DAY, "A", "buy", 10, 100, "USD", 1)]
    split_day = DAY + timedelta(days=1)
    quotes = {"A": dict(date=split_day, price=50, fx_to_base=1, currency="USD")}
    result = analyze_portfolio(
        tx,
        quotes,
        as_of=split_day,
        base_currency="USD",
        corporate_actions=[dict(date=split_day, security_id="A", action_type="split", factor=2)],
    )
    assert result["holdings"][0]["quantity"] == 20
    assert result["market_value"] == 1000
    assert result["unrealized_pnl"] == 0
