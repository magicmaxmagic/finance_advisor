"""Consolidated statement bridges and issuer events from one published epoch."""

from datetime import UTC, datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from finlab.analytics.financial_context import company_events
from finlab.analytics.snapshot import attached_views, published_read_conn

TAGS = {
    "revenue": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ),
    "cost_of_revenue": ("CostOfRevenue", "CostOfGoodsAndServicesSold"),
    "gross_profit": ("GrossProfit",),
    "operating_expenses": ("OperatingExpenses",),
    "total_costs": ("CostsAndExpenses",),
    "research": ("ResearchAndDevelopmentExpense",),
    "sales_marketing": ("SellingAndMarketingExpense",),
    "administration": ("GeneralAndAdministrativeExpense",),
    "selling_general_admin": ("SellingGeneralAndAdministrativeExpense",),
    "operating_income": ("OperatingIncomeLoss",),
    "nonoperating": ("NonoperatingIncomeExpense",),
    "pretax": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ),
    "tax": ("IncomeTaxExpenseBenefit",),
    "net_income": ("ProfitLoss", "NetIncomeLoss"),
}
COMPONENTS = (
    ("research", "sales_marketing", "administration"),
    ("research", "selling_general_admin"),
)
LABELS = {
    "revenue": "Revenue",
    "cost_of_revenue": "Cost of revenue",
    "gross_profit": "Gross profit",
    "operating_expenses": "Operating expenses",
    "research": "Research and development",
    "sales_marketing": "Sales and marketing",
    "administration": "General and administrative",
    "selling_general_admin": "Selling, general and administrative",
    "operating_income": "Operating income",
    "nonoperating": "Net nonoperating result",
    "pretax": "Pretax income",
    "tax": "Income tax",
    "net_income": "Net income",
}


def reconcile_statement(facts):
    """Identify signed amounts and verify each bridge before drawing a flow."""
    values = {}
    for key, tags in TAGS.items():
        for tag in tags:
            observed = facts.get(tag, ())
            if len(observed) == 1 and isfinite(observed[0]):
                values[key] = {"value": observed[0], "source": tag}
                break
            if observed:
                break

    def value(key):
        return values[key]["value"] if key in values else None

    checks = []

    def bridge(label, result, components):
        amounts = [(value(key), coefficient) for key, coefficient in components]
        if any(number is None for number, _ in amounts):
            checks.append({"label": label, "status": "missing", "difference": None})
            return
        derived = sum(number * coefficient for number, coefficient in amounts)
        reported = value(result)
        if reported is None:
            values[result] = {"value": derived, "source": "Calculated from this filing"}
        delta = None if reported is None else reported - derived
        checks.append(
            {
                "label": label,
                "status": "derived"
                if delta is None
                else "matched"
                if abs(delta) <= 1
                else "mismatch",
                "difference": delta,
            }
        )

    bridge(
        "Revenue less cost equals gross profit",
        "gross_profit",
        [("revenue", 1), ("cost_of_revenue", -1)],
    )
    if value("total_costs") is not None:
        bridge(
            "Reported total costs less cost of revenue",
            "operating_expenses",
            [
                ("total_costs", 1),
                ("cost_of_revenue", -1),
            ],
        )
    bridge(
        "Gross profit less operating expenses equals operating income",
        "operating_expenses",
        [
            ("gross_profit", 1),
            ("operating_income", -1),
        ],
    )
    bridge(
        "Operating income plus nonoperating result equals pretax income",
        "nonoperating",
        [
            ("pretax", 1),
            ("operating_income", -1),
        ],
    )
    if value("net_income") is None:
        checks.append(
            {
                "label": "Pretax less tax equals reported net income",
                "status": "missing",
                "difference": None,
            }
        )
    else:
        bridge(
            "Pretax less tax equals reported net income", "net_income", [("pretax", 1), ("tax", -1)]
        )

    components = next(
        (
            list(group)
            for group in COMPONENTS
            if value("operating_expenses") is not None
            and all(value(key) is not None and value(key) >= 0 for key in group)
            and abs(sum(value(key) for key in group) - value("operating_expenses")) <= 1
        ),
        [],
    )
    required = (
        "revenue",
        "cost_of_revenue",
        "gross_profit",
        "operating_expenses",
        "operating_income",
        "pretax",
        "net_income",
        "nonoperating",
        "tax",
    )
    missing = [key for key in required if value(key) is None]
    chart_status = (
        "incomplete"
        if missing
        else "unreconciled"
        if any(check["status"] == "mismatch" for check in checks)
        else "signed_statement"
        if any(
            value(key) <= 0
            for key in ("revenue", "gross_profit", "operating_income", "pretax", "net_income")
        )
        or any(value(key) < 0 for key in ("cost_of_revenue", "operating_expenses"))
        else "available"
    )

    links = []

    def link(source, target, amount, tone):
        if amount > 0:
            links.append({"source": source, "target": target, "amount": amount, "tone": tone})

    if chart_status == "available":
        link("revenue", "gross_profit", value("gross_profit"), "retained")
        link("revenue", "cost_of_revenue", value("cost_of_revenue"), "cost")
        link("gross_profit", "operating_income", value("operating_income"), "retained")
        for key in components or ["operating_expenses"]:
            link("gross_profit", key, value(key), "cost")
        nonop, tax = value("nonoperating"), value("tax")
        link(
            "operating_income",
            "pretax",
            min(value("operating_income"), value("pretax")),
            "retained",
        )
        link("nonoperating_gain", "pretax", nonop, "income")
        link("operating_income", "nonoperating_loss", -nonop, "cost")
        link("pretax", "net_income", min(value("pretax"), value("net_income")), "retained")
        link("pretax", "tax", tax, "cost")
        link("tax_benefit", "net_income", -tax, "income")
    revenue = value("revenue")
    margins = {}
    if revenue and revenue > 0:
        for key in ("gross_profit", "operating_income", "net_income", "cost_of_revenue"):
            if value(key) is not None:
                margins[key] = value(key) / revenue
    return {
        "values": values,
        "checks": checks,
        "missing": missing,
        "links": links,
        "chart_status": chart_status,
        "margins": margins,
        "expense_components": components,
    }


def _read_statements(con, cik, as_of, tables):
    if not {"fact", "filing"} <= tables:
        return []
    fields = {row[0] for row in con.execute("DESCRIBE filing").fetchall()}
    accepted = (
        "CASE WHEN l.accepted_at_status='verified_sgml' THEN l.accepted_at_utc END"
        if {"accepted_at_status", "accepted_at_utc"} <= fields
        else "NULL::TIMESTAMPTZ"
    )
    cutoff = datetime.combine(
        as_of + timedelta(days=1), time.min, ZoneInfo("America/New_York")
    ).astimezone(UTC)
    cursor = con.execute(
        f"""WITH observations AS (
            SELECT f.period_start,f.period_end,f.duration_bucket,f.accession_number,
                l.filed_date,l.primary_doc_url,{accepted} accepted_at,f.tag,f.value
            FROM fact f JOIN filing l USING(cik,accession_number)
            WHERE f.cik=? AND f.taxonomy='us-gaap' AND f.unit='USD' AND f.tag IN (SELECT unnest(?))
                AND f.period_start IS NOT NULL AND f.filed_date=l.filed_date AND l.filed_date<=?
                AND f.period_end=l.period_of_report AND f.period_end<=?
                AND ((f.duration_bucket='annual' AND l.form IN ('10-K','10-K/A','20-F','20-F/A','40-F','40-F/A'))
                    OR (f.duration_bucket='quarter' AND l.form IN ('10-Q','10-Q/A','10-K','10-K/A')))
        )
        SELECT * FROM observations WHERE accepted_at IS NULL OR accepted_at < ?
        QUALIFY dense_rank() OVER (
            PARTITION BY period_end,duration_bucket ORDER BY filed_date DESC,accession_number DESC
        )=1 ORDER BY period_end DESC,duration_bucket,accession_number,period_start,tag""",
        [cik, list({tag for values in TAGS.values() for tag in values}), as_of, as_of, cutoff],
    )
    grouped = {}
    for start, end, kind, accession, filed, url, published, tag, value in cursor.fetchall():
        evidence = grouped.setdefault((end, kind, accession), {})
        period = evidence.setdefault(
            start,
            {
                "id": f"{kind}:{end}",
                "period_kind": kind,
                "period_start": str(start),
                "period_end": str(end),
                "accession": accession,
                "filed_date": str(filed),
                "source_url": url,
                "accepted_at": published.isoformat() if published else None,
                "facts": {},
            },
        )
        period["facts"].setdefault(tag, set()).add(value)
    output = []
    count = {"annual": 0, "quarter": 0}
    for (_, kind, _), windows in grouped.items():
        if count[kind] >= (8 if kind == "annual" else 16):
            continue
        # A report must establish a unique consolidated duration. Comparative
        # YTDs or inconsistent starts cannot be mixed with the selected period.
        candidates = [
            item
            for item in windows.values()
            if any(tag in item["facts"] for tag in TAGS["revenue"])
        ]
        if len(candidates) == 1:
            candidate = candidates[0]
            facts = {tag: list(values) for tag, values in candidate.pop("facts").items()}
            output.append({**candidate, **reconcile_statement(facts)})
            count[kind] += 1
    return output


def _read_events(con, cik, sid, as_of, tables, statements):
    history = company_events(con, sid, as_of, tables)
    periods = {row["accession"]: row["id"] for row in statements}
    for event in history:
        event.update(
            status="historical",
            statement_id=periods.get(event["evidence"]),
            event_at=event.get("publication_timestamp"),
            timezone="America/New_York" if event.get("publication_timestamp") else None,
        )
    official = []
    if "financial_event" in tables:
        events = con.execute(
            """SELECT event_id,kind,label,event_date,event_at,timezone,source,source_url,status,
                      occurred_on,occurrence_url,statement_period_end
               FROM financial_event WHERE cik=? AND announced_on<=? AND event_date>=?
               ORDER BY event_date DESC LIMIT 40""",
            [cik, str(as_of), str(as_of - timedelta(days=365 * 3))],
        ).fetchall()
        for (
            identity,
            kind,
            label,
            day,
            timestamp,
            zone,
            source,
            url,
            status,
            occurred,
            occurrence_url,
            fiscal,
        ) in events:
            completed = occurred is not None and occurred <= str(as_of)
            if status == "historical" and not completed:
                continue
            statement = (
                next(
                    (
                        row["id"]
                        for row in statements
                        if row["period_end"] == fiscal and row["period_kind"] == "quarter"
                    ),
                    None,
                )
                if completed
                else None
            )
            official.append(
                {
                    "evidence": identity,
                    "kind": kind,
                    "label": label,
                    "event_date": day,
                    "event_at": timestamp,
                    "timezone": zone,
                    "status": "historical" if completed else status,
                    "source": source,
                    "source_url": occurrence_url if completed else url,
                    "statement_id": statement,
                }
            )
    return sorted([*official, *history], key=lambda row: row["event_date"], reverse=True)


def financial_performance(identity, as_of, epoch=None):
    with published_read_conn(epoch=epoch) as con:
        matches = con.execute(
            """SELECT security_id,cik,ticker FROM security
               WHERE security_id=? OR (ticker=? AND EXISTS (SELECT 1 FROM price_daily p WHERE p.security_id=security.security_id))""",
            [identity, identity.upper()],
        ).fetchall()
        if len(matches) != 1:
            raise ValueError("Unknown or ambiguous security; supply its stable identifier")
        sid, cik, ticker = matches[0]
        tables = set(attached_views(con))
        statements = _read_statements(con, cik, as_of, tables)
        events = _read_events(con, cik, sid, as_of, tables, statements)
        publication = con.execute("SELECT epoch FROM _warehouse_publication").fetchone()[0]
    return {
        "ticker": ticker,
        "security_id": sid,
        "warehouse_epoch": publication,
        "as_of": str(as_of),
        "currency": "USD",
        "statements": statements,
        "events": events,
        "segment_status": "No verified segment revenue reconciliation is published.",
        "labels": LABELS,
    }
