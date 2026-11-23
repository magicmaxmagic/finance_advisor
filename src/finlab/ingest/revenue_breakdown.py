"""Extract a single reconciled revenue presentation from SEC Inline XBRL."""

import hashlib
import io
import re
from decimal import Decimal
from xml.etree import ElementTree as ET

import polars as pl

from finlab.db.conn import parquet_files
from finlab.ingest.share_classes import DIM, IX, XBRL, _text, share_class_filing
from finlab.ingest.warehouse import publish_table

HTML = "{http://www.w3.org/1999/xhtml}"
SEGMENT = "us-gaap:StatementBusinessSegmentsAxis"
PRODUCT = "srt:ProductOrServiceAxis"
CONTRACT = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
NONCONTRACT = "us-gaap:RevenueNotFromContractWithCustomer"
TOTALS = ("us-gaap:Revenues", "us-gaap:SalesRevenueNet", CONTRACT)
REVENUE_CONCEPTS = {*TOTALS, NONCONTRACT}


def _row_value(node):
    value = re.sub(r"[,\s]", "", _text(node))
    scale = int(node.get("scale", "0"))
    if not re.fullmatch(r"\d+(\.\d+)?", value) or not 0 <= scale <= 9:
        return None
    number = Decimal(value).scaleb(scale)
    if number != number.to_integral_value() or number >= 10**16:
        return None
    return int(-number if node.get("sign") == "-" else number)


def _reconcile_rows(rows, total):
    """Use the disclosed presentation order and reject ambiguous subtotals or axes."""
    if any(row["amount"] < 0 for row in rows):
        return None  # A negative adjustment needs a signed presentation.
    output = []
    group = []
    active_segment = None
    for row in rows:
        segment = row["segment"]
        if row["product"]:
            if group and segment != active_segment:
                return None  # Never reinterpret an unfinished product cohort.
            active_segment = segment
            # Some tables disclose a subtotal inline among its constituent rows.
            # Accept it only if it sums at least two preceding unique products.
            trailing = [
                start
                for start in range(len(group) - 1)
                if sum(item["amount"] for item in group[start:]) == row["amount"]
            ]
            if trailing:
                if len(trailing) != 1:
                    return None
            else:
                group.append(row)
        elif group:
            if active_segment != segment or sum(item["amount"] for item in group) != row["amount"]:
                return None
            output.extend(group)
            group = []
            active_segment = None
        else:
            output.append(row)
    if group or len(output) < 2 or sum(row["amount"] for row in output) != total:
        return None
    identities = {(row["segment"], row["product"], row["concept"]) for row in output}
    if len(identities) != len(output) or len({row["label"] for row in output}) != len(output):
        return None
    return output


def extract_revenue_breakdowns(document, cik, period_end):
    parser = ET.iterparse(io.BytesIO(document), events=("start-ns", "end"))
    bindings = {}
    for event, item in parser:
        if event == "start-ns":
            prefix, uri = item
            bindings.setdefault(prefix, set()).add(uri)
    families = {
        "us-gaap": "http://fasb.org/us-gaap/",
        "srt": "http://fasb.org/srt/",
        "iso4217": "http://www.xbrl.org/2003/iso4217",
    }
    if any(
        len(bindings.get(key, ())) != 1 or not next(iter(bindings[key])).startswith(uri)
        for key, uri in families.items()
    ):
        raise ValueError("Filing lacks trusted revenue taxonomy or USD bindings")
    root = parser.root
    units = {
        unit.get("id")
        for unit in root.iter(XBRL + "unit")
        if len(unit) == 1 and unit.findtext(XBRL + "measure") == "iso4217:USD"
    }
    contexts = {}
    for context in root.iter(XBRL + "context"):
        identity = context.find(f".//{XBRL}identifier")
        start = context.findtext(f".//{XBRL}startDate")
        end = context.findtext(f".//{XBRL}endDate")
        if (
            identity is None
            or identity.get("scheme") != "http://www.sec.gov/CIK"
            or (identity.text or "").lstrip("0") != str(cik)
            or not start
            or end != str(period_end)
            or context.find(f".//{DIM}typedMember") is not None
        ):
            continue
        members = {
            item.get("dimension"): _text(item) for item in context.iter(DIM + "explicitMember")
        }
        contexts[context.get("id")] = (start, members)
    consolidated = {}
    for node in root.iter(IX + "nonFraction"):
        context = contexts.get(node.get("contextRef"))
        if (
            context
            and not context[1]
            and node.get("name") in TOTALS
            and node.get("unitRef") in units
        ):
            consolidated.setdefault((context[0], node.get("name")), set()).add(_row_value(node))
    candidates = {}
    for table in root.iter(HTML + "table"):
        periods = {}
        for tr in table.iter(HTML + "tr"):
            label = tr.find(HTML + "td")
            if label is None or tr.find(f".//{HTML}table") is not None:
                continue
            name = " ".join(_text(label).split())
            for node in tr.iter(IX + "nonFraction"):
                concept = node.get("name")
                context = contexts.get(node.get("contextRef"))
                if (
                    not context
                    or node.get("unitRef") not in units
                    or concept not in REVENUE_CONCEPTS
                ):
                    continue
                start, axes = context
                if axes.keys() - {SEGMENT, PRODUCT} or (PRODUCT in axes and SEGMENT not in axes):
                    continue
                val = _row_value(node)
                if val is not None and name:
                    periods.setdefault(start, []).append(
                        dict(
                            concept=concept,
                            label=name,
                            segment=axes.get(SEGMENT),
                            product=axes.get(PRODUCT),
                            amount=val,
                        )
                    )
        for start, rows in periods.items():
            total_concept = next((key for key in TOTALS if (start, key) in consolidated), None)
            totals = consolidated.get((start, total_concept), set())
            if len(totals) != 1 or None in totals:
                continue
            total = next(iter(totals))
            if (
                len(rows) < 3
                or rows[-1]["amount"] != total
                or rows[-1]["segment"]
                or rows[-1]["concept"] != total_concept
            ):
                continue
            items = rows[:-1]
            if any(
                (row["segment"] and row["concept"] != CONTRACT)
                or (not row["segment"] and row["concept"] != NONCONTRACT)
                for row in items
            ):
                continue
            reconciled = _reconcile_rows(items, total)
            if reconciled:
                candidates.setdefault(start, {})[
                    tuple(tuple(row.values()) for row in reconciled)
                ] = reconciled

    def refines(leaves, alternative):
        for row in alternative:
            if row in leaves:
                continue
            if not row["segment"] or row["product"]:
                return False
            products = [
                leaf for leaf in leaves if leaf["segment"] == row["segment"] and leaf["product"]
            ]
            if len(products) < 2 or sum(leaf["amount"] for leaf in products) != row["amount"]:
                return False
        return True

    selected = {}
    for start, options in candidates.items():
        eligible = [
            rows
            for rows in options.values()
            if all(refines(rows, other) for other in options.values())
        ]
        if len(eligible) == 1:
            selected[start] = eligible[0]
    return selected


def stage_revenue_breakdown(con, settings, accession, document):
    cik, period_end, filed_date, source_url = share_class_filing(con, accession)
    periods = extract_revenue_breakdowns(document, cik, period_end)
    if not periods:
        raise ValueError("No unambiguous revenue presentation reconciles in this filing")
    digest = hashlib.sha256(document).hexdigest()
    rows = [
        {
            **row,
            "ordinal": position,
            "period_start": start,
            "period_end": period_end,
            "cik": cik,
            "accession_number": accession,
            "filed_date": filed_date,
            "source_url": source_url,
            "source_sha256": digest,
        }
        for start, group in sorted(periods.items())
        for position, row in enumerate(group)
    ]
    frame = pl.DataFrame(rows, schema_overrides={"segment": pl.String, "product": pl.String})
    root = settings.warehouse_dir / "revenue_breakdown"
    files = [str(path) for path in parquet_files(root)]
    con.register("_incoming_revenue", frame)
    try:
        con.execute("DROP TABLE IF EXISTS _working_revenue")
        if files:
            con.execute(
                """CREATE TEMP TABLE _working_revenue AS
                SELECT * FROM read_parquet(?) WHERE accession_number<>?
                UNION ALL BY NAME SELECT * FROM _incoming_revenue""",
                [files, accession],
            )
        else:
            con.execute("CREATE TEMP TABLE _working_revenue AS SELECT * FROM _incoming_revenue")
        publish_table(
            con,
            source="_working_revenue",
            root=root,
            order_by=("cik", "period_end", "period_start", "ordinal"),
        )
    finally:
        con.unregister("_incoming_revenue")
    return len(rows)
