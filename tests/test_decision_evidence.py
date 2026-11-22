import pytest

from finlab.research.decision_evidence import decision_flags, decision_outcomes


def test_distinct_entry_and_thesis_review_conditions_use_only_available_inputs():
    assert decision_flags(None, -0.1, -0.02) == {"entry": None, "exit_review": True}
    assert decision_flags(70, 0.05, None) == {"entry": True, "exit_review": None}
    assert decision_flags(70, -0.05, 0.02) == {"entry": False, "exit_review": False}


def test_decisions_record_costs_unmatched_outcomes_and_false_alarms():
    flags = {
        "A": decision_flags(70, 0.1, 0),
        "B": decision_flags(50, -0.1, -0.01),
        "C": decision_flags(None, -0.1, -0.02),
    }
    result = decision_outcomes(flags, {"A": 0.05, "B": 0.1}, 0.06, 0.001)
    entry, exit_review = result["entry"], result["exit_review"]
    assert entry["net_return"] == pytest.approx(1.05 * 0.999 / 1.001 - 1)
    assert entry["missed_outcome_share"] == 1
    assert exit_review["eligible"] == 3
    assert exit_review["matched"] == 2
    assert exit_review["false_alarm_share"] == 1
