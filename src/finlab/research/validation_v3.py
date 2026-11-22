"""Prespecified factor, ablation and chronological weight research."""

import hashlib
import json
from datetime import timedelta
from itertools import combinations, pairwise
from math import isfinite
from uuid import uuid4

import numpy as np

from finlab import config
from finlab.analytics.assessment import assess, assessment_cohort, values
from finlab.analytics.provenance import archive_sources, code_hash, runtime_versions
from finlab.analytics.rankings import historical_rankings
from finlab.analytics.snapshot import attached_views, published_read_conn, require_valid_snapshot
from finlab.ingest.universe import load_poc_universe
from finlab.research.score_validation import (
    HORIZONS,
    daily_buy_and_hold,
    evaluate_fold,
    forward_price_panel,
    rank_ic,
)
from finlab.research.signal_statistics import holm, summarize
from finlab.research.stats import performance_metrics
from finlab.research.trials import research_trial

VERSION = "signal-research-v3.0"
FACTORS = {
    "trailing_pe": -1,
    "ev_to_operating_ebitda": -1,
    "fcf_yield": 1,
    "roic": 1,
    "revenue_growth": 1,
    "eps_growth": 1,
    "price_to_sma200": 1,
    "return_12m": 1,
    "annualized_volatility": -1,
    "max_drawdown": 1,
    "debt_to_assets": -1,
    "reported_debt_to_assets": -1,
}
MIXES = {
    "fundamental": (1, 0, 0),
    "technical": (0, 1, 0),
    "risk": (0, 0, 1),
    "fundamental_technical": (0.5, 0.5, 0),
    "fundamental_risk": (0.5, 0, 0.5),
    "technical_risk": (0, 0.5, 0.5),
    "opportunity": (0.6, 0.25, 0.15),
    "equal_dimensions": (1 / 3, 1 / 3, 1 / 3),
    "fundamental_heavy": (0.8, 0.1, 0.1),
    "technical_heavy": (0.25, 0.6, 0.15),
}
CANDIDATES = (
    "opportunity",
    "equal_dimensions",
    "fundamental_heavy",
    "technical_heavy",
    "risk_constraint",
    "entry_filter",
)


def eps_growth(row):
    """Compare annual diluted EPS only when one filing supplies both vintages."""
    evidence = row["fundamental_factors"]["filing_evidence"]
    annual = [
        fact for fact in evidence if fact["canonical"] == "eps_diluted" and fact["period_start"]
    ]
    annual.sort(key=lambda fact: fact["period_end"], reverse=True)
    if len(annual) < 2:
        return None
    recent, old = annual[:2]
    from datetime import date

    if (
        recent["accession_number"] != old["accession_number"]
        or old["value"] <= 0
        or not 300
        <= (date.fromisoformat(recent["period_end"]) - date.fromisoformat(old["period_end"])).days
        <= 430
    ):
        return None
    return recent["value"] / old["value"] - 1


def choose_weights(folds, signal_index, *, train, validation, embargo, rolling):
    """Pick a challenger only when the disjoint validation block supports it."""
    mature = [fold for fold in folds if fold["exit_index"] + embargo < signal_index]
    selection = dict(selected=None, train_windows=0, validation_windows=0, tests={})
    if len(mature) < train + validation:
        return selection
    valid = mature[-validation:]
    prefix = [
        fold
        for fold in mature[:-validation]
        if fold["exit_index"] + embargo < valid[0]["signal_index"]
    ]
    training = prefix[-train:] if rolling else prefix
    if len(training) < train:
        return selection
    selection.update(
        train_windows=len(training),
        validation_windows=len(valid),
        train_end=training[-1]["exit_date"],
        validation_end=valid[-1]["exit_date"],
        validation_start=valid[0]["signal_date"],
    )

    def diffs(rows, name):
        pairs = [
            (row["models"][name]["top_excess_net"], row["models"]["fundamental"]["top_excess_net"])
            for row in rows
        ]
        return [a - b if a is not None and b is not None else None for a, b in pairs]

    train_results = {key: summarize(diffs(training, key), draws=500) for key in CANDIDATES}
    tests = {key: summarize(diffs(valid, key), draws=1500) for key in CANDIDATES}
    adjusted = holm({key: value["p_value"] for key, value in tests.items()})
    passed = []
    for key, result in tests.items():
        result["holm_p_value"] = adjusted[key]
        result["training_mean_difference"] = train_results[key]["mean"]
        if (
            adjusted[key] is not None
            and adjusted[key] < 0.05
            and result["confidence_95"][0] > 0
            and (result["training_mean_difference"] or 0) > 0
        ):
            passed.append(key)
    selection.update(
        tests=tests,
        selected=max(passed, key=lambda key: tests[key]["mean"]) if passed else "fundamental",
    )
    return selection


def _book(panel, ids, days, cost):
    if not ids or any(not all(day in panel[sid] for day in days) for sid in ids):
        return None
    return daily_buy_and_hold(panel, ids, days, cost)


def _factor_metrics(scores, outcomes, benchmark, cost):
    if len(scores) < 10 or len(set(scores.values())) < 5:
        return dict(
            available=len(scores),
            matched=sum(sid in outcomes for sid in scores),
            rank_ic=None,
            quintile_net_returns=[None] * 5,
            monotonicity=None,
        )
    sample = evaluate_fold(scores, outcomes, benchmark, cost)
    buckets = [group["net_return"] for group in sample["quintiles"]]
    # Nondecreasing adjacent quintile steps, with all five observed.
    monotonicity = (
        sum(b >= a for a, b in pairwise(buckets)) / 4
        if all(x is not None for x in buckets)
        else None
    )
    return dict(
        available=len(scores),
        matched=sample["matched"],
        rank_ic=sample["rank_ic"],
        quintile_net_returns=buckets,
        monotonicity=monotonicity,
    )


def _model_metrics(scores, outcomes, benchmark, cost, panel, days):
    result = evaluate_fold(scores, outcomes, benchmark, cost)
    result["daily"] = (
        _book(panel, result["quintiles"][-1]["security_ids"], days, cost)
        if result["top_turnover"] is not None
        else None
    )
    return result


def _portfolio_row(ids, panel, days, cost, benchmark):
    path = _book(panel, ids, days, cost)
    return dict(
        rank_ic=None,
        quintiles=[],
        daily=path,
        top_turnover=2.0 if path else None,
        top_excess_net=float(np.prod(np.add(path, 1)) - 1 - benchmark) if path else None,
    )


def _summary(folds, key, domain):
    rows = [fold[domain].get(key) for fold in folds]
    ic = summarize([row["rank_ic"] if row else None for row in rows])
    regime = {
        label: summarize(
            [
                row["rank_ic"] if row else None
                for row, fold in zip(rows, folds, strict=True)
                if fold["regime"] == label
            ]
        )
        for label in ("up", "down", "unclassified")
    }
    result = dict(ic=ic, regime=regime, sector_status="dated_sector_classifications_unavailable")
    if domain == "factors":
        result["coverage"] = dict(
            eligible=sum(fold["universe"]["eligible"] for fold in folds),
            available=sum(row["available"] for row in rows),
            matched=sum(row["matched"] for row in rows),
        )
        result["quintiles"] = [
            summarize([row["quintile_net_returns"][i] for row in rows]) for i in range(5)
        ]
        result["monotonicity"] = summarize([row["monotonicity"] for row in rows])
        return result
    paths = [row["daily"] if row else None for row in rows]
    complete_indices = [i for i, path in enumerate(paths) if path is not None]
    contiguous = bool(complete_indices) and complete_indices == list(
        range(complete_indices[0], len(folds))
    )
    if key != "adaptive_oos":
        contiguous = contiguous and len(complete_indices) == len(folds)
    selected_folds = [folds[i] for i in complete_indices]
    contiguous = contiguous and all(
        a["exit_date"] == b["signal_date"] for a, b in pairwise(selected_folds)
    )
    result["portfolio"] = (
        performance_metrics(
            [v for i in complete_indices for v in paths[i]],
            [v for fold in selected_folds for v in fold["spy_daily"]],
        )
        if contiguous
        else None
    )
    result["rank_stability"] = summarize([fold["rank_stability"].get(key) for fold in folds])
    result["portfolio_period"] = (
        [selected_folds[0]["entry_date"], selected_folds[-1]["exit_date"]] if contiguous else None
    )
    result["excess"] = summarize([row["top_excess_net"] if row else None for row in rows])
    result["turnover"] = summarize([row["top_turnover"] if row else None for row in rows])
    if all(row is not None and len(row.get("quintiles", ())) == 5 for row in rows) and rows:
        result["quintiles"] = [
            summarize([row["quintiles"][i]["net_return"] for row in rows]) for i in range(5)
        ]
    return result


def _collect(start, end, epoch, *, stride, cost_bps, train, validation, embargo, rolling):
    epoch, spy, sessions, panel = forward_price_panel(start, end, epoch)
    with published_read_conn(epoch=epoch) as con:
        metadata = {
            sid: (delisting, reason)
            for sid, delisting, reason in con.execute(
                "SELECT security_id,delisting_date,delisting_reason FROM security WHERE delisting_date IS NOT NULL"
            ).fetchall()
        }
        membership_dates = (
            [
                row[0]
                for row in con.execute(
                    "SELECT DISTINCT snapshot_date FROM universe_snapshot ORDER BY snapshot_date"
                ).fetchall()
            ]
            if "universe_snapshot" in attached_views(con)
            else []
        )
    cost = cost_bps / 10000
    folds = {h: [] for h in HORIZONS}
    next_signal = dict.fromkeys(HORIZONS, 0)
    previous = {h: {} for h in HORIZONS}
    for ix in range(0, len(sessions) - 21, stride):
        horizons = [h for h in HORIZONS if ix >= next_signal[h] and ix + h < len(sessions)]
        if not horizons:
            continue
        date = sessions[ix]
        fact_date = date - timedelta(days=1)
        _, cohort, captured = assessment_cohort(date, epoch, filing_cutoff=fact_date)
        legacy = historical_rankings(date, epoch=epoch, filing_cutoff=fact_date)
        eligible = {r["security_id"]: r for r in cohort if r["eligible"]}
        oriented = {name: {} for name in FACTORS}
        dimensions, raw_values = {}, {}
        legacy_scores = {
            row["security_id"]: row["score"] for row in legacy["rows"] if row["score"] is not None
        }
        for sid, row in eligible.items():
            raw = values(row)
            raw["eps_growth"] = eps_growth(row)
            raw_values[sid] = raw
            assessment = assess(row, cohort)
            scores = tuple(
                assessment[key]
                for key in ("fundamental_score", "technical_score", "risk_resilience_score")
            )
            if (
                row["market_ready"]
                and all(value is not None for value in scores)
                and sid in legacy_scores
            ):
                dimensions[sid] = scores
            for key, direction in FACTORS.items():
                value = raw.get(key)
                market = key in {
                    "price_to_sma200",
                    "return_12m",
                    "annualized_volatility",
                    "max_drawdown",
                }
                if value is not None and isfinite(value) and (not market or row["market_ready"]):
                    oriented[key][sid] = direction * value
        models = {
            model: {
                sid: sum(x * w for x, w in zip(scores, weights, strict=True))
                for sid, scores in dimensions.items()
            }
            for model, weights in MIXES.items()
        }
        models["legacy"] = {sid: legacy_scores[sid] for sid in dimensions}
        floor = float(np.quantile([x[2] for x in dimensions.values()], 0.2)) if dimensions else 0
        models["risk_constraint"] = {
            sid: score[0] for sid, score in dimensions.items() if score[2] >= floor
        }
        models["entry_filter"] = {
            sid: score[0]
            for sid, score in dimensions.items()
            if (raw_values[sid].get("price_to_sma200") or 0) > 0
        }
        correlation = {}
        for left, right in combinations(FACTORS, 2):
            ids = sorted(oriented[left].keys() & oriented[right].keys())
            correlation[f"{left}/{right}"] = rank_ic(
                [oriented[left][sid] for sid in ids], [oriented[right][sid] for sid in ids]
            )
        regime = (
            ("up" if panel[spy][date][1] >= panel[spy][sessions[ix - 126]][1] else "down")
            if ix >= 126
            else "unclassified"
        )
        stamp = next((day for day in reversed(membership_dates) if day <= date), None)
        for horizon in horizons:
            next_signal[horizon] = ix + horizon
            days = sessions[ix + 1 : ix + horizon + 1]
            outcomes = {
                sid: panel[sid][days[-1]][1] / panel[sid][days[0]][0] - 1
                for sid in eligible
                if all(day in panel[sid] for day in days)
            }
            spy_daily = daily_buy_and_hold(panel, [spy], days, cost)
            benchmark = float(np.prod(np.add(spy_daily, 1)) - 1)
            model_rows = {
                key: _model_metrics(score, outcomes, benchmark, cost, panel, days)
                for key, score in models.items()
            }
            model_rows["equal_weight"] = _portfolio_row(
                sorted(dimensions), panel, days, cost, benchmark
            )
            model_rows["spy"] = _portfolio_row([spy], panel, days, cost, benchmark)
            choice = choose_weights(
                folds[horizon],
                ix,
                train=train,
                validation=validation,
                embargo=embargo,
                rolling=rolling,
            )
            model_rows["adaptive_oos"] = model_rows.get(choice["selected"])
            missing = set(eligible) - set(outcomes)
            known_delists = [
                sid
                for sid in sorted(missing)
                if sid in metadata and date < metadata[sid][0] <= days[-1]
            ]
            ranks = {}
            for label, scores in models.items():
                prior = previous[horizon].get(label, {})
                shared = sorted(scores.keys() & prior.keys())
                ranks[label] = rank_ic(
                    [scores[sid] for sid in shared], [prior[sid] for sid in shared]
                )
            previous[horizon] = models
            folds[horizon].append(
                dict(
                    signal_date=str(date),
                    signal_index=ix,
                    filing_cutoff=str(fact_date),
                    entry_date=str(days[0]),
                    exit_date=str(days[-1]),
                    exit_index=ix + horizon,
                    regime=regime,
                    spy_daily=spy_daily,
                    spy_return=benchmark,
                    universe=dict(
                        status="captured_membership_on_seed"
                        if captured
                        else "surviving_universe_study",
                        snapshot_date=str(stamp) if stamp else None,
                        snapshot_age_days=(date - stamp).days if stamp else None,
                        eligible=len(eligible),
                        common_model_names=len(dimensions),
                        covered=len(outcomes),
                        missing_outcomes=sorted(missing),
                        known_delistings=known_delists,
                        delisting_returns_status="unavailable",
                    ),
                    factors={
                        key: _factor_metrics(score, outcomes, benchmark, cost)
                        for key, score in oriented.items()
                    },
                    models=model_rows,
                    selection=choice,
                    feature_correlations=correlation,
                    rank_stability=ranks,
                )
            )
    summary = {}
    model_names = (
        *MIXES,
        "legacy",
        "risk_constraint",
        "entry_filter",
        "equal_weight",
        "spy",
        "adaptive_oos",
    )
    for h, rows in folds.items():
        summary[str(h)] = dict(
            factors={key: _summary(rows, key, "factors") for key in FACTORS},
            models={key: _summary(rows, key, "models") for key in model_names},
        )
        summary[str(h)]["paired_vs_fundamental"] = {}
        for model in model_names:
            if model == "fundamental":
                continue
            changes = []
            for row in rows:
                one, base = row["models"][model], row["models"]["fundamental"]
                a, b = one["top_excess_net"] if one else None, base["top_excess_net"]
                changes.append(a - b if a is not None and b is not None else None)
            summary[str(h)]["paired_vs_fundamental"][model] = summarize(changes)
    for domain in ("factors", "paired_vs_fundamental"):
        tests = {
            (h, key): row["ic"]["p_value"] if domain == "factors" else row["p_value"]
            for h, value in summary.items()
            for key, row in value[domain].items()
        }
        for (h, key), p in holm(tests).items():
            summary[h][domain][key]["holm_p_value"] = p
    return dict(folds=folds, summary=summary)


def run_v3(
    start,
    end,
    *,
    epoch=None,
    stride=21,
    cost_bps=10,
    train=12,
    validation=12,
    embargo=1,
    rolling=False,
):
    if (
        not isfinite(cost_bps)
        or not 0 <= cost_bps < 10000
        or end <= start
        or min(stride, train, validation) < 1
        or embargo < 0
    ):
        raise ValueError("Invalid V3 dates or study parameters")
    cfg = config.get_settings()
    stamp = require_valid_snapshot(cfg.warehouse_dir, epoch)["epoch"]
    source_hash = code_hash()
    metadata = dict(
        version=VERSION,
        warehouse_epoch=stamp,
        code_sha256=source_hash,
        runtime_versions=runtime_versions(),
        start=str(start),
        end=str(end),
        params=dict(
            stride=stride,
            cost_bps=cost_bps,
            train=train,
            validation=validation,
            embargo=embargo,
            rolling=rolling,
        ),
        universe_ids=[c.security_id for c in load_poc_universe(cfg.universe_path)],
        candidates=CANDIDATES,
        weight_grid=MIXES,
        factor_directions=FACTORS,
        evidence_status="Predictive evidence inconclusive.",
        limitations=[
            "Surviving-universe study wherever historical membership was not captured. Captured membership is intersected with the configured seed, not a full market universe.",
            "No historical sector classifications or reliable delisting settlement returns. Holdings with missing outcomes remain in coverage and invalidate affected portfolio paths.",
            "Signal uses prior-calendar-day SEC filings and observed closes. Outputs can reflect retrospective vendor corrections.",
            "Bootstrap intervals use nonoverlapping windows and circular blocks. Small, sparse, or constant samples receive no interval. Research trials are retrospective.",
            "Quintiles are long-only equal-dollar entries; each window liquidates at exit. Spread excludes short borrowing.",
            "The risk constraint removes the lowest resilience quintile before ranking. The entry filter requires a close above SMA200. Both screen an eligible seed and can change concentration.",
        ],
    )
    params = json.dumps(metadata, sort_keys=True, allow_nan=False)
    directory = cfg.reports_dir / "research"
    with research_trial(
        directory / "trials.sqlite", hashlib.sha256(params.encode()).hexdigest(), params
    ) as trial:
        result = _collect(
            start,
            end,
            stamp,
            stride=stride,
            cost_bps=cost_bps,
            train=train,
            validation=validation,
            embargo=embargo,
            rolling=rolling,
        )
        archive_sources(directory, source_hash)
        report = {**result, **metadata}
        encoded = json.dumps(report, allow_nan=False, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()[:16]
        target = directory / f"validation-v3-{start}-{end}-{digest}.json"
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        temporary.write_text(encoded)
        temporary.replace(target)
        latest = directory / "validation-v3-latest.json"
        temporary.write_text(encoded)
        temporary.replace(latest)
        trial.update(result["summary"])
    return target
