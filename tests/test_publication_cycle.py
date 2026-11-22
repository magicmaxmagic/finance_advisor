from datetime import date

from finlab.db.conn import write_conn
from finlab.ingest.prices import build_securities
from finlab.ingest.universe import Company


def test_security_metadata_upserts_preserve_currency_and_first_observation(initialised):
    company = Company(1, "AAA", "Technology", "Issuer")
    with write_conn(initialised) as con:
        build_securities(con, initialised, (company,))
        con.execute(
            "UPDATE security SET currency='USD',exchange='NASDAQ',first_date=DATE '2006-01-03' WHERE security_id=?",
            [company.security_id],
        )
        build_securities(con, initialised, (company,))
        assert con.execute(
            "SELECT exchange,first_date FROM security WHERE security_id=?", [company.security_id]
        ).fetchone() == ("NASDAQ", date(2006, 1, 3))
