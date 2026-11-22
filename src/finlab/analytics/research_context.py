"""Benchmark context and comparable historical score changes from local evidence."""

from datetime import date

from finlab.analytics.snapshot import published_read_conn


def market_context(as_of: date, epoch: str | None, *, settings=None) -> dict:
    with published_read_conn(settings, epoch=epoch) as con:
        rows = con.execute(
            "SELECT p.date,p.close FROM price_daily p JOIN security s USING(security_id) WHERE s.ticker='SPY' AND p.date<=? ORDER BY p.date DESC LIMIT 253",
            [as_of],
        ).fetchall()
    if not rows:
        return {"benchmark": "SPY", "status": "unavailable"}
    values = [float(row[1]) for row in reversed(rows)]
    annual = values[-1] / values[-253] - 1 if len(values) >= 253 else None
    ma200 = sum(values[-200:]) / 200 if len(values) >= 200 else None
    regime = "insufficient_data"
    if annual is not None and ma200 is not None:
        regime = (
            "upward_trend"
            if annual > 0 and values[-1] > ma200
            else "downward_trend"
            if annual < 0 and values[-1] < ma200
            else "mixed"
        )
    return dict(
        benchmark="SPY",
        status="available",
        price=values[-1],
        price_date=str(rows[0][0]),
        price_return_12m=annual,
        ma200=ma200,
        regime=regime,
        regime_definition="Price above/below MA200 together with positive/negative 252-session price return; descriptive trend, not a forecast.",
    )


def compare_reports(previous: dict, current: dict) -> dict:
    keys = ("model_version", "code_sha256", "model_parameters", "configured_universe_ids")
    if any(previous.get(key) != current.get(key) for key in keys):
        return {
            "status": "not_comparable",
            "reason": "Model, source, parameters or configured universe changed",
        }
    if previous["as_of"] >= current["as_of"]:
        raise ValueError("Previous report must precede current report")
    prior = {row["security_id"]: row for row in previous["rows"]}
    changes = []
    for row in current["rows"]:
        old = prior.get(row["security_id"])
        delta = (
            row["score"] - old["score"]
            if old and old["score"] is not None and row["score"] is not None
            else None
        )
        changes.append(
            dict(
                security_id=row["security_id"],
                ticker=row["ticker"],
                previous_score=old["score"] if old else None,
                current_score=row["score"],
                change=delta,
            )
        )
    return dict(
        status="comparable",
        previous_as_of=previous["as_of"],
        current_as_of=current["as_of"],
        rows=changes,
        interpretation="Descriptive cross-sectional score changes reflect updated inputs and the available ranking population.",
    )
