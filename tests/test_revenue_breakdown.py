"""One filing can contain non-additive dimensions and multiple fiscal windows."""

from datetime import date
from xml.sax.saxutils import escape

import pytest

from finlab.ingest.revenue_breakdown import (
    CONTRACT,
    NONCONTRACT,
    PRODUCT,
    SEGMENT,
    extract_revenue_breakdowns,
)

END = date(2025, 9, 30)

CONTEXTS = [
    ("all", (), "2025-07-01"),
    ("services", ((SEGMENT, "test:Services"),), "2025-07-01"),
    ("north", ((SEGMENT, "test:Services"), (PRODUCT, "test:North")), "2025-07-01"),
    ("south", ((SEGMENT, "test:Services"), (PRODUCT, "test:South")), "2025-07-01"),
    ("regional", ((SEGMENT, "test:Services"), (PRODUCT, "test:Regional")), "2025-07-01"),
    ("hosting", ((SEGMENT, "test:Hosting"),), "2025-07-01"),
    ("geography", (("srt:StatementGeographicalAxis", "test:Foreign"),), "2025-07-01"),
    ("cumulative", (), "2025-01-01"),
]
REVENUE = "us-gaap:Revenues"


def filing(*tables, cik="0000000007"):
    contexts = "".join(
        f'''
      <xbrli:context id="{name}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">{cik}</xbrli:identifier>
      <xbrli:segment>{"".join(f'<d:explicitMember dimension="{axis}">{member}</d:explicitMember>' for axis, member in dims)}</xbrli:segment>
      </xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{END}</xbrli:endDate></xbrli:period>
      </xbrli:context>'''
        for name, dims, start in CONTEXTS
    )

    def fact(row):
        label, context, name, amount = row
        sign = "sign='-'" if amount < 0 else ""
        return f'''<tr><td>{escape(label)}</td><td><ix:nonFraction name="{name}" contextRef="{context}"
                   unitRef="usd" scale="6" {sign}>{abs(amount)}</ix:nonFraction></td></tr>'''

    presentations = "".join(
        "<table>" + "".join(fact(row) for row in rows) + "</table>" for rows in tables
    )
    return f"""<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:d="http://xbrl.org/2006/xbrldi"
      xmlns:us-gaap="http://fasb.org/us-gaap/2025" xmlns:srt="http://fasb.org/srt/2025"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:test="https://example.com/2025">
      <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      {contexts}{presentations}</html>""".encode()


DETAIL = [
    ("North", "north", CONTRACT, 38),
    ("South", "south", CONTRACT, 22),
    ("Regional subtotal", "regional", CONTRACT, 60),
    ("Services total", "services", CONTRACT, 60),
    ("Managed hosting", "hosting", CONTRACT, 35),
    ("Licensing adjustment", "all", NONCONTRACT, 5),
    ("Total revenue", "all", REVENUE, 100),
]
COARSE = DETAIL[3:]
GEO = [("Region", "geography", CONTRACT, 95), *DETAIL[-2:]]


def test_revenue_extracts_unique_reconciled_filing_presentation_without_subtotals():
    observations = extract_revenue_breakdowns(filing(DETAIL, COARSE, GEO), 7, END)
    rows = observations["2025-07-01"]
    assert [row["label"] for row in rows] == [
        "North",
        "South",
        "Managed hosting",
        "Licensing adjustment",
    ]
    assert sum(row["amount"] for row in rows) == 100_000_000
    assert rows[0]["product"] == "test:North"
    assert rows[-1]["concept"] == NONCONTRACT


@pytest.mark.parametrize(
    "change",
    [
        (1, ("South", "south", CONTRACT, 21)),  # leaf and subtotal disagree
        (4, ("Managed hosting", "hosting", CONTRACT, 34)),  # totals do not reconcile
        (5, ("Pending adjustment", "all", NONCONTRACT, -5)),  # signed, unsupported
        (
            5,
            ("Missing classification", "cumulative", NONCONTRACT, 5),
        ),  # incompatible fiscal duration
    ],
)
def test_revenue_mismatches_signed_adjustments_and_wrong_period_fail_closed(change):
    index, replacement = change
    rows = DETAIL.copy()
    rows[index] = replacement
    assert extract_revenue_breakdowns(filing(rows), 7, END) == {}


def test_foreign_issuer_and_competing_breakdowns_fail_closed():
    assert extract_revenue_breakdowns(filing(DETAIL, cik="0000000008"), 7, END) == {}
    competing = DETAIL.copy()
    competing[0] = ("North", "north", CONTRACT, 39)
    competing[1] = ("South", "south", CONTRACT, 21)
    assert extract_revenue_breakdowns(filing(DETAIL, competing), 7, END) == {}
