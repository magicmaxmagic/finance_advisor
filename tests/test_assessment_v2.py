import pytest

from finlab.analytics.assessment import assess, redundancy_diagnostics
from finlab.research.score_validation import evaluate_fold, rank_ic


def issuer(index, *, instant_risk=True, price_ready=True):
    facts = dict(
        earnings_yield=(index + 1) / 100,
        roa=index / 100,
        revenue_growth=index / 100,
        fcf_margin=index / 100,
        debt_to_assets=index / 10,
        current_ratio=2 - index / 20,
        interest_coverage=15 - index,
        trailing_pe=100 / (index + 1),
    )
    if not instant_risk:
        facts.pop("debt_to_assets")
    return dict(
        eligible=True,
        financial_issuer=False,
        market_ready=price_ready,
        fundamental_factors={"factors": {k: {"value": v} for k, v in facts.items()}},
        market_factors=[
            dict(factor=k, value=v)
            for k, v in dict(
                price_to_sma200=index / 100,
                relative_momentum_12m=index / 100,
                annualized_volatility=index / 100,
                max_drawdown=-index / 100,
                var_95=index / 100,
            ).items()
        ],
    )


def test_group_weights_reconcile_and_optional_risk_does_not_reweight():
    cohort = [issuer(index) for index in range(10)]
    assessment = assess(cohort[2], cohort)
    assert sum(v["weight"] for v in assessment["contributions"]) == pytest.approx(1)
    assert 50 + sum(v["points_from_neutral"] for v in assessment["contributions"]) == pytest.approx(
        assessment["opportunity_score"]
    )
    assert assess(cohort[-1], cohort)["risk_resilience_score"] < assessment["risk_resilience_score"]
    assert assessment["breakdown"]["risk"]["market_risk"]["coverage"] == 1
    assert "beta" in assessment["breakdown"]["risk"]["market_risk"]["missing"]
    assert any(
        item["metrics"] == ["trailing_pe", "earnings_yield"] and item["spearman"] == -1
        for item in redundancy_diagnostics(cohort, False)
    )


def test_required_evidence_falls_back_within_slot_and_fails_closed_when_empty():
    cohort = [issuer(index) for index in range(10)]
    company = issuer(4, instant_risk=False)
    assessment = assess(company, cohort)
    slot = assessment["breakdown"]["risk"]["financial_risk"]["slots"][0]
    assert slot["selected"]["metric"] == "interest_coverage"
    assert assessment["opportunity_score"] is not None
    del company["fundamental_factors"]["factors"]["current_ratio"]
    result = assess(company, cohort)
    assert result["risk_resilience_score"] is None
    assert result["opportunity_score"] is None
    assert assess(issuer(1, price_ready=False), cohort)["opportunity_score"] is None
    assert assess(cohort[0], cohort[:2])["opportunity_score"] is None


def test_forward_rank_ic_and_missing_constituent_behavior():
    scores = {str(i): i for i in range(10)}
    returns = {str(i): i / 100 for i in range(10)}
    result = evaluate_fold(scores, returns, 0.01, 0.001)
    assert rank_ic(list(scores.values()), list(reversed(returns.values()))) == pytest.approx(-1)
    assert result["rank_ic"] == pytest.approx(1)
    assert result["top_minus_bottom_gross"] == pytest.approx(0.08)
    assert result["quintiles"][-1]["net_return"] < 0.085
    del returns["9"]
    assert evaluate_fold(scores, returns, 0.01, 0.001)["quintiles"][-1]["net_return"] is None


def test_daily_portfolio_path_reconciles_next_open_and_both_fees():
    from datetime import date

    from finlab.research.score_validation import daily_buy_and_hold

    first, second = date(2020, 1, 2), date(2020, 1, 3)
    panels = {
        "A": {first: (100, 110), second: (110, 120)},
        "B": {first: (200, 190), second: (190, 220)},
    }
    fee = 0.001
    returns = daily_buy_and_hold(panels, ["A", "B"], [first, second], fee)
    assert 1 + returns[0] == pytest.approx((1.10 + 0.95) / 2 / (1 + fee))
    assert (1 + returns[0]) * (1 + returns[1]) == pytest.approx(
        (1.20 + 1.10) / 2 * (1 - fee) / (1 + fee)
    )
