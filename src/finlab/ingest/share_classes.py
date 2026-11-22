"""Normalize reconciled common-share classes from SEC Inline XBRL filings."""

import hashlib
import io
import re
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

import polars as pl

from finlab.db.conn import parquet_files
from finlab.ingest.warehouse import publish_table

XBRL = "{http://www.xbrl.org/2003/instance}"
IX = "{http://www.xbrl.org/2013/inlineXBRL}"
DIM = "{http://xbrl.org/2006/xbrldi}"
STOCK_AXIS = "us-gaap:StatementClassOfStockAxis"
FACT = "us-gaap:CommonStockSharesOutstanding"


def _text(element):
    return "".join(element.itertext()).strip()


def extract_share_classes(document: bytes, *, cik: int, period_end: date) -> list[dict]:
    """Accept only exact fiscal-instant class totals that reconcile to the filing."""
    parser = ET.iterparse(io.BytesIO(document), events=("start-ns", "end"))
    namespaces = {}
    for event, item in parser:
        if event == "start-ns":
            prefix, url = item
            namespaces.setdefault(prefix, set()).add(url)
    for prefix, root in (
        ("us-gaap", "http://fasb.org/us-gaap/"),
        ("dei", "http://xbrl.sec.gov/dei/"),
    ):
        bindings = namespaces.get(prefix, set())
        if len(bindings) != 1 or not next(iter(bindings)).startswith(root):
            raise ValueError("Shares require recognized SEC and U.S. GAAP taxonomy definitions")
    tree = parser.root
    contexts = {}
    for context in tree.iter(XBRL + "context"):
        identity = context.find(f".//{XBRL}identifier")
        if (
            identity is None
            or identity.get("scheme") != "http://www.sec.gov/CIK"
            or (identity.text or "").lstrip("0") != str(cik)
        ):
            continue
        members = list(context.iter(DIM + "explicitMember"))
        if context.find(f".//{DIM}typedMember") is not None or len(members) > 1:
            continue
        if members and members[0].get("dimension") != STOCK_AXIS:
            continue
        member = members[0].text.strip() if members else ""
        contexts[context.get("id")] = (member, context.findtext(f".//{XBRL}instant"))
    units = {
        unit.get("id")
        for unit in tree.iter(XBRL + "unit")
        if len(unit) == 1 and unit.findtext(XBRL + "measure") == "xbrli:shares"
    }
    by_class: dict[str, set[int]] = {}
    symbols: dict[str, set[str]] = {}
    listing_titles: dict[str, set[str]] = {}
    identity_verified = False
    for node in tree.iter():
        context = contexts.get(node.get("contextRef"))
        if not context:
            continue
        member, instant = context
        name = node.get("name")
        if node.tag == IX + "nonNumeric":
            if name == "dei:EntityCentralIndexKey" and _text(node).lstrip("0") == str(cik):
                identity_verified = True
            if (
                name == "dei:TradingSymbol"
                and member
                and re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", _text(node))
            ):
                symbols.setdefault(member, set()).add(_text(node))
            if name == "dei:Security12bTitle" and member:
                listing_titles.setdefault(member, set()).add(" ".join(_text(node).split()))
        if (
            node.tag != IX + "nonFraction"
            or name != FACT
            or instant != str(period_end)
            or node.get("unitRef") not in units
        ):
            continue
        text = re.sub(r"[,\s]", "", _text(node))
        scale = int(node.get("scale", "0"))
        if not re.fullmatch(r"\d+(\.\d+)?", text) or not 0 <= scale <= 9 or node.get("sign"):
            raise ValueError("Unsupported inline share-count representation")
        number = Decimal(text).scaleb(scale)
        if number != number.to_integral_value() or number >= 10**16:
            raise ValueError("Invalid integer share count")
        by_class.setdefault(member, set()).add(int(number))
    if not identity_verified:
        raise ValueError("The document does not identify the requested SEC issuer")
    total = by_class.pop("", set())
    if (
        len(total) != 1
        or not by_class
        or any(len(values) != 1 for values in by_class.values())
        or sum(next(iter(values)) for values in by_class.values()) != next(iter(total))
        or next(iter(total)) <= 0
    ):
        raise ValueError("Fiscal-date common share classes do not reconcile to one reported total")
    rows = []
    for name, shares in sorted(by_class.items()):
        tickers = set(symbols.get(name, set()))
        binding = "matching_dimension" if tickers else None
        standard = re.fullmatch(r"us-gaap:CommonClass([A-Z])Member", name)
        if standard:
            pattern = re.compile(rf"Class {standard[1]} (?:Common|Capital) Stock(?:,.*)?$")
            matched_titles = {
                ticker
                for member, titles in listing_titles.items()
                if len(titles) == 1 and pattern.fullmatch(next(iter(titles)))
                for ticker in symbols.get(member, ())
            }
            if matched_titles and not tickers:
                binding = "matching_reported_common_class_title"
            tickers.update(matched_titles)
        if len(tickers) > 1:
            raise ValueError("A reported common class has conflicting listed symbols")
        rows.append(
            {
                "class_member": name,
                "shares": next(iter(shares)),
                "listed_symbol": next(iter(tickers), None),
                "listing_binding": binding,
                "issuer_common_shares": next(iter(total)),
            }
        )
    return rows


def share_class_filing(con, accession: str):
    filing = con.execute(
        """SELECT cik, period_of_report, filed_date, primary_doc_url
           FROM filing WHERE accession_number=? AND form IN ('10-K','10-Q','10-K/A','10-Q/A')""",
        [accession],
    ).fetchone()
    if not filing or not all(filing):
        raise ValueError("The SEC accession needs a dated filing record and official URL")
    cik, period_end, filed_date, source_url = filing
    expected = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"
    if not source_url.startswith(expected) or period_end > filed_date:
        raise ValueError("The accession and filing provenance do not agree")
    return filing


def stage_share_classes(con, settings, accession: str, document: bytes) -> list[dict]:
    """Stage provider bytes retrieved for this exact accession by the Data Engine."""
    cik, period_end, filed_date, source_url = share_class_filing(con, accession)
    rows = extract_share_classes(document, cik=cik, period_end=period_end)
    digest = hashlib.sha256(document).hexdigest()
    archive = settings.raw_dir / "filing_documents"
    archive.mkdir(parents=True, exist_ok=True)
    raw = archive / f"{accession}-{digest}.htm"
    if not raw.exists():
        temporary = raw.with_suffix(".tmp")
        temporary.write_bytes(document)
        temporary.replace(raw)
    evidence = [
        {
            **row,
            "cik": cik,
            "period_end": period_end,
            "filed_date": filed_date,
            "accession_number": accession,
            "source_url": source_url,
            "source_sha256": digest,
        }
        for row in rows
    ]
    root = settings.warehouse_dir / "share_class_fact"
    frame = pl.DataFrame(evidence, schema_overrides={"listed_symbol": pl.String})
    con.register("_incoming_share_classes", frame)
    try:
        previous = parquet_files(root)
        con.execute("DROP TABLE IF EXISTS _working_share_classes")
        if previous:
            con.execute(
                """CREATE TEMP TABLE _working_share_classes AS
                   SELECT * FROM read_parquet(?) WHERE accession_number<>?
                   UNION ALL BY NAME SELECT * FROM _incoming_share_classes""",
                [[str(path) for path in previous], accession],
            )
        else:
            con.execute(
                "CREATE TEMP TABLE _working_share_classes AS SELECT * FROM _incoming_share_classes"
            )
        publish_table(
            con,
            source="_working_share_classes",
            root=root,
            order_by=("cik", "period_end", "accession_number", "class_member"),
        )
    finally:
        con.unregister("_incoming_share_classes")
    return evidence
