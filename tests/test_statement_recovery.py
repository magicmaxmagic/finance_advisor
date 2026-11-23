"""Offline primary SEC recovery and staging contract."""

import gzip
import json
from datetime import date, timedelta

import pytest

from finlab.ingest.companyfacts import parse_all, raw_path
from finlab.ingest.statement_recovery import (
    StatementRecoveryError,
    extract_statement,
    filing_url,
    load_recovery,
    recovery_path,
    store_recovery,
)
from finlab.ingest.universe import Company

FILING = dict(
    cik=1,
    accession="0000000001-26-000001",
    form="10-Q",
    filed_date="2026-11-09",
    report_period="2026-09-30",
    primary_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/a.htm",
)
AXES = """
<xbrli:segment xmlns:d="http://xbrl.org/2006/xbrldi">
<d:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">a:Consumer</d:explicitMember>
</xbrli:segment>
"""


def filing_document(extra="", *, doc_type="10-Q", amendment="false", end=FILING["report_period"]):
    start = (date.fromisoformat(end) - timedelta(days=91)).isoformat()
    contexts = "".join(
        f'<xbrli:context id="{key}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier>'
        f"{AXES if key == 'segment' else ''}</xbrli:entity><xbrli:period>{period}</xbrli:period></xbrli:context>"
        for key, period in (
            (
                "quarter",
                f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>",
            ),
            ("end", f"<xbrli:instant>{end}</xbrli:instant>"),
            ("segment", f"<xbrli:instant>{end}</xbrli:instant>"),
        )
    )
    headers = {
        "EntityCentralIndexKey": "0000000001",
        "DocumentType": doc_type,
        "DocumentPeriodEndDate": end,
        "DocumentFiscalYearFocus": "2026",
        "DocumentFiscalPeriodFocus": "Q3",
        "AmendmentFlag": amendment,
    }
    meta = "".join(
        f'<ix:nonNumeric name="dei:{key}" contextRef="quarter">{value}</ix:nonNumeric>'
        for key, value in headers.items()
    )
    return f"""
    <html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
    xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:dei="http://xbrl.sec.gov/dei/2026"
    xmlns:us-gaap="http://fasb.org/us-gaap/2026" xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
      {contexts}
      <xbrli:unit id="u"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      {meta}
      <ix:nonFraction name="us-gaap:Assets" contextRef="end" unitRef="u" decimals="-3" scale="3">1,401</ix:nonFraction>
      <ix:nonFraction name="us-gaap:Assets" contextRef="segment" unitRef="u" decimals="-3" scale="3">200</ix:nonFraction>
      <ix:nonFraction name="us-gaap:NetIncomeLoss" contextRef="quarter" unitRef="u" decimals="0" sign="-">50</ix:nonFraction>
      <ix:nonFraction name="us-gaap:NetCashProvidedByUsedInOperatingActivities" contextRef="quarter" unitRef="u" decimals="0">42</ix:nonFraction>
      {extra}
    </html>""".encode()


def test_primary_document_preserves_sign_precision_and_source_cutoff(settings):
    document = filing_document("""<ix:nonFraction name="us-gaap:Assets" contextRef="end"
                              unitRef="u" decimals="-6" scale="6">1</ix:nonFraction>""")
    provenance = store_recovery(settings, FILING, document)
    accession = FILING["accession"]
    filing, source, report = load_recovery(recovery_path(settings, FILING["cik"], accession))
    assert filing == FILING and source == provenance
    assert provenance["observation_count"] == 3
    values = report["facts"]["us-gaap"]
    assert values["Assets"]["units"]["USD"][0]["val"] == 1_401_000
    assert values["NetIncomeLoss"]["units"]["USD"][0]["val"] == -50
    assert values["Assets"]["units"]["USD"][0]["filed"] == FILING["filed_date"]


def test_amendment_and_inconsistent_anchors_fail_closed():
    with pytest.raises(StatementRecoveryError, match="IDENTITY_MISMATCH"):
        extract_statement(filing_document(amendment="true"), FILING)
    amended = {**FILING, "form": "10-Q/A"}
    assert extract_statement(filing_document(amendment="true"), amended)["cik"] == FILING["cik"]
    with pytest.raises(StatementRecoveryError, match="STATEMENTS_INCOMPLETE"):
        extract_statement(
            filing_document("""<ix:nonFraction name="us-gaap:Assets" contextRef="end"
                           unitRef="u" decimals="-3" scale="3">2,001</ix:nonFraction>"""),
            FILING,
        )
    with pytest.raises(StatementRecoveryError, match="IDENTITY_MISMATCH"):
        extract_statement(filing_document(end="2026-06-30"), FILING)
    assert filing_url(1, FILING["accession"], "../outside.htm") is None


def test_primary_cache_can_be_reprocessed_and_rejects_wrong_hash(settings):
    target = raw_path(settings, 1)
    target.parent.mkdir(parents=True)
    with gzip.open(target, "wt") as handle:
        json.dump({"cik": 1, "facts": {}}, handle)
    store_recovery(settings, FILING, filing_document())
    rows, _, _ = parse_all(settings, [Company(1, "AAA", "Technology", "Example")])
    assert len(list(rows)) == 3
    (raw,) = list(recovery_path(settings, 1, FILING["accession"]).parent.glob("*.htm.gz"))
    raw.write_bytes(gzip.compress(b"changed document"))
    rows, _, _ = parse_all(settings, [Company(1, "AAA", "Technology", "Example")])
    with pytest.raises(StatementRecoveryError, match="CACHE_INVALID"):
        list(rows)


def test_exact_primary_document_recovers_a_company_facts_gap(settings, monkeypatch):
    import asyncio

    from finlab.engine import financial_sync

    requested = []

    class LocalSec:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def get_bytes(self, url):
            requested.append(url)
            return filing_document()

    monkeypatch.setattr(financial_sync, "SecClient", LocalSec)
    filing = {**FILING, "inline_xbrl": True}
    assert (
        financial_sync._check_statement(settings, filing, ({}, None))[0]
        == "ACCESSION_MISSING_IN_COMPANY_FACTS"
    )
    output = asyncio.run(financial_sync._recover_primary(settings, [filing]))
    assert output[filing["accession"]][0] is None
    assert requested == [filing["primary_url"]]
    error, source = financial_sync._check_statement(settings, filing, ({}, None))
    assert error is None and source["observation_count"] == 3


def test_primary_recovery_honors_retry_after_and_stops_the_batch(settings, monkeypatch):
    import asyncio

    import httpx

    from finlab.engine import financial_sync

    calls = []

    class LocalSec:
        def __init__(self, settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def get_bytes(self, url):
            calls.append(url)
            response = httpx.Response(
                429, headers={"Retry-After": "7200"}, request=httpx.Request("GET", url)
            )
            response.raise_for_status()

    monkeypatch.setattr(financial_sync, "SecClient", LocalSec)
    report = {"issuer_errors": {}, "pending": [FILING]}
    checks = asyncio.run(financial_sync._recover_primary(settings, [FILING, FILING], report))
    assert checks[FILING["accession"]][0] == "PRIMARY_FILING_RATE_LIMITED"
    assert len(calls) == 1
    assert financial_sync.SecSyncIncomplete(report).retry_after == 7200
