"""The read-only, session-aware market workstation. No broker order API exists here."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from finlab.config import Settings
from finlab.ingest.live_facts import load_facts
from finlab.ingest.live_market import MarketDataError, MarketSession, Quote, fetch_snapshot
from finlab.ingest.universe import Company, load_poc_universe
from finlab.research.live import assess_live
from finlab.research.live_demo import demo_inputs
from finlab.research.weekly import completed_friday
from finlab.ui.live_data import cached_history

NY = ZoneInfo("America/New_York")
STATUSES = {
    "Buy candidate": "Candidat à l’achat",
    "Watch": "À surveiller",
    "Insufficient data": "Données insuffisantes",
}


@st.cache_data(ttl=3600, show_spinner=False)
def _facts(identity: str, companies: tuple[Company, ...], as_of: date, _settings: Settings) -> dict:
    return load_facts(_settings, companies, as_of)[0]


@st.cache_data(ttl=15, show_spinner=False)
def _snapshot(identity: str, symbols: tuple[str, ...], _settings: Settings):
    return fetch_snapshot(_settings, list(symbols))


@st.cache_data(ttl=3600, show_spinner=False)
def _histories(identity: str, symbols: tuple[str, ...], cutoff: date, _settings: Settings):
    return cached_history(_settings, symbols, cutoff)


@st.cache_data(show_spinner=False)
def _demo(companies: tuple[Company, ...], week: date):
    return demo_inputs(companies, week)


def _identity(settings: Settings) -> str:
    # Credentials stay on the server; only an irreversible cache namespace is used as a key.
    values = [
        settings.alpaca_key_id.get_secret_value() if settings.alpaca_key_id else "",
        settings.alpaca_secret_key.get_secret_value() if settings.alpaca_secret_key else "",
        settings.sec_user_agent,
        str(settings.data_dir),
    ]
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def _cross_threshold() -> None:
    st.session_state.demo_price = 101.0 if st.session_state.demo_price <= 100 else 99.0


def _record_changes(rows: list[dict], source: str, now: datetime) -> None:
    previous = st.session_state.setdefault("market_previous", {})
    changes = st.session_state.setdefault("market_changes", [])
    for row in rows:
        key = (source, row["ticker"])
        old = previous.get(key)
        new = row["status"]
        if old is not None and old != new:
            changes.append(
                {
                    "Heure (New York)": now.astimezone(NY).isoformat(timespec="seconds"),
                    "Ticker": row["ticker"],
                    "Ancien statut": STATUSES[old],
                    "Nouveau statut": STATUSES[new],
                    "Raison": row["reason"],
                }
            )
        previous[key] = new


def _line_chart(records: list[dict], x: str) -> None:
    frame = pd.DataFrame(records)
    if frame.empty:
        st.info("Aucune paire de points synchronisés disponible.")
        return
    frame = frame.melt(x, var_name="Série", value_name="Base 100")
    st.altair_chart(
        alt.Chart(frame)
        .mark_line()
        .encode(
            x=alt.X(f"{x}:T", title="Heure de New York" if x == "time" else "Date"),
            y=alt.Y("Base 100:Q", scale=alt.Scale(zero=False)),
            color=alt.Color("Série:N", scale=alt.Scale(range=["#169d79", "#7185ae"])),
            tooltip=[f"{x}:T", "Série:N", alt.Tooltip("Base 100:Q", format=".2f")],
        )
        .properties(height=290),
        width="stretch",
    )


def _intraday_chart(intraday: dict, ticker: str) -> None:
    stock = {p["time"]: p["close"] for p in intraday.get(ticker, [])}
    market = {p["time"]: p["close"] for p in intraday.get("SPY", [])}
    times = sorted(stock.keys() & market.keys())
    if not times:
        _line_chart([], "time")
        return
    first = times[0]
    _line_chart(
        [
            {
                "time": datetime.fromisoformat(t).astimezone(NY).isoformat(),
                ticker: 100 * stock[t] / stock[first],
                "SPY": 100 * market[t] / market[first],
            }
            for t in times
        ],
        "time",
    )
    st.caption(
        "Base 100 au premier point commun; minutes communes uniquement, même séance régulière. "
        "Les minutes manquantes ne sont pas interpolées."
    )


def _detail(row: dict, intraday: dict, demo: bool) -> None:
    ticker = row["ticker"]
    st.subheader(f"{ticker} · {STATUSES[row['status']]}")
    st.write(row["reason"])
    yield_ = row.get("earnings_yield")
    if yield_ is not None and all(row.get(key) is not None for key in ("growth", "excess", "drawdown")):
        st.write(
            f"**Thèse sur 3 à 12 mois.** À {row['price']:.2f} USD, le rendement bénéficiaire "
            f"est de {yield_:.2%}; la croissance annuelle du CA est de {row['growth']:+.1%}. "
            f"L’écart de performance ajustée face à SPY est de {100 * row['excess']:+.1f} points "
            f"et la baisse maximale hebdomadaire de {abs(row['drawdown']):.1%}."
        )
    st.markdown("**Les quatre critères et leurs dates de référence**")
    rules = row.get("tests", [])
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Critère": rule.get("name", rule["detail"]),
                    "Seuil": rule.get("threshold", "—"),
                    "Valeur / seuil / explication": rule["detail"],
                    "Résultat": "Inconnu" if rule["passed"] is None else "Passe" if rule["passed"] else "Échoue",
                    "Référence": str(rule.get("reference", "indisponible")),
                }
                for rule in rules
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        f"Prix : {row.get('quote_date', 'indisponible')} · "
        f"Momentum et baisse : semaines terminées au {row.get('week', 'voir graphique')}. "
        "BPA et CA : exercice et date de publication indiqués dans les preuves ci-dessous."
    )
    ceiling = row.get("yield_price_ceiling")
    if ceiling is not None:
        st.markdown("**Scénario et invalidation**")
        a, b, c = st.columns(3)
        a.metric("Prix plafond à 4 %", f"{ceiling:.2f} USD")
        b.metric("Plafond si BPA −20 %", f"{ceiling * .8:.2f} USD")
        stressed = row.get("stressed_yield")
        c.metric("Rendement si BPA −20 %", "—" if stressed is None else f"{stressed:.2%}")
        st.write(
            f"**Condition d’invalidation :** un prix strictement supérieur à {ceiling:.2f} USD "
            "fait échouer le rendement minimal. Un CA sans croissance, un écart de momentum ≤ 0, "
            "une baisse > 35 %, ou une donnée non valide invalide également la candidature. "
            "Le passage sous le plafond ne suffit que si les trois autres critères passent."
        )
    st.write(
        "**Principale prudence :** "
        + (row["reason"] if row["status"] != "Buy candidate" else
           "le bénéfice annuel peut être au sommet d’un cycle; une baisse de 20 % réduit aussi le prix plafond de 20 %.")
    )
    tabs = st.tabs(["Séance · base 100", "Historique · un an"])
    with tabs[0]:
        _intraday_chart(intraday, ticker)
    with tabs[1]:
        _line_chart(row.get("chart", []), "date")
        st.caption("Clôtures hebdomadaires ajustées, alignées avec SPY; semaines terminées uniquement.")
    history = row.get("history")
    if history and history["windows"]:
        count = history["windows"]
        frequency = history["wins"] / count if count else 0
        st.write(
            f"**Comparaison historique :** {count} périodes non chevauchantes de 13 semaines; "
            f"surperformance {history['wins']} fois ({frequency:.1%}); "
            f"écart médian {100 * history['median_excess']:+.2f} points; "
            f"pire écart {100 * history['worst_excess']:+.2f} points."
        )
    st.caption(
        "Cette fréquence historique ne mesure pas la probabilité de succès du signal. "
        "Elle inclut toutes les périodes disponibles, même lorsque les critères auraient échoué. "
        "Univers actuel fixe, biais de survivance; rendements USD avant frais, impôts et change."
    )
    st.markdown("**Publications SEC et provenance**")
    for label, item in row.get("evidence", {}).items():
        text = f"{label} : {item['value']:,.2f} · exercice {item['period']} · publication {item['filed']}"
        if demo:
            st.write(text + " · FICTIF")
        else:
            st.markdown(f"{text} · [Publication SEC]({item['url']})")


@st.fragment(run_every="15s")
def _market(companies: tuple[Company, ...], mode: str, demo_price: float) -> None:
    now = datetime.now(UTC)
    settings = Settings()
    demo = mode == "Démo fictive"
    symbols = tuple(sorted({"SPY", *(c.ticker for c in companies)}))
    week = completed_friday(now)
    errors: dict[str, str] = {}
    history_error = None
    if demo:
        bars, facts = _demo(companies, week)
        session_date = week
        open_at = datetime.combine(session_date, time(9, 30), NY)
        session = MarketSession(True, session_date, session_date - timedelta(days=1), open_at,
                                datetime.combine(session_date, time(16), NY), "Séance fictive ouverte")
        # The synthetic clock is pinned inside its synthetic session.
        now = datetime.combine(session_date, time(14, 30), NY)
        quotes = {c.ticker: Quote(demo_price, now) for c in companies}
        closes = {}
        previous = {c.ticker: 100.0 for c in companies}
        intraday = {
            symbol: [
                {"time": (open_at + timedelta(minutes=i)).isoformat(),
                 "close": 100 + (demo_price - 100) * i / 300 if symbol != "SPY" else 100 + i / 1000}
                for i in range(301)
            ]
            for symbol in symbols
        }
        refreshed = datetime.now(UTC)
    else:
        identity = _identity(settings)
        try:
            snapshot = _snapshot(identity, symbols, settings)
        except MarketDataError as exc:
            st.error(str(exc))
            st.caption("Signal suspendu. Le mode réel ne remplace jamais une panne par des données fictives.")
            return
        session, quotes, closes = snapshot.session, snapshot.quotes, snapshot.closes
        previous, intraday = snapshot.previous_closes, snapshot.intraday
        errors, refreshed = snapshot.errors, snapshot.refreshed_at
        try:
            bars = _histories(identity, symbols, session.last_close_date, settings)
        except (MarketDataError, ValueError, OSError) as exc:
            bars = {}
            history_error = f"Historique Alpaca indisponible : {exc}"
            st.error(history_error)
        try:
            # Companyfacts lacks accepted_at. Exclude today's filings rather than guess publication time.
            facts = _facts(identity, companies, now.astimezone(NY).date(), settings)
        except Exception:
            facts = {}
            st.error("Publications SEC indisponibles. Vérifiez FINLAB_SEC_USER_AGENT et l’accès SEC; les critères fondamentaux sont suspendus.")
    st.caption(
        f"Flux : {'FICTIF' if demo else 'Alpaca IEX'} · USD · {session.label} · "
        f"Actualisation : {refreshed.astimezone(NY):%Y-%m-%d %H:%M:%S %Z} · cadence 15 s"
    )
    if not session.is_open:
        st.info(f"Hors séance : analyse de la dernière clôture du {session.last_close_date}. Prochaine actualisation dans 15 s.")
    for symbol, message in errors.items():
        st.warning(f"{symbol} : {message}")
    rows = []
    for company in companies:
        row = assess_live(company, bars.get(company.ticker, {}), bars.get("SPY", {}),
                          facts.get(company.cik, {}), week, quotes.get(company.ticker), session,
                          now, close_quote=closes.get(company.ticker), historical_error=history_error)
        row["week"] = week.isoformat()
        quote = quotes.get(company.ticker) if session.is_open else closes.get(company.ticker)
        row["transaction_time"] = quote.timestamp.astimezone(NY).isoformat(timespec="seconds") if quote else None
        price, base = row.get("price"), previous.get(company.ticker)
        row["day_change"] = price / base - 1 if price and base else None
        rows.append(row)
    _record_changes(rows, mode, refreshed)
    count = sum(r["status"] == "Buy candidate" for r in rows)
    a, b, c = st.columns(3)
    a.metric("Candidats à étudier", count)
    b.metric("Titres suivis", len(rows))
    c.metric("Horizon", "3–12 mois")
    filters = st.columns(3)
    ticker_filter = filters[0].multiselect("Filtrer les tickers", [r["ticker"] for r in rows])
    sectors = filters[1].multiselect("Secteur", sorted({r["sector"] for r in rows}))
    statuses = filters[2].multiselect("Statut", list(STATUSES.values()))
    shown = [r for r in rows if (not ticker_filter or r["ticker"] in ticker_filter)
             and (not sectors or r["sector"] in sectors)
             and (not statuses or STATUSES[r["status"]] in statuses)]
    shown.sort(key=lambda r: (list(STATUSES).index(r["status"]), -(r.get("earnings_yield") or 0), r["ticker"]))
    st.dataframe(pd.DataFrame([
        {"Ticker": r["ticker"], "Secteur": r["sector"], "Statut": STATUSES[r["status"]],
         "Prix USD": r.get("price"), "Variation jour": r.get("day_change"),
         "Rendement BPA": r.get("earnings_yield"),
         "Écart au plafond": r.get("ceiling_gap"),
         "Transaction (NY)": r.get("transaction_time"), "Motif principal": r["reason"]}
        for r in shown]), hide_index=True, width="stretch", column_config={
            col: st.column_config.NumberColumn(format="percent")
            for col in ("Variation jour", "Rendement BPA", "Écart au plafond")}),
    st.caption("Écart au plafond = plafond / prix − 1; positif : marge avant franchissement du seuil de 4 %.")
    if shown:
        ticker = st.selectbox("Détail du titre", [r["ticker"] for r in shown], key="live_detail")
        _detail(next(r for r in shown if r["ticker"] == ticker), intraday, demo)
    else:
        st.info("Aucun titre ne correspond aux filtres.")
    st.markdown("**Changements de recommandation observés pendant cette session**")
    changes = st.session_state.get("market_changes", [])
    if changes:
        st.dataframe(pd.DataFrame(changes).iloc[::-1], hide_index=True, width="stretch")
    else:
        st.caption("Aucun changement observé depuis l’ouverture de cette session.")


def render() -> None:
    st.header("Marché en direct")
    st.caption("Quels titres regarder maintenant, pourquoi à ce prix, et quand changer d’avis ?")
    settings = Settings()
    modes = ("Démo fictive", "Alpaca IEX · réel")
    mode = st.sidebar.radio("Source de données", modes, index=0 if settings.market_mode == "demo" else 1)
    universe = load_poc_universe()
    selected = st.sidebar.multiselect("Watchlist · 54 entreprises", [c.ticker for c in universe],
                                     default=["AAPL", "MSFT", "JPM"], key="live_watchlist")
    companies = tuple(c for c in universe if c.ticker in selected)
    if not companies:
        st.info("Sélectionnez au moins un titre dans la watchlist.")
        return
    demo_price = 99.0
    if mode == modes[0]:
        st.warning("DÉMONSTRATION FICTIVE · Les prix, les comptes, SPY et la séance sont simulés, même si les noms des entreprises sont réels.")
        st.number_input("Prix fictif USD · BPA fixé à 4 USD", min_value=1.0, max_value=1000.0,
                        value=99.0, step=1.0, key="demo_price")
        st.button("Franchir le seuil · 99 ↔ 101 USD", on_click=_cross_threshold)
        st.caption("À 99 USD le rendement passe; à 101 USD il échoue. Le plafond est 100 USD.")
        demo_price = st.session_state.demo_price
    else:
        st.info("Configurez les deux clés Alpaca et votre contact SEC dans .env côté serveur, puis connectez la source.")
        if st.button("Connecter / actualiser la source"):
            _snapshot.clear()
            _facts.clear()
            st.session_state.live_connected = True
        if not st.session_state.get("live_connected"):
            return
        st.caption("Publications vérifiées chaque heure. Les dépôts du jour sont utilisés à partir du lendemain (heure d’acceptation indisponible). IEX représente une seule place de marché.")
    _market(companies, mode, demo_price)
