"""Read-only weekly brief and dated evidence cards."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from finlab.research.weekly_report import read_brief


def render(directory: Path) -> None:
    st.header("Weekly investment brief")
    st.caption("US equities · USD · Weekly review for a 3 to 12 month research horizon")
    archives = sorted(directory.glob("????-??-??.zip"), reverse=True)
    example = not archives
    if example:
        st.info(
            "Your first brief is ready to generate once market data and SEC access are configured."
        )
        st.code("uv run finlab weekly", language="bash")
        st.caption(
            "Set FINLAB_SEC_USER_AGENT and the two Alpaca keys in .env, or supply a daily price CSV."
        )
        if not st.toggle("Explore a fictional example"):
            return
        from finlab.research.weekly_example import example_brief

        st.warning(
            "Example edition. All stock prices, benchmark returns and filings below are fictional."
        )
        brief = example_brief()
    else:
        chosen = st.selectbox("Week ending", archives, format_func=lambda p: p.stem)
        brief = read_brief(chosen)
    week = date.fromisoformat(brief["week"])
    if not example and (date.today() - week).days > 7:
        st.warning(
            f"Archived research from {week:%d %b %Y}. Refresh before considering a purchase."
        )
    st.caption(f"Market source: {brief['source']}. Generated {brief['generated_at'][:10]} UTC.")
    rows = brief["rows"]
    buys = [r for r in rows if r["status"] == "Buy candidate"]
    covered = [r for r in rows if "tests" in r]
    left, center, right = st.columns(3)
    left.metric("Buy candidates", len(buys))
    center.metric("Fully evaluated", f"{len(covered)} / {len(rows)}")
    right.metric("Benchmark", brief["benchmark"])
    if buys:
        best = buys[0]
        st.subheader(f"First to research · {best['ticker']}")
        st.write(
            f"At USD {best['price']:.2f}, annual diluted earnings yield was "
            f"{best['earnings_yield']:.1%}. Annual sales grew {best['growth']:.1%}, "
            f"and the stock led SPY by {best['excess'] * 100:.1f} percentage points "
            "over the momentum window."
        )
    else:
        st.info(
            "No buy candidate this week. The company evidence below shows which condition failed or which input was missing."
        )

    table = pd.DataFrame(
        [
            {
                "Ticker": r["ticker"],
                "Decision": r["status"],
                "Sector": r["sector"],
                "Earnings yield": r.get("earnings_yield"),
                "Revenue growth": r.get("growth"),
                "Spread vs SPY (pp)": None if "excess" not in r else 100 * r["excess"],
                "52-week drawdown": r.get("drawdown"),
            }
            for r in rows
        ]
    )
    st.dataframe(
        table,
        hide_index=True,
        column_config={
            col: st.column_config.NumberColumn(format="%.2f") for col in ("Spread vs SPY (pp)",)
        }
        | {
            col: st.column_config.NumberColumn(format="percent")
            for col in ("Earnings yield", "Revenue growth", "52-week drawdown")
        },
    )

    ticker = st.selectbox("Inspect a company", [r["ticker"] for r in rows])
    row = next(r for r in rows if r["ticker"] == ticker)
    st.subheader(f"{ticker} · {row['status']}")
    st.write(row["reason"])
    if "tests" in row:
        for rule in row["tests"]:
            st.write(("✓ " if rule["passed"] else "✗ ") + rule["detail"])
        if row["yield_price_ceiling"] > 0:
            st.write(
                f"**Price sensitivity.** With fiscal-year diluted EPS of "
                f"USD {row['evidence']['Diluted EPS']['value']:.2f}, the 4% yield rule allows "
                f"a maximum price of USD {row['yield_price_ceiling']:.2f}."
            )
        else:
            st.write(
                "Annual diluted earnings are zero or negative. No positive share price clears the yield hurdle."
            )
        if row.get("stressed_yield") is not None:
            stress = row["stressed_yield"]
            verdict = "passes" if stress >= 0.04 else "fails"
            st.write(
                f"**Downside scenario.** At the same share price, a 20% EPS decline "
                f"takes earnings yield to {stress:.1%}. The stock then {verdict} the valuation rule."
            )
    if row.get("chart"):
        st.markdown("**Growth of 100 over the past year**")
        frame = pd.DataFrame(row["chart"]).melt("date", var_name="Series", value_name="Index")
        st.altair_chart(
            alt.Chart(frame)
            .mark_line()
            .encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("Index:Q", scale=alt.Scale(zero=False)),
                color=alt.Color(
                    "Series:N",
                    scale=alt.Scale(domain=["Stock", "SPY"], range=["#19765e", "#8793a6"]),
                ),
                tooltip=["date:T", "Series:N", alt.Tooltip("Index:Q", format=".1f")],
            ),
            use_container_width=True,
        )
        history = row["history"]
        st.write(
            f"**Historical comparison.** In {history['windows']} completed, non-overlapping "
            f"13-week periods, {ticker} beat SPY {history['wins']} times. "
            f"The median spread was {100 * history['median_excess']:+.1f} pp and the worst "
            f"was {100 * history['worst_excess']:+.1f} pp."
        )
        st.caption(
            "These are the stock's observed returns across all sampled periods. "
            "The frequency includes periods when the screen would have rejected the stock."
        )
    if "evidence" in row:
        st.markdown("**Filing evidence**")
        for label, item in row["evidence"].items():
            if example:
                st.write(f"{label} · {item['value']:,.2f} · fictional FY ended {item['period']}")
                continue
            st.markdown(
                f"{label} · {item['value']:,.2f} · fiscal year ended {item['period']} · "
                f"[SEC filing of {item['filed']}]({item['url']})"
            )
    with st.expander("Decision rules, ranking and limitations"):
        st.write(
            "All four gates must pass: annual diluted EPS / raw USD close ≥ 4%, positive "
            "annual sales growth, positive 52-to-4-week adjusted return spread over SPY, "
            "and a peak-to-trough fall of at most 35% between weekly closes. "
            "Candidates are ordered by earnings yield, then SPY spread. The price ceiling "
            "comes from a chosen 25x annual earnings limit. Earnings can fall, and this "
            "fixed multiple can favor cyclical businesses near peak profits."
        )
        st.write(
            "Prices use split-, distribution- and spin-off-adjusted returns. IEX quotes "
            "come from one exchange. The 54-filer universe is fixed. History describes "
            "these securities in USD, before trading costs, taxes and CAD/USD conversion. "
            "These rules have yet to demonstrate out-of-sample excess returns."
        )
    if not example:
        st.download_button(
            "Download brief and source prices",
            chosen.read_bytes(),
            file_name=chosen.name,
            mime="application/zip",
        )
