"""Prespecified entry and thesis-review observations for research windows."""

from statistics import mean

import numpy as np

from finlab.research.signal_statistics import summarize

RULES = {
    "entry": "Fundamental score >=65 and the observed close above SMA200.",
    "exit_review": "Observed close below SMA200 and annual net margin fell year over year.",
}


def decision_flags(fundamental, trend, margin_change):
    return {
        "entry": fundamental >= 65 and trend > 0
        if fundamental is not None and trend is not None
        else None,
        "exit_review": trend < 0 and margin_change < 0
        if trend is not None and margin_change is not None
        else None,
    }


def decision_outcomes(flags, outcomes, benchmark, cost):
    """Compare each signal with evaluable non-signals over identical forward sessions."""
    results = {}
    for name in RULES:
        flagged, unflagged = [], []
        eligible = 0
        for sid, decisions in flags.items():
            decision = decisions[name]
            if decision is None:
                continue
            eligible += 1
            if sid in outcomes:
                (flagged if decision else unflagged).append(
                    (1 + outcomes[sid]) * (1 - cost) / (1 + cost) - 1
                )
        positive_evidence = (
            (lambda value: value > benchmark)
            if name == "entry"
            else (lambda value: value < benchmark)
        )
        results[name] = dict(
            eligible=eligible,
            matched=len(flagged) + len(unflagged),
            flagged=len(flagged),
            net_return=mean(flagged) if flagged else None,
            net_difference=mean(flagged) - mean(unflagged) if flagged and unflagged else None,
            net_05=float(np.quantile(flagged, 0.05)) if len(flagged) >= 10 else None,
            net_loss_share=mean(value < 0 for value in flagged) if flagged else None,
            false_alarm_share=mean(not positive_evidence(value) for value in flagged)
            if flagged
            else None,
            missed_outcome_share=mean(positive_evidence(value) for value in unflagged)
            if unflagged
            else None,
        )
    return results


def summarize_decisions(folds):
    metrics = (
        "net_return",
        "net_difference",
        "net_05",
        "net_loss_share",
        "false_alarm_share",
        "missed_outcome_share",
    )

    def outcomes(rows):
        return {metric: summarize([row[metric] for row in rows]) for metric in metrics}

    return {
        name: {
            "rule": description,
            "eligible": sum(row["decisions"][name]["eligible"] for row in folds),
            "matched": sum(row["decisions"][name]["matched"] for row in folds),
            "flagged": sum(row["decisions"][name]["flagged"] for row in folds),
            "windows": outcomes([row["decisions"][name] for row in folds]),
            "regimes": {
                label: outcomes([row["decisions"][name] for row in folds if row["regime"] == label])
                for label in ("up", "down", "unclassified")
            },
        }
        for name, description in RULES.items()
    }
