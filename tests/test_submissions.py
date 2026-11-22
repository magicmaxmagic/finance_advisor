from datetime import UTC, datetime

import pytest

from finlab.db.conn import write_conn
from finlab.ingest.submissions import parse_submissions, verify_acceptance_sgml


def test_json_acceptance_is_preserved_without_claiming_a_verified_clock():
    raw = "2020-07-01T16:30:00.000Z"
    row = next(
        parse_submissions(
            {
                "cik": 1,
                "filings": {"recent": {"accessionNumber": ["one"], "acceptanceDateTime": [raw]}},
            }
        )
    )
    assert row["accepted_at_raw"] == raw
    assert row["accepted_at_status"] == "unverified_sec_json"


def test_verified_sgml_uses_new_york_daylight_time_and_checks_identity(initialised):
    with write_conn(initialised) as con:
        con.execute(
            "INSERT INTO filing(accession_number,form,filed_date) VALUES ('0000000001-20-000001','10-K',DATE '2020-07-01')"
        )
        text = "<ACCESSION-NUMBER>0000000001-20-000001\n<ACCEPTANCE-DATETIME>20200701163000"
        result = verify_acceptance_sgml(con, "0000000001-20-000001", text)
        assert result == datetime(2020, 7, 1, 20, 30, tzinfo=UTC)
        assert con.execute("SELECT accepted_at_status FROM filing").fetchone() == ("verified_sgml",)
        with pytest.raises(ValueError, match="identity"):
            verify_acceptance_sgml(con, "wrong", text)
