"""Read archived research and describe its evidence without changing the model."""

import json
from datetime import date, timedelta
from functools import lru_cache
from itertools import pairwise
from math import isfinite
from pathlib import Path

from finlab.ingest.calendar import is_session
from finlab.research.signal_statistics import holm


def evidence_state(ic: dict, adjusted_p: float | None, *, walk_forward: bool) -> dict:
    """Require adequate dependent samples and family-corrected directional evidence."""
    count = ic.get("observations", 0) or 0
    effective = ic.get("effective_n") or 0
    interval = ic.get("confidence_95")
    if count < 12 or effective < 8 or not interval or len(interval) != 2:
        return dict(
            status="insufficient",
            label="Insufficient evidence",
            reason="The study needs 12 usable windows, an effective sample of eight, and a dependence-aware interval.",
        )
    direction = "positive" if interval[0] > 0 else "negative" if interval[1] < 0 else None
    if direction and adjusted_p is not None and adjusted_p < 0.05:
        qualifier = "out-of-sample" if walk_forward else "historical"
        return dict(
            status=direction,
            label=f"{direction.title()} {qualifier} evidence",
            reason="The rank IC interval excludes zero and the family-adjusted p-value is below 0.05. This tests ranking association.",
        )
    return dict(
        status="inconclusive",
        label="Predictive evidence inconclusive",
        reason="The interval or multiple-test adjustment does not support a directional ranking claim.",
    )


@lru_cache(maxsize=2)
def _load_report(path: Path, revision: int):
    return json.loads(path.read_text())


def read_validation(directory: Path, *, holdout=False, study=None):
    names = (
        (f"validation-{study}-latest.json",)
        if study in {"v7", "v8"}
        else ("validation-v6-latest.json",)
        if holdout
        else (
            "validation-v3-latest.json",
            "score-validation-v2-2020-2022-monthly.json",
        )
    )
    for name in names:
        path = directory / name
        try:
            revision = path.stat().st_mtime_ns
            return _load_report(path, revision)
        except FileNotFoundError:
            continue
    return None


def validation_at_cutoff(directory: Path, as_of: date, *, held_out_only=False):
    for study in ("v8", "v7", "v6", "v3"):
        if held_out_only and study == "v3":
            break
        try:
            report = (
                read_validation(directory)
                if study == "v3"
                else read_validation(directory, holdout=study == "v6", study=study)
            )
            if report and report.get("end") and date.fromisoformat(report["end"]) <= as_of:
                return report
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            continue
    return None


def summarize_validation(report: dict) -> dict:
    result = {
        key: value
        for key, value in report.items()
        if key not in {"folds", "universe_ids", "evidence_status"}
    }
    tests = {
        (horizon, model): row.get("ic", {}).get("p_value")
        for horizon, data in report.get("summary", {}).items()
        for model, row in data.get("models", {}).items()
        if model not in {"spy", "equal_weight"}
    }
    adjusted = holm(tests)
    result["model_evidence"] = {
        horizon: {
            model: evidence_state(
                row.get("ic", {}),
                adjusted.get((horizon, model)),
                walk_forward=bool(report.get("test_start")) or model == "adaptive_oos",
            )
            for model, row in data.get("models", {}).items()
        }
        for horizon, data in report.get("summary", {}).items()
    }
    result["universe_count"] = len(report.get("universe_ids", [])) or None
    result["headline"] = (
        result["model_evidence"]
        .get("21", {})
        .get("opportunity", evidence_state({}, None, walk_forward=False))
    )
    return result


def validation_series(report: dict, horizon: int, series: str) -> dict:
    """Project one archived outcome series and suppress curves with unresolved gaps."""
    windows = report.get("folds", {}).get(str(horizon), [])
    if not report.get("version", "").startswith(
        ("signal-research-v3", "signal-research-v6", "signal-research-v7", "signal-research-v8")
    ):
        return dict(
            windows=[], portfolio=[], reason="Archived window data is unavailable in this report."
        )
    current = []
    entries = []
    for window in windows:
        chosen = window.get("models", {}).get(series) or window.get("factors", {}).get(series) or {}
        entries.append(chosen)
        current.append(
            dict(
                date=window["signal_date"],
                ic=chosen.get("rank_ic"),
                turnover=chosen.get("top_turnover"),
                available=chosen.get("available")
                if series in window.get("factors", {})
                else chosen.get("eligible"),
                matched=chosen.get("matched"),
            )
        )
    selected = [(i, row) for i, row in enumerate(entries) if row.get("daily")]
    portfolio = []
    if not selected:
        return dict(
            windows=current, portfolio=[], reason="No complete daily portfolio path is available."
        )
    first = selected[0][0]
    if [i for i, _ in selected] != list(range(first, len(windows))) or (
        first and series != "adaptive_oos"
    ):
        return dict(
            windows=current,
            portfolio=[],
            reason="Gaps in the archived path prevent a continuous comparison.",
        )
    if any(a["exit_date"] != b["signal_date"] for a, b in pairwise(windows[first:])):
        return dict(windows=current, portfolio=[], reason="Archived windows are not contiguous.")
    wealth = benchmark = 100.0
    peak = benchmark_peak = 100.0
    for index, row in selected:
        window = windows[index]
        start, end = map(date.fromisoformat, (window["entry_date"], window["exit_date"]))
        dates = [
            start + timedelta(days=i)
            for i in range((end - start).days + 1)
            if is_session(start + timedelta(days=i))
        ]
        spy = window.get("spy_daily", [])
        if len(dates) != len(row["daily"]) or len(dates) != len(spy):
            return dict(
                windows=current,
                portfolio=[],
                reason="The archive does not reconcile to a full trading-session calendar.",
            )
        for day, value, reference in zip(dates, row["daily"], spy, strict=True):
            if not (isfinite(value) and isfinite(reference) and value > -1 and reference > -1):
                return dict(
                    windows=current,
                    portfolio=[],
                    reason="An archived daily observation is invalid.",
                )
            wealth *= 1 + value
            benchmark *= 1 + reference
            peak, benchmark_peak = max(peak, wealth), max(benchmark_peak, benchmark)
            portfolio.append(
                dict(
                    date=str(day),
                    wealth=wealth,
                    benchmark=benchmark,
                    drawdown=100 * (wealth / peak - 1),
                    benchmark_drawdown=100 * (benchmark / benchmark_peak - 1),
                )
            )
    return dict(windows=current, portfolio=portfolio, reason=None)
