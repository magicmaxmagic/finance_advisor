"""Deterministic, cutoff-aware context for the existing company assessment."""

from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from math import isfinite, sqrt
from statistics import median, pstdev, stdev
from zoneinfo import ZoneInfo

from finlab.analytics.snapshot import attached_views
from finlab.ingest.calendar import is_session, last_completed_session

NEW_YORK = ZoneInfo("America/New_York")


def company_history(comparisons, as_of):
    """Describe available sampled values without inferring regimes or breaks."""
    output = {}
    for key, comparison in comparisons.items():
        points = sorted(
            (point["as_of"], point["value"])
            for point in comparison["history"]
            if date.fromisoformat(point["as_of"]) < as_of and isfinite(point["value"])
        )
        values = [value for _, value in points]
        baseline = comparison.get("historical_median")
        current = comparison.get("current")
        if current is not None and isfinite(current) and values and baseline is not None:
            relation = "above" if current > baseline else "below" if current < baseline else "at"
        else:
            relation = "unavailable"
        output[key] = dict(
            observations=len(values),
            first_cutoff=points[0][0] if points else None,
            last_cutoff=points[-1][0] if points else None,
            minimum=min(values) if values else None,
            maximum=max(values) if values else None,
            median=baseline if values else None,
            percentile=comparison.get("historical_percentile") if values else None,
            median_relation=relation,
            recent_change=current - values[-1] if relation != "unavailable" else None,
            observed_level_dispersion=pstdev(values) if len(values) >= 8 else None,
            recent_four_minus_earlier_four=median(values[-4:]) - median(values[-8:-4])
            if len(values) >= 8
            else None,
        )
    return output


def market_state(con, as_of):
    bars = con.execute(
        """WITH benchmark AS (
            SELECT p.security_id, count(*) AS observations
            FROM price_daily p JOIN security s USING(security_id)
            WHERE s.ticker='SPY' AND p.date<=? AND p.source='yahoo'
              AND p.adjustment_basis='yahoo_auto_adjust_false'
            GROUP BY p.security_id ORDER BY observations DESC, p.security_id LIMIT 1
        )
        SELECT p.date,p.close FROM price_daily p JOIN benchmark b USING(security_id)
        WHERE p.date<=? AND p.source='yahoo' AND p.adjustment_basis='yahoo_auto_adjust_false'
        ORDER BY p.date DESC LIMIT 253""",
        [as_of, as_of],
    ).fetchall()
    expected = last_completed_session(datetime.combine(as_of, time(23, 59), NEW_YORK))
    completed = [
        expected - timedelta(days=offset)
        for offset in range(550)
        if is_session(expected - timedelta(days=offset))
    ][:253]
    if [day for day, _ in bars] != completed or any(
        value is None or not isfinite(value) or value <= 0 for _, value in bars
    ):
        return dict(
            status="insufficient_history",
            benchmark="SPY",
            observation_date=str(bars[0][0]) if bars else None,
        )
    closes = [value for _, value in reversed(bars)]
    momentum = closes[-1] / closes[-253] - 1
    distance = closes[-1] / (sum(closes[-200:]) / 200) - 1
    trend = (
        "uptrend"
        if momentum > 0 and distance > 0
        else ("downtrend" if momentum < 0 and distance < 0 else "mixed")
    )
    daily = [current / previous - 1 for previous, current in pairwise(closes)]
    return dict(
        status="available",
        benchmark="SPY",
        observation_date=str(expected),
        source="Yahoo daily close",
        trend=trend,
        price_return_12m=momentum,
        distance_from_sma200=distance,
        realized_volatility_60=stdev(daily[-60:]) * sqrt(252),
        drawdown_from_252_session_high=closes[-1] / max(closes[-252:]) - 1,
        definition="Trend requires agreement between the 252-session price return and distance from the 200-session mean. Volatility uses the last 60 daily price returns.",
    )


def company_events(con, security_id, as_of, tables):
    events = []
    if "filing" in tables:
        fields = {column[0] for column in con.execute("DESCRIBE filing").fetchall()}
        trusted_time = (
            "accepted_at_utc"
            if {"accepted_at_utc", "accepted_at_status"} <= fields
            else "NULL::TIMESTAMPTZ"
        )
        trusted = (
            f"CASE WHEN accepted_at_status='verified_sgml' THEN {trusted_time} END"
            if trusted_time == "accepted_at_utc"
            else trusted_time
        )
        cutoff = datetime.combine(as_of + timedelta(days=1), time.min, NEW_YORK).astimezone(UTC)
        rows = con.execute(
            f"""WITH relevant AS (
                SELECT f.form,f.filed_date,f.accession_number,f.primary_doc_url,{trusted} AS verified_time
                FROM filing f JOIN security s ON s.cik=f.cik WHERE s.security_id=? AND f.filed_date<=?
                  AND f.form IN ('10-K','10-Q','8-K','10-K/A','10-Q/A','8-K/A')
            ) SELECT * FROM relevant WHERE verified_time IS NULL OR verified_time < ?
              ORDER BY filed_date DESC, verified_time DESC NULLS LAST, accession_number DESC LIMIT 15""",
            [security_id, as_of, cutoff],
        ).fetchall()
        for form, day, accession, url, timestamp in rows:
            events.append(
                dict(
                    kind="sec_filing",
                    label=f"SEC {form}",
                    source="SEC EDGAR",
                    event_date=str(day),
                    publication_timestamp=timestamp.isoformat() if timestamp else None,
                    timing="verified_acceptance" if timestamp else "filing_date_only",
                    evidence=accession,
                    source_url=url,
                    limitation="Form and accession identify the filing. No announcement, surprise or price effect was inferred.",
                )
            )
    if "corporate_action" in tables:
        rows = con.execute(
            """SELECT ex_date,action_type,source,factor,amount FROM corporate_action
            WHERE security_id=? AND ex_date<=? ORDER BY ex_date DESC,action_type LIMIT 15""",
            [security_id, as_of],
        ).fetchall()
        for day, action, source, factor, amount in rows:
            events.append(
                dict(
                    kind="corporate_action",
                    label=action.replace("_", " "),
                    source=source,
                    event_date=str(day),
                    publication_timestamp=None,
                    timing="ex_date_only",
                    evidence=None,
                    split_factor=factor,
                    amount_per_share=amount,
                    limitation="Ex-date observation. Announcement time and historical vendor vintage are unavailable.",
                )
            )
    return sorted(
        events, key=lambda event: (event["event_date"], event["kind"], event["label"]), reverse=True
    )[:20]


def company_peers(con, row, cohort, as_of):
    from finlab.analytics.assessment import MIN_COHORT, percentile, values

    latest = con.execute("SELECT max(date) FROM price_daily").fetchone()[0]
    if latest != as_of:
        return dict(status="historical_classifications_unavailable", comparisons={})
    identity = row["security_id"]
    members = [
        peer
        for peer in cohort
        if peer["eligible"]
        and peer["security_id"] != identity
        and peer["financial_issuer"] == row["financial_issuer"]
    ]
    metadata = {
        sid: (industry, sector, cik)
        for sid, industry, sector, cik in con.execute(
            """SELECT s.security_id,e.industry,e.sector,e.cik FROM security s JOIN entity e USING(cik)
               WHERE s.security_id IN (SELECT unnest(?))""",
            [[identity, *[peer["security_id"] for peer in members]]],
        ).fetchall()
    }
    if identity not in metadata:
        return dict(status="classification_unavailable", comparisons={})
    normalized_members = [
        (metadata[p["security_id"]], values(p))
        for p in sorted(members, key=lambda p: p["security_id"], reverse=True)
        if p["security_id"] in metadata and metadata[p["security_id"]][2] != metadata[identity][2]
    ]
    comparisons = {}
    for key, current in values(row).items():
        if (
            key not in row["fundamental_factors"]["factors"]
            or current is None
            or not isfinite(current)
        ):
            continue
        for index, kind in enumerate(("industry", "sector")):
            group = metadata[identity][index]
            if not group or group.casefold() in {"unknown", "unclassified", "other", "n/a"}:
                continue
            by_issuer = {
                labels[2]: v[key]
                for labels, v in normalized_members
                if labels[index] == group and v.get(key) is not None and isfinite(v[key])
            }
            sample = list(by_issuer.values())
            if len(sample) >= MIN_COHORT:
                comparisons[key] = dict(
                    cohort=kind,
                    label=group,
                    count=len(sample),
                    median=median(sample),
                    percentile=percentile(current, sample),
                )
                break
    return dict(
        status="current_classifications" if comparisons else "insufficient_valid_peers",
        minimum_peer_count=MIN_COHORT,
        comparisons=comparisons,
    )


def archived_factor_evidence(as_of):
    from finlab.analytics.validation_view import read_validation
    from finlab.config import get_settings

    try:
        report = read_validation(get_settings().reports_dir / "research")
        if not isinstance(report, dict) or not str(report.get("version")).startswith(
            "signal-research-v3"
        ):
            return dict(status="archive_unavailable", factors={})
        end = date.fromisoformat(report["end"])
        if end > as_of:
            return dict(status="outcomes_after_assessment", factors={})
        factors = {
            key: dict(
                ic=result["ic"].get("mean"),
                interval=result["ic"].get("confidence_95"),
                observations=result["ic"].get("observations"),
                holm_p=result.get("holm_p_value"),
            )
            for key, result in report["summary"]["21"]["factors"].items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return dict(status="archive_unavailable", factors={})
    return dict(
        status="retrospective",
        start=report.get("start"),
        end=report["end"],
        horizon_sessions=21,
        factors=factors,
    )


def financial_context(con, row, as_of, cohort):
    tables = set(attached_views(con))
    return dict(
        version="financial-context-v5.1",
        as_of=str(as_of),
        warehouse_epoch=row["warehouse_epoch"],
        history=company_history(row["comparisons"], as_of),
        market=market_state(con, as_of) if "price_daily" in tables else {"status": "unavailable"},
        events=company_events(con, row["security_id"], as_of, tables),
        peers=company_peers(con, row, cohort, as_of)
        if {"price_daily", "security", "entity"} <= tables
        else {"status": "unavailable", "comparisons": {}},
        predictive_evidence=archived_factor_evidence(as_of),
        macro=dict(status="release_vintages_unavailable"),
        sentiment=dict(status="timestamped_source_unavailable"),
        limitations=[
            "Historical distributions summarize comparable samples, not structural change tests.",
            "SPY price observations exclude reinvested dividends. Market context describes observed conditions.",
            "Price and corporate-action rows can contain provider corrections published after the cutoff.",
        ],
    )
