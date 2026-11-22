"""Next-open forward observations for archived candidates, never signal inputs."""

from datetime import date

from finlab.analytics.snapshot import published_read_conn


def evaluate_recommendations(
    report: dict, horizon_sessions: int = 63, cost_bps_per_side: float = 10
) -> dict:
    if horizon_sessions < 1 or not 0 <= cost_bps_per_side < 10000:
        raise ValueError("Invalid horizon or transaction cost")
    signal_date = date.fromisoformat(report["as_of"])
    rows = []
    with published_read_conn() as con:
        evaluation_epoch = con.execute("SELECT epoch FROM _warehouse_publication").fetchone()[0]
        benchmark_id = report.get("benchmark_security_id")
        if not benchmark_id:
            match = con.execute(
                "SELECT s.security_id FROM security s WHERE s.ticker='SPY' AND EXISTS(SELECT 1 FROM price_daily p WHERE p.security_id=s.security_id) ORDER BY s.security_id LIMIT 1"
            ).fetchone()
            benchmark_id = match[0] if match else None
        market = (
            con.execute(
                "SELECT date,open,close,adjusted_close,adjustment_basis FROM price_daily WHERE security_id=? AND date>? ORDER BY date LIMIT ?",
                [benchmark_id, signal_date, horizon_sessions],
            ).fetchall()
            if benchmark_id
            else []
        )
        dates = [row[0] for row in market]
        for candidate in report["rows"]:
            if candidate.get("score") is None:
                continue
            if not candidate.get("security_id"):
                rows.append(dict(ticker=candidate["ticker"], status="unresolved_legacy_identity"))
                continue
            if len(market) < horizon_sessions:
                rows.append(
                    dict(
                        ticker=candidate["ticker"],
                        status="pending",
                        available_benchmark_sessions=len(market),
                    )
                )
                continue
            observed = con.execute(
                "SELECT date,open,close,adjusted_close,adjustment_basis FROM price_daily WHERE security_id=? AND date IN (SELECT unnest(?)) ORDER BY date",
                [candidate["security_id"], dates],
            ).fetchall()
            if len(observed) != len(market):
                rows.append(dict(ticker=candidate["ticker"], status="missing_matched_sessions"))
                continue
            if any(
                row[3] is None or row[4] != "yahoo_auto_adjust_false"
                for row in (*observed, *market)
            ):
                rows.append(dict(ticker=candidate["ticker"], status="insufficient_adjusted_data"))
                continue
            entry, exit = observed[0], observed[-1]
            first, last = market[0], market[-1]
            stock_return = exit[3] / (entry[1] * entry[3] / entry[2]) - 1
            benchmark_return = last[3] / (first[1] * first[3] / first[2]) - 1
            rate = cost_bps_per_side / 10000
            rows.append(
                dict(
                    ticker=candidate["ticker"],
                    security_id=candidate["security_id"],
                    status="observed",
                    signal_date=str(signal_date),
                    entry_date=str(entry[0]),
                    exit_date=str(exit[0]),
                    score=candidate["score"],
                    total_return=stock_return,
                    net_return=(1 + stock_return) * (1 - rate) / (1 + rate) - 1,
                    benchmark_return=benchmark_return,
                    excess_return=stock_return - benchmark_return,
                )
            )
    return dict(
        evaluation_epoch=evaluation_epoch,
        as_of=str(signal_date),
        horizon_sessions=horizon_sessions,
        cost_bps_per_side=cost_bps_per_side,
        rows=rows,
        limitation="Descriptive follow-up of archived candidates; retrospective data revisions and universe selection prevent a claim of prospective robustness.",
    )
