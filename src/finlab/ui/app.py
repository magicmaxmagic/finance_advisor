"""Streamlit entrypoint, reading the Parquet warehouse.

**This app never opens the read-write DuckDB connection.** It opens an
in-memory database over `data/warehouse/*.parquet` and installs the
point-in-time macros on it, so it keeps working while `finlabd` holds the write
lock. `tests/test_read_path.py` proves that by holding the lock in one process
and querying from another.

Every figure on every page is computed through `fundamentals_asof(as_of)` at
the date chosen by the sidebar slider. Moving the slider back does not filter a
precomputed table — it re-resolves the fundamentals from the filings that
existed on that date, which is why the restatement page can show the original
number and then the revision.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import altair as alt
import duckdb
import polars as pl
import streamlit as st

from finlab.config import get_settings
from finlab.ui import queries as q

st.set_page_config(page_title="finlab", page_icon="📊", layout="wide")

PAGES = ("Screen", "Company", "Restatement explorer")

HISTORY_CONCEPTS = ("revenue", "gross_profit", "operating_income", "net_income")

PERCENT_METRICS = {
    "gross_margin",
    "operating_margin",
    "net_margin",
    "fcf_margin",
    "roe",
    "roa",
    "roic",
}


@contextmanager
def _reader() -> Iterator[duckdb.DuckDBPyConnection]:
    """A reader owned by the calling thread, for the duration of one query.

    Deliberately NOT `st.cache_resource`. Streamlit runs every rerun on a fresh
    thread, and DuckDB connections are not thread-safe: a cached connection
    shared across script threads segfaults inside Arrow conversion. Opening one
    costs ~30 ms and only happens on a cache miss, so the churn is invisible
    while the correctness problem disappears.
    """
    con = q.open_reader(get_settings())
    try:
        yield con
    finally:
        con.close()


# --- cached data access -----------------------------------------------------
# Every cache key includes as_of, so the slider genuinely re-resolves the
# point-in-time view rather than reusing a stale frame.


@st.cache_data(show_spinner=False)
def _range() -> tuple[date, date]:
    with _reader() as con:
        return q.data_range(con)


@st.cache_data(show_spinner=False)
def _has_prices() -> bool:
    with _reader() as con:
        return q.has_prices(con)


@st.cache_data(show_spinner=False)
def _companies() -> pl.DataFrame:
    with _reader() as con:
        return q.companies(con)


@st.cache_data(show_spinner=False)
def _screen(as_of: date) -> pl.DataFrame:
    with _reader() as con:
        return q.screen(con, as_of)


@st.cache_data(show_spinner=False)
def _history(cik: int, as_of: date) -> pl.DataFrame:
    with _reader() as con:
        return q.company_history(con, cik, as_of, HISTORY_CONCEPTS)


@st.cache_data(show_spinner=False)
def _percentiles(cik: int, as_of: date) -> pl.DataFrame:
    with _reader() as con:
        return q.company_metrics_with_percentile(con, cik, as_of)


@st.cache_data(show_spinner=False)
def _candidates(cik: int) -> pl.DataFrame:
    with _reader() as con:
        return q.restatement_candidates(con, cik)


@st.cache_data(show_spinner=False)
def _revisions(
    cik: int, canonical: str, period_end: date, as_of: date, duration_bucket: str
) -> pl.DataFrame:
    with _reader() as con:
        return q.restatement_history(con, cik, canonical, period_end, as_of, duration_bucket)


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    for scale, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(value) >= scale:
            return f"{value / scale:,.2f}{suffix}"
    return f"{value:,.0f}"


# --- sidebar ----------------------------------------------------------------

first, last = _range()
st.sidebar.title("finlab")
page = st.sidebar.radio("Page", PAGES)

st.sidebar.divider()
as_of = st.sidebar.slider(
    "As-of date",
    min_value=first,
    max_value=max(last, date.today()),
    value=date.today() if date.today() >= first else last,
    format="YYYY-MM-DD",
    help=(
        "Every figure is resolved as it was known on this date. No filing with a "
        "later filed_date can influence anything shown."
    ),
)
st.sidebar.caption(f"Filings available: {first} → {last}")
if not _has_prices():
    st.sidebar.warning(
        "No price series ingested — Stooq is refusing automated access, so "
        "market cap is unavailable. Fundamentals are unaffected.",
        icon="⚠️",
    )

companies = _companies()

# --- Screen -----------------------------------------------------------------

if page == "Screen":
    st.header("Screen")
    st.caption(f"All {companies.height} companies as of {as_of}, one row each.")

    frame = _screen(as_of)
    chosen = st.multiselect("Sector", sorted(set(companies["sector"].to_list())))
    if chosen:
        frame = frame.filter(pl.col("sector").is_in(chosen))

    # to_pandas() explicitly: Streamlit's own polars conversion path segfaults
    # inside pyarrow on these frames, while polars' own Arrow-backed conversion
    # is fine.
    st.dataframe(
        frame.to_pandas(),
        hide_index=True,
        column_config={
            "market_cap": st.column_config.NumberColumn("market cap", format="compact"),
            "revenue": st.column_config.NumberColumn("revenue (FY)", format="compact"),
            "total_assets": st.column_config.NumberColumn("assets", format="compact"),
            **{
                name: st.column_config.NumberColumn(name.replace("_", " "), format="percent")
                for name in q.METRIC_NAMES
                if name in PERCENT_METRICS
            },
            "net_debt_to_equity": st.column_config.NumberColumn("net debt / equity", format="%.2f"),
        },
    )
    st.download_button(
        "Download CSV",
        frame.write_csv(),
        file_name=f"finlab-screen-{as_of}.csv",
        mime="text/csv",
    )

# --- Company ----------------------------------------------------------------

elif page == "Company":
    st.header("Company")
    ticker = st.selectbox("Ticker", companies["ticker"].to_list())
    row = companies.filter(pl.col("ticker") == ticker).row(0, named=True)
    cik = int(row["cik"])
    st.subheader(f"{row['name']} · {row['sector']} · CIK {cik}")

    history = _history(cik, as_of)
    if history.is_empty():
        st.info(f"No annual figures were on file for {ticker} as of {as_of}.")
    else:
        revenue = history.filter(pl.col("canonical") == "revenue")
        present = set(history["canonical"].unique().to_list())
        wide = history.pivot(on="canonical", index="period_end", values="value")
        margin_columns = [
            (pl.col(c) / pl.col("revenue")).alias(f"{c} margin")
            for c in ("gross_profit", "operating_income", "net_income")
            if c in present and "revenue" in present
        ]

        left, right = st.columns(2)
        with left:
            st.caption("Revenue (annual)")
            st.altair_chart(
                alt.Chart(revenue.select("period_end", "value").to_pandas())
                .mark_line(point=True)
                .encode(
                    x=alt.X("period_end:T", title=None),
                    y=alt.Y("value:Q", title="revenue", axis=alt.Axis(format="~s")),
                    tooltip=["period_end:T", alt.Tooltip("value:Q", format=",.0f")],
                )
                .properties(height=280),
                use_container_width=True,
            )
        with right:
            st.caption("Margins (annual)")
            if margin_columns:
                long = (
                    wide.with_columns(margin_columns)
                    .select(["period_end", *[c.meta.output_name() for c in margin_columns]])
                    .unpivot(index="period_end", variable_name="margin", value_name="value")
                    .drop_nulls()
                )
                st.altair_chart(
                    alt.Chart(long.to_pandas())
                    .mark_line(point=True)
                    .encode(
                        x=alt.X("period_end:T", title=None),
                        y=alt.Y("value:Q", title=None, axis=alt.Axis(format="%")),
                        color=alt.Color("margin:N", title=None),
                        tooltip=[
                            "period_end:T",
                            "margin:N",
                            alt.Tooltip("value:Q", format=".1%"),
                        ],
                    )
                    .properties(height=280),
                    use_container_width=True,
                )
            else:
                st.info("No margin inputs resolve for this company.")

    st.subheader("Metrics vs sector")
    percentiles = _percentiles(cik, as_of)
    if percentiles.is_empty():
        st.info("No metrics resolve for this company at the selected date.")
    else:
        st.dataframe(
            percentiles.to_pandas(),
            hide_index=True,
            column_config={
                "metric_name": "metric",
                "value": st.column_config.NumberColumn("value", format="%.4f"),
                "sector_percentile": st.column_config.ProgressColumn(
                    "sector percentile", min_value=0, max_value=1, format="%.0f%%"
                ),
                "peers": "peers",
            },
        )

    st.subheader("Provenance")
    st.caption(
        "Which filing each figure came from. A number with no filing behind it is "
        "not a number this system will show."
    )
    if not history.is_empty():
        st.dataframe(
            history.select(
                "canonical",
                "period_end",
                "value",
                "form",
                "filed_date",
                "accession_number",
                "derived",
            )
            .sort(["period_end", "canonical"], descending=[True, False])
            .to_pandas(),
            hide_index=True,
            column_config={
                "value": st.column_config.NumberColumn("value", format="compact"),
                "accession_number": "accession",
                "derived": st.column_config.CheckboxColumn(
                    "derived",
                    help="Computed from an accounting identity, not tagged directly.",
                ),
            },
        )

# --- Restatement explorer ---------------------------------------------------

else:
    st.header("Restatement explorer")
    st.caption(
        "Every value ever reported for one figure, one row per filing. This is "
        "the page that proves the point-in-time store works: the older value is "
        "still here, not overwritten."
    )

    ticker = st.selectbox("Ticker", companies["ticker"].to_list())
    row = companies.filter(pl.col("ticker") == ticker).row(0, named=True)
    cik = int(row["cik"])

    candidates = _candidates(cik)
    if candidates.is_empty():
        st.info(f"No figure has been reported more than once for {ticker}.")
        st.stop()

    annual = candidates.filter(pl.col("duration_bucket") == "annual")
    pool = annual if not annual.is_empty() else candidates
    bucket = "annual" if not annual.is_empty() else pool["duration_bucket"].to_list()[0]

    concept = st.selectbox("Concept", sorted(set(pool["canonical"].to_list())))
    periods = (
        pool.filter(pl.col("canonical") == concept)
        .sort("period_end", descending=True)["period_end"]
        .to_list()
    )
    period_end = st.selectbox("Fiscal period ending", periods)

    revisions = _revisions(cik, concept, period_end, as_of, bucket)
    if revisions.is_empty():
        st.warning(
            f"Nothing had been filed for {concept} / {period_end} as of {as_of}. "
            "Move the as-of date forward."
        )
        st.stop()

    revisions = revisions.with_columns(
        [
            (pl.col("value") - pl.col("value").shift(1)).alias("delta"),
            ((pl.col("value") / pl.col("value").shift(1)) - 1).alias("delta_pct"),
        ]
    )

    st.dataframe(
        revisions.to_pandas(),
        hide_index=True,
        column_config={
            "filed_date": "filed",
            "accession_number": "accession",
            "value": st.column_config.NumberColumn("reported value", format="compact"),
            "delta": st.column_config.NumberColumn("Δ vs previous", format="compact"),
            "delta_pct": st.column_config.NumberColumn("Δ %", format="percent"),
        },
    )

    distinct = revisions["value"].n_unique()
    st.metric(
        "Distinct values reported",
        distinct,
        help="More than one means this figure was restated.",
    )
    if distinct > 1:
        values = revisions["value"].to_list()
        filed = revisions["filed_date"].to_list()
        st.success(
            f"As of {as_of}, {ticker}'s {concept} for the period ending {period_end} "
            f"was first reported as {_fmt_money(values[0])} on {filed[0]} and most "
            f"recently as {_fmt_money(values[-1])} on {filed[-1]}."
        )

    st.altair_chart(
        alt.Chart(revisions.select("filed_date", "value").to_pandas())
        .mark_line(point=True)
        .encode(
            x=alt.X("filed_date:T", title="filed"),
            y=alt.Y(
                "value:Q",
                title="reported value",
                scale=alt.Scale(zero=False),
                axis=alt.Axis(format="~s"),
            ),
            tooltip=["filed_date:T", alt.Tooltip("value:Q", format=",.0f")],
        )
        .properties(height=260),
        use_container_width=True,
    )
