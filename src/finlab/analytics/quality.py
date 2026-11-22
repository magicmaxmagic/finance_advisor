"""Read-only coverage and missing-session diagnostics over a pinned epoch."""

from datetime import date, timedelta

from finlab.analytics.snapshot import published_read_conn
from finlab.ingest.calendar import is_session


def warehouse_quality(as_of: date) -> dict:
    rows = []
    with published_read_conn() as con:
        epoch = con.execute("SELECT epoch FROM _warehouse_publication").fetchone()[0]
        securities = con.execute(
            "SELECT security_id,ticker FROM security ORDER BY ticker"
        ).fetchall()
        for sid, ticker in securities:
            observations = con.execute(
                "SELECT date FROM price_daily WHERE security_id=? AND date<=? ORDER BY date",
                [sid, as_of],
            ).fetchall()
            if not observations:
                rows.append(dict(security_id=sid, ticker=ticker, status="unavailable", rows=0))
                continue
            dates = {r[0] for r in observations}
            start = max(min(dates), as_of - timedelta(days=365))
            day = start
            missing = []
            while day <= as_of:
                if is_session(day) and day not in dates:
                    missing.append(str(day))
                day += timedelta(days=1)
            rows.append(
                dict(
                    security_id=sid,
                    ticker=ticker,
                    rows=len(dates),
                    first_date=str(min(dates)),
                    last_date=str(max(dates)),
                    checked_from=str(start),
                    missing_sessions=missing,
                    status="missing_sessions" if missing else "complete_checked_window",
                )
            )
    return dict(
        as_of=str(as_of),
        warehouse_epoch=epoch,
        rows=rows,
        scope="NYSE regular sessions over the last calendar year, bounded by first available observation; no claim of pre-listing history.",
    )
