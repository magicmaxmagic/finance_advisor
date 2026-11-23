"""Point-in-time financial events from the published local warehouse."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from finlab.analytics.snapshot import attached_views, published_read_conn
from finlab.ingest.universe import load_poc_universe


def event_calendar(start, end, cutoff, tickers=(), kind=None):
    if end < start or (end - start).days > 62:
        raise ValueError("Select a calendar interval of at most 63 days.")
    companies = {company.ticker: company for company in load_poc_universe()}
    requested = {ticker.upper().strip() for ticker in tickers}
    if requested - companies.keys():
        raise ValueError("Calendar filters must use tracked tickers.")
    members = {
        company.cik: symbol
        for symbol, company in companies.items()
        if not requested or symbol in requested
    }
    seen_at = datetime.combine(
        cutoff + timedelta(days=1), time.min, ZoneInfo("America/New_York")
    ).astimezone(UTC)
    rows = []
    with published_read_conn() as con:
        tables = set(attached_views(con))
        fields = {row[0] for row in con.execute("DESCRIBE filing").fetchall()}
        accepted = (
            "CASE WHEN accepted_at_status='verified_sgml' THEN accepted_at_utc END"
            if {"accepted_at_status", "accepted_at_utc"} <= fields
            else "NULL::TIMESTAMPTZ"
        )
        if not kind or kind == "sec_filing":
            filings = con.execute(
                f"""WITH listed AS (
                SELECT cik, accession_number, filed_date, form, primary_doc_url, {accepted} AS verified
                FROM filing WHERE cik IN (SELECT unnest(?)) AND filed_date BETWEEN ? AND ?
                  AND form IN ('10-Q','10-K','8-K','10-Q/A','10-K/A','8-K/A','20-F','40-F')
                ) SELECT * FROM listed WHERE verified IS NULL OR verified < ?
                ORDER BY filed_date DESC,accession_number""",
                [list(members), start, min(end, cutoff), seen_at],
            ).fetchall()
            rows.extend(
                dict(
                    id=acc,
                    ticker=members[cik],
                    kind="sec_filing",
                    label=f"SEC {form}",
                    event_date=str(day),
                    event_at=stamp.isoformat() if stamp else None,
                    timezone="America/New_York" if stamp else None,
                    status="historical",
                    source="SEC EDGAR",
                    source_url=url,
                    statement_period_end=None,
                )
                for cik, acc, day, form, url, stamp in filings
            )
        if "corporate_action" in tables and kind in {None, "dividend", "corporate_action"}:
            actions = con.execute(
                """SELECT p.security_id,s.ticker,p.ex_date,p.action_type,p.source
                FROM corporate_action p JOIN security s USING(security_id)
                WHERE s.cik IN (SELECT unnest(?)) AND p.ex_date BETWEEN ? AND ?
                ORDER BY p.ex_date DESC""",
                [list(members), start, min(end, cutoff)],
            ).fetchall()
            for sid, symbol, day, action, source in actions:
                category = "dividend" if action == "cash_dividend" else "corporate_action"
                if (
                    symbol not in companies
                    or (requested and symbol not in requested)
                    or (kind and kind != category)
                ):
                    continue
                rows.append(
                    dict(
                        id=f"{sid}:{day}:{action}",
                        ticker=symbol,
                        kind=category,
                        label="Cash dividend ex-date"
                        if category == "dividend"
                        else action.replace("_", " ").capitalize(),
                        event_date=str(day),
                        event_at=None,
                        timezone=None,
                        status="historical",
                        source=source,
                        source_url=None,
                        statement_period_end=None,
                    )
                )
        if "financial_event" in tables and kind != "sec_filing":
            evidence = con.execute(
                """SELECT event_id,cik,kind,label,event_date,event_at,timezone,source,source_url,status,
                occurred_on,occurrence_url,statement_period_end FROM financial_event
                WHERE cik IN (SELECT unnest(?)) AND event_date BETWEEN ? AND ?
                AND announced_on<=? AND (CAST(? AS VARCHAR) IS NULL OR kind=?)""",
                [list(members), str(start), str(end), str(cutoff), kind, kind],
            ).fetchall()
            for (
                identity,
                cik,
                event_kind,
                label,
                day,
                at,
                zone,
                source,
                url,
                status,
                occurred,
                occurrence,
                fiscal,
            ) in evidence:
                completed = occurred is not None and str(occurred) <= str(cutoff)
                if status == "historical" and not completed:
                    continue
                if not completed and str(day) < str(cutoff):
                    status = "unverified_occurrence"
                statement = None
                if completed and fiscal and "fact" in tables:
                    known = con.execute(
                        f"""SELECT EXISTS (SELECT 1 FROM filing f
                        JOIN fact USING(cik,accession_number)
                        WHERE cik=? AND f.period_of_report=CAST(? AS DATE)
                        AND f.filed_date<=? AND fact.filed_date=f.filed_date
                        AND fact.period_end=f.period_of_report AND fact.duration_bucket='quarter'
                        AND f.form IN ('10-Q','10-Q/A','10-K','10-K/A')
                        AND (coalesce(({accepted}) < ?, true)))""",
                        [cik, fiscal, cutoff, seen_at],
                    ).fetchone()[0]
                    statement = fiscal if known else None
                rows.append(
                    dict(
                        id=identity,
                        ticker=members[cik],
                        kind=event_kind,
                        label=label,
                        event_date=str(day),
                        event_at=at,
                        timezone=zone,
                        status="historical" if completed else status,
                        source=source,
                        source_url=occurrence if completed else url,
                        statement_period_end=statement,
                    )
                )
    rows.sort(key=lambda row: (row["event_date"], row["event_at"] or "", row["ticker"], row["id"]))
    return {"events": rows, "start": str(start), "end": str(end), "as_of": str(cutoff)}
