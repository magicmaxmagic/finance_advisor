from datetime import date

import duckdb
import pytest

from finlab.analytics.company import _class_market_value, _reported_common_shares
from finlab.analytics.snapshot import published_read_conn
from finlab.db.conn import write_conn
from finlab.db.migrations import init_database
from finlab.engine.snapshot import REQUIRED_DATASETS, publish_snapshot
from finlab.ingest.share_classes import extract_share_classes, stage_share_classes

DAY = date(2020, 6, 30)
ACCESSION = "0000000001-20-000001"


def filing_document(symbol_b=True):
    def context(key, member):
        dim = (
            f'<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">{member}</xbrldi:explicitMember></xbrli:segment>'
            if member
            else ""
        )
        return f'<xbrli:context id="{key}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier>{dim}</xbrli:entity><xbrli:period><xbrli:instant>2020-06-30</xbrli:instant></xbrli:period></xbrli:context>'

    symbol = (
        '<ix:nonNumeric contextRef="b" name="dei:TradingSymbol">BB</ix:nonNumeric>'
        if symbol_b
        else ""
    )
    values = "".join(
        f'<ix:nonFraction contextRef="{key}" name="us-gaap:CommonStockSharesOutstanding" unitRef="shares" scale="3">{shares}</ix:nonFraction>'
        for key, shares in (("total", 10), ("a", 7), ("b", 3))
    )
    return f"""
    <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:us-gaap="http://fasb.org/us-gaap/2020" xmlns:dei="http://xbrl.sec.gov/dei/2020"
      xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
      {context("total", "")}{context("a", "us-gaap:CommonClassAMember")}{context("b", "us-gaap:CommonClassBMember")}
      <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
      <ix:nonNumeric contextRef="total" name="dei:EntityCentralIndexKey">0000000001</ix:nonNumeric>
      <ix:nonNumeric contextRef="a" name="dei:TradingSymbol">AA</ix:nonNumeric>{symbol}{values}</html>
    """.encode()


def test_reconciles_dated_share_classes_and_rejects_different_issuer_or_bad_total():
    raw = filing_document()
    rows = extract_share_classes(raw, cik=1, period_end=DAY)
    assert [(row["listed_symbol"], row["shares"]) for row in rows] == [("AA", 7000), ("BB", 3000)]
    for altered, cik, period in (
        (raw, 2, DAY),
        (raw, 1, date(2020, 7, 1)),
        (raw.replace(b">10<", b">11<"), 1, DAY),
    ):
        with pytest.raises(ValueError):
            extract_share_classes(altered, cik=cik, period_end=period)


def test_class_value_has_no_unquoted_substitution_and_respects_filing_cutoff(settings):
    with write_conn(settings) as con:
        init_database(con)
        con.execute("INSERT INTO entity(cik,name) VALUES (1, 'Issuer')")
        con.execute(
            """INSERT INTO filing(accession_number,cik,form,filed_date,period_of_report,primary_doc_url)
            VALUES (?,1,'10-Q',DATE '2020-07-20',DATE '2020-06-30',?)""",
            [ACCESSION, "https://www.sec.gov/Archives/edgar/data/1/000000000120000001/q.htm"],
        )
        stage_share_classes(con, settings, ACCESSION, filing_document())
        stage_share_classes(con, settings, ACCESSION, filing_document())
    for relation in REQUIRED_DATASETS:
        (settings.warehouse_dir / relation).mkdir(exist_ok=True)
    snapshot = publish_snapshot(warehouse_dir=settings.warehouse_dir, schema_version=5)
    with published_read_conn(settings, epoch=snapshot.epoch) as con:
        assert con.execute("SELECT count(*) FROM share_class_fact").fetchone()[0] == 2
        con.execute(
            "CREATE TABLE security AS SELECT 'A' AS security_id, 1 AS cik, 'AA' AS ticker, 'USD' AS currency UNION ALL SELECT 'B',1,'BB','USD'"
        )
        con.execute("""CREATE TABLE price_daily AS SELECT 'A' AS security_id, DATE '2020-07-22' AS date, 20.0 AS close,
                  'yahoo_auto_adjust_false' AS adjustment_basis,'yahoo' AS source
                  UNION ALL SELECT 'B', DATE '2020-07-22',30,'yahoo_auto_adjust_false','yahoo'""")
        con.execute("""CREATE TABLE price_ingestion AS SELECT security_id,DATE '2020-06-01' AS actions_checked_from,
                    DATE '2020-07-22' AS actions_checked_through, DATE '2020-07-22' AS price_basis_date,'yahoo' AS source
                    FROM security""")
        con.execute("""CREATE TABLE corporate_action AS SELECT 'A' AS security_id, 'split' AS action_type,
                      DATE '2020-07-21' AS ex_date, 2.0 AS factor""")
        end = date(2020, 7, 22)
        assert _class_market_value(con, 1, date(2020, 7, 19), end, end) == (None, None, None)
        market_cap, _, evidence = _class_market_value(con, 1, end, end, end)
        assert market_cap == 7000 * 2 * 20 + 3000 * 30
        assert len(evidence["share_class_evidence"]) == 2
        con.execute("DELETE FROM price_daily WHERE security_id='B'")
        market_cap, _, evidence = _class_market_value(con, 1, end, end, end)
        assert market_cap is None
        assert evidence["share_class_evidence"][1]["reason"] == "CLASS_PRICE_UNAVAILABLE"


def test_common_share_fallback_requires_matching_public_fiscal_vintage():
    with duckdb.connect() as con:
        con.execute("""CREATE MACRO common_shares_pit(d) AS TABLE
            SELECT NULL::DOUBLE shares, NULL::DATE period_end, NULL::DATE filed_date,
                   NULL::VARCHAR accession_number, NULL::INTEGER cik WHERE FALSE""")
        con.execute("""CREATE TABLE fact(cik INTEGER, accession_number VARCHAR,taxonomy VARCHAR,
                    tag VARCHAR,unit VARCHAR,period_start DATE,period_end DATE,filed_date DATE,value DOUBLE)""")
        con.execute(
            """CREATE TABLE filing(cik INTEGER,accession_number VARCHAR,period_of_report DATE,filed_date DATE,form VARCHAR)"""
        )
        con.execute("""INSERT INTO fact VALUES (1,'new','us-gaap','CommonStockSharesOutstanding','shares',NULL,
            DATE '2020-06-30',DATE '2020-07-20', 10000)""")
        con.execute("INSERT INTO filing VALUES(1,'new',DATE '2020-06-30',DATE '2020-07-20','10-Q')")
        end = date(2020, 7, 22)
        assert _reported_common_shares(con, 1, date(2020, 7, 19), end, {"filing"}) is None
        assert _reported_common_shares(con, 1, end, end, {"filing"})[0] == 10000
        con.execute("UPDATE filing SET period_of_report = DATE '2020-06-29'")
        assert _reported_common_shares(con, 1, end, end, {"filing"}) is None
