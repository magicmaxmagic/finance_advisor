"""Forward-only comparison of fixed scoring rules across historical decision dates."""

import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta
from itertools import pairwise
from math import isfinite

import numpy as np

from finlab import config
from finlab.analytics.assessment import VERSION, assess, assessment_cohort
from finlab.analytics.provenance import code_hash, runtime_versions
from finlab.analytics.rankings import historical_rankings
from finlab.analytics.snapshot import published_read_conn, require_valid_snapshot
from finlab.ingest.calendar import is_session
from finlab.ingest.universe import load_poc_universe
from finlab.research.stats import performance_metrics
from finlab.research.trials import research_trial

HORIZONS = (21, 63, 126, 252)
MODELS = ("five_factor", "opportunity", "fundamental", "technical")


def rank_ic(scores, outcomes):
    """Spearman correlation using average ranks for ties."""
    if len(scores) < 10:
        return None

    def ranks(values):
        return [sum(x < v for x in values) + (sum(x == v for x in values) - 1) / 2 for v in values]

    x, y = ranks(scores), ranks(outcomes)
    return float(np.corrcoef(x, y)[0, 1]) if np.std(x) and np.std(y) else None


def evaluate_fold(scores, returns, benchmark_return, cost):
    ordered = sorted(scores, key=lambda sid: (scores[sid], sid))
    bins = np.array_split(np.asarray(ordered, dtype=object), 5)
    quintiles = []
    for group in bins:
        matched = [returns[sid] for sid in group if sid in returns]
        gross = float(np.mean(matched)) if len(group) and len(matched) == len(group) else None
        quintiles.append(
            dict(
                selected=len(group),
                matched=len(matched),
                security_ids=group.tolist(),
                gross_return=gross,
                net_return=(1 + gross) * (1 - cost) / (1 + cost) - 1 if gross is not None else None,
            )
        )
    matched_ids = [sid for sid in ordered if sid in returns]
    top, bottom = quintiles[-1]["net_return"], quintiles[0]["net_return"]
    return dict(
        eligible=len(ordered),
        matched=len(matched_ids),
        rank_ic=rank_ic(
            [scores[sid] for sid in matched_ids], [returns[sid] for sid in matched_ids]
        ),
        quintiles=quintiles,
        top_minus_bottom_gross=quintiles[-1]["gross_return"] - quintiles[0]["gross_return"]
        if len(ordered) >= 10 and top is not None and bottom is not None
        else None,
        top_excess_net=top - benchmark_return if len(ordered) >= 10 and top is not None else None,
        top_turnover=2.0 if len(ordered) >= 10 and top is not None else None,
    )


def daily_buy_and_hold(panels, ids, days, cost):
    """Daily NAV for equally funded next-open entries and a fee-inclusive exit."""
    nav = [
        sum(panels[sid][day][1] / panels[sid][days[0]][0] for sid in ids) / len(ids) / (1 + cost)
        for day in days
    ]
    nav[-1] *= 1 - cost
    return [current / prior - 1 for prior, current in pairwise([1.0, *nav])]


def run_validation(start: date, end: date, *, stride=21, cost_bps=10, epoch=None):
    cfg = config.get_settings()
    epoch = require_valid_snapshot(cfg.warehouse_dir, epoch)["epoch"]
    metadata = dict(
        model_version=VERSION,
        code_sha256=code_hash(),
        warehouse_epoch=epoch,
        start=str(start),
        end=str(end),
        stride=stride,
        cost_bps_per_side=cost_bps,
        assessment_weights=cfg.assessment_weights,
        five_factor_weights=cfg.factor_weights,
        universe_ids=[company.security_id for company in load_poc_universe(cfg.universe_path)],
        runtime_versions=runtime_versions(),
    )
    parameters = json.dumps(metadata, sort_keys=True, allow_nan=False)
    with research_trial(
        cfg.reports_dir / "research" / "trials.sqlite",
        hashlib.sha256(parameters.encode()).hexdigest(),
        parameters,
    ) as summary:
        result = _run_validation(start, end, stride=stride, cost_bps=cost_bps, epoch=epoch)
        summary.update(result["summary"])
    return {**result, **metadata}


def forward_price_panel(start, end, epoch):
    with published_read_conn(epoch=epoch) as con:
        epoch = con.execute("SELECT epoch FROM _warehouse_publication").fetchone()[0]
        benchmark_id = con.execute(
            """SELECT s.security_id FROM price_daily p JOIN security s USING(security_id)
               WHERE s.ticker='SPY' GROUP BY s.security_id ORDER BY count(*) DESC,s.security_id LIMIT 1"""
        ).fetchone()[0]
        rows = con.execute(
            """SELECT p.security_id,p.date,p.open,p.close,p.adjusted_close FROM price_daily p
               JOIN security s USING(security_id) WHERE p.date BETWEEN ? AND ? AND s.currency='USD'
               AND p.adjustment_basis='yahoo_auto_adjust_false'""",
            [start, end],
        ).fetchall()
    panels = defaultdict(dict)
    for sid, day, opening, close, adjusted in rows:
        if all(v is not None and isfinite(v) and v > 0 for v in (opening, close, adjusted)):
            panels[sid][day] = (opening * adjusted / close, adjusted)
    sessions = [
        start + timedelta(days=i)
        for i in range((end - start).days + 1)
        if is_session(start + timedelta(days=i))
    ]
    if any(day not in panels[benchmark_id] for day in sessions):
        raise ValueError("SPY adjusted history is incomplete for the requested validation dates")
    return epoch, benchmark_id, sessions, panels


def _run_validation(start: date, end: date, *, stride=21, cost_bps=10, epoch=None):
    if end <= start or stride < 1 or not isfinite(cost_bps) or not 0 <= cost_bps < 10000:
        raise ValueError("Invalid dates, stride, or transaction cost")
    source_hash = code_hash()
    cfg = config.get_settings()
    weights = cfg.assessment_weights
    epoch, benchmark_id, sessions, panels = forward_price_panel(start, end, epoch)
    result = {str(h): [] for h in HORIZONS}
    next_signal = dict.fromkeys(HORIZONS, 0)
    cost = cost_bps / 10000
    for index in range(0, len(sessions) - 21, stride):
        active = [h for h in HORIZONS if index >= next_signal[h] and index + h < len(sessions)]
        if not active:
            continue
        day = sessions[index]
        _, rows, captured = assessment_cohort(day, epoch)
        legacy = historical_rankings(day, epoch=epoch)["rows"]
        model_scores = {
            "five_factor": {r["security_id"]: r["score"] for r in legacy if r["score"] is not None}
        }
        fields = {
            "opportunity": "opportunity_score",
            "fundamental": "fundamental_score",
            "technical": "technical_score",
        }
        model_scores.update({key: {} for key in fields})
        for row in rows:
            if not row["eligible"]:
                continue
            assessment = assess(row, rows, weights)
            for model, key in fields.items():
                score = assessment[key]
                if score is not None:
                    model_scores[model][row["security_id"]] = score
        # Start all comparisons from the same signal-time eligible population.
        common = set.intersection(*(set(model_scores[key]) for key in MODELS))
        for horizon in active:
            next_signal[horizon] = index + horizon
            days = sessions[index + 1 : index + horizon + 1]
            entry, exit = days[0], days[-1]
            matched = {
                sid: panels[sid][exit][1] / panels[sid][entry][0] - 1
                for sid in common
                if all(d in panels[sid] for d in days)
            }
            spy = panels[benchmark_id][exit][1] / panels[benchmark_id][entry][0] - 1
            spy_net = (1 + spy) * (1 - cost) / (1 + cost) - 1
            evaluations = {
                key: evaluate_fold({sid: scores[sid] for sid in common}, matched, spy_net, cost)
                for key, scores in model_scores.items()
            }
            for evaluation in evaluations.values():
                evaluation["daily_top_returns"] = (
                    daily_buy_and_hold(
                        panels, evaluation["quintiles"][-1]["security_ids"], days, cost
                    )
                    if evaluation["top_turnover"] is not None
                    else None
                )
            result[str(horizon)].append(
                dict(
                    signal_date=str(day),
                    entry_date=str(entry),
                    exit_date=str(exit),
                    captured_membership=captured,
                    spy_gross_return=spy,
                    spy_net_return=spy_net,
                    spy_daily_returns=daily_buy_and_hold(panels, [benchmark_id], days, cost),
                    model_coverage={k: len(v) for k, v in model_scores.items()},
                    models=evaluations,
                )
            )
    summary = {}
    for horizon, folds in result.items():
        summary[horizon] = {}
        for model in MODELS:
            ics = [
                v["models"][model]["rank_ic"]
                for v in folds
                if v["models"][model]["rank_ic"] is not None
            ]
            paths = [fold["models"][model]["daily_top_returns"] for fold in folds]
            metrics = None
            contiguous = all(a["exit_date"] == b["signal_date"] for a, b in pairwise(folds))
            if paths and contiguous and all(path is not None for path in paths):
                metrics = performance_metrics(
                    [r for path in paths for r in path],
                    [r for fold in folds for r in fold["spy_daily_returns"]],
                )
            summary[horizon][model] = dict(
                folds=len(folds),
                ic_count=len(ics),
                average_ic=float(np.mean(ics)) if ics else None,
                portfolio_metrics=metrics,
            )
    if source_hash != code_hash():
        raise RuntimeError("Source changed during validation; rerun with one consistent revision")
    return dict(
        warehouse_epoch=epoch,
        code_sha256=source_hash,
        model_version=VERSION,
        start=str(start),
        end=str(end),
        stride=stride,
        assessment_weights=weights,
        five_factor_weights=cfg.factor_weights,
        cost_bps_per_side=cost_bps,
        folds=result,
        summary=summary,
        limitations=[
            "Fixed rules evaluated forward without fitting; retrospective experiment, not a prospective holdout.",
            "Horizons do not overlap within each series. Serial dependence remains; no significance claims.",
            "Quintiles open next session and liquidate at period end; two-sided turnover is 200% per completed independent trade.",
            "Top-minus-bottom is a gross return spread, without borrow availability or borrow costs.",
            "Drawdown and portfolio statistics use daily marked values. Gaps or unresolved holdings suppress compounded metrics.",
            "Before captured membership dates the configured universe has survivorship bias. Vendor-adjusted histories can include retrospective revisions.",
        ],
    )
