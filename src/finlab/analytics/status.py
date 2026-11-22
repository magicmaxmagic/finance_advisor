"""Latest-published research dates are independent of wall-clock quote promises."""

from datetime import date

from finlab.analytics.snapshot import published_read_conn


def latest_research_date() -> date:
    with published_read_conn() as con:
        if "price_daily" not in {row[0] for row in con.execute("SHOW TABLES").fetchall()}:
            return date.today()
        row = con.execute(
            "SELECT max(date) FROM price_daily WHERE date<=?", [date.today()]
        ).fetchone()
        return row[0] or date.today()
