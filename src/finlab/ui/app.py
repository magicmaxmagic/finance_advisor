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
import pandas as pd
import streamlit as st

from finlab.config import get_settings
from finlab.ui import queries as q

st.set_page_config(page_title="finlab", page_icon="📊", layout="wide")

PAGES = ("Marché en direct", "Weekly brief", "Screen", "Company", "Restatement explorer")

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
def _companies() -> pd.DataFrame:
    with _reader() as con:
        return q.companies(con)


@st.cache_data(show_spinner=False)
def _screen(as_of: date) -> pd.DataFrame:
    with _reader() as con:
        return q.screen(con, as_of)


@st.cache_data(show_spinner=False)
def _history(cik: int, as_of: date) -> pd.DataFrame:
    with _reader() as con:
        return q.company_history(con, cik, as_of, HISTORY_CONCEPTS)


@st.cache_data(show_spinner=False)
def _percentiles(cik: int, as_of: date) -> pd.DataFrame:
    with _reader() as con:
        return q.company_metrics_with_percentile(con, cik, as_of)


@st.cache_data(show_spinner=False)
def _candidates(cik: int) -> pd.DataFrame:
    with _reader() as con:
        return q.restatement_candidates(con, cik)


@st.cache_data(show_spinner=False)
def _revisions(
    cik: int, canonical: str, period_end: date, as_of: date, duration_bucket: str
) -> pd.DataFrame:
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

st.sidebar.title("finlab")
has_facts = (get_settings().warehouse_dir / "fact").is_dir()
page = st.sidebar.radio("Page", PAGES)
if page == "Marché en direct":
    from finlab.ui.live import render

    render()
    st.stop()
if page == "Weekly brief":
    from finlab.ui.weekly import render

    render(get_settings().reports_dir / "weekly")
    st.stop()
if not has_facts:
    st.info("Run make bootstrap to load filing history for this page.")
    st.stop()
first, last = _range()

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
    st.caption(f"All {len(companies)} companies as of {as_of}, one row each.")

    frame = _screen(as_of)
    chosen = st.multiselect("Sector", sorted(companies["sector"].unique()))
    if chosen:
        frame = frame[frame["sector"].isin(chosen)]

    st.dataframe(
        frame,
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
        frame.to_csv(index=False),
        file_name=f"finlab-screen-{as_of}.csv",
        mime="text/csv",
    )

# --- Company ----------------------------------------------------------------

elif page == "Company":
    st.header("Company")
    ticker = st.selectbox("Ticker", companies["ticker"].tolist())
    row = companies[companies["ticker"] == ticker].iloc[0]
    cik = int(row["cik"])
    st.subheader(f"{row['name']} · {row['sector']} · CIK {cik}")

    history = _history(cik, as_of)
    if history.empty:
        st.info(f"No annual figures were on file for {ticker} as of {as_of}.")
    else:
        revenue = history[history["canonical"] == "revenue"]
        present = set(history["canonical"].unique())
        wide = history.pivot_table(
            index="period_end", columns="canonical", values="value", aggfunc="max"
        )
        margins = [
            c
            for c in ("gross_profit", "operating_income", "net_income")
            if c in present and "revenue" in present
        ]

        left, right = st.columns(2)
        with left:
            st.caption("Revenue (annual)")
            st.altair_chart(
                alt.Chart(revenue[["period_end", "value"]])
                .mark_line(point=True)
                .encode(
                    x=alt.X("period_end:T", title=None),
                    y=alt.Y("value:Q", title="revenue", axis=alt.Axis(format="~s")),
                    tooltip=["period_end:T", alt.Tooltip("value:Q", format=",.0f")],
                )
                .properties(height=280),
                width="stretch",
            )
        with right:
            st.caption("Margins (annual)")
            if margins:
                ratios = pd.DataFrame({f"{c} margin": wide[c] / wide["revenue"] for c in margins})
                long = (
                    ratios.reset_index()
                    .melt(id_vars="period_end", var_name="margin", value_name="value")
                    .dropna()
                )
                st.altair_chart(
                    alt.Chart(long)
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
                    width="stretch",
                )
            else:
                st.info("No margin inputs resolve for this company.")

    st.subheader("Metrics vs sector")
    percentiles = _percentiles(cik, as_of)
    if percentiles.empty:
        st.info("No metrics resolve for this company at the selected date.")
    else:
        st.dataframe(
            percentiles,
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
    if not history.empty:
        st.dataframe(
            history[
                [
                    "canonical",
                    "period_end",
                    "value",
                    "form",
                    "filed_date",
                    "accession_number",
                    "derived",
                ]
            ].sort_values(["period_end", "canonical"], ascending=[False, True]),
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

    ticker = st.selectbox("Ticker", companies["ticker"].tolist())
    row = companies[companies["ticker"] == ticker].iloc[0]
    cik = int(row["cik"])

    candidates = _candidates(cik)
    if candidates.empty:
        st.info(f"No figure has been reported more than once for {ticker}.")
        st.stop()

    annual = candidates[candidates["duration_bucket"] == "annual"]
    pool = annual if not annual.empty else candidates
    bucket = "annual" if not annual.empty else pool["duration_bucket"].iloc[0]

    concept = st.selectbox("Concept", sorted(pool["canonical"].unique()))
    periods = (
        pool[pool["canonical"] == concept]
        .sort_values("period_end", ascending=False)["period_end"]
        .tolist()
    )
    period_end = st.selectbox("Fiscal period ending", periods)

    revisions = _revisions(cik, concept, period_end, as_of, bucket)
    if revisions.empty:
        st.warning(
            f"Nothing had been filed for {concept} / {period_end} as of {as_of}. "
            "Move the as-of date forward."
        )
        st.stop()

    revisions = revisions.assign(
        delta=revisions["value"].diff(),
        delta_pct=revisions["value"].pct_change(),
    )

    st.dataframe(
        revisions,
        hide_index=True,
        column_config={
            "filed_date": "filed",
            "accession_number": "accession",
            "value": st.column_config.NumberColumn("reported value", format="compact"),
            "delta": st.column_config.NumberColumn("Δ vs previous", format="compact"),
            "delta_pct": st.column_config.NumberColumn("Δ %", format="percent"),
        },
    )

    distinct = revisions["value"].nunique()
    st.metric(
        "Distinct values reported",
        distinct,
        help="More than one means this figure was restated.",
    )
    if distinct > 1:
        values = revisions["value"].tolist()
        filed = revisions["filed_date"].tolist()
        st.success(
            f"As of {as_of}, {ticker}'s {concept} for the period ending {period_end} "
            f"was first reported as {_fmt_money(values[0])} on {filed[0]} and most "
            f"recently as {_fmt_money(values[-1])} on {filed[-1]}."
        )

    st.altair_chart(
        alt.Chart(revisions[["filed_date", "value"]])
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
        width="stretch",
    )
