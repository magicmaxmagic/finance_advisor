import { useEffect, useState } from 'react'
import './App.css'
import { StrategyLab, PortfolioPanel, DataCenterControls } from './ResearchTools'

type Company = { cik: number; ticker: string; name: string; sector: string }
type WarehouseStatus = {
  published_at?: string
  warehouse_epoch?: string
  price_label?: string
  datasets: { entity: number; filing: number; security: number; fact: number; price_daily: number }
  fundamental_range: [string | null, string | null]
  price_range: [string | null, string | null]
}
type Ranking = {
  ticker: string
  name: string
  sector: string
  currency: string
  price: number | null
  earnings_yield: number | null
  momentum_12m: number | null
  momentum_3m: number | null
  momentum_1m: number | null
  volatility: number | null
  max_drawdown: number
  revenue: number | null
  score: number | null
  category: string
  data_status: string
  price_date?: string
  factor_scores?: Record<string, number | null>
  factor_weights?: Record<string, number>
  normalization?: Record<string, string>
}
type Rankings = { as_of: string; method: string; rows: Ranking[]; freshness?: { status: string } }
type WarehouseAsset = {
  ticker: string
  available: boolean
  as_of?: string
  reason?: string
  company?: { cik: number; name: string; sector: string; currency: string | null; exchange: string | null }
  latest_filing?: { accession_number: string; form: string; filed_date: string; primary_doc_url: string | null } | null
  facts: Array<{ taxonomy: string; tag: string; unit: string; period_end: string; duration_bucket: string; value: number; filed_date: string }>
  prices: Array<{ date: string; close: number; volume: number | null; source: string | null }>
}

const percent = (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`
const money = (value: number | null) => value == null ? '—' : `$${value.toLocaleString('en-US', { maximumFractionDigits: 2 })}`

async function readLocal<T>(url: string): Promise<T> {
  const response = await fetch(url)
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.detail ?? 'Données locales indisponibles.')
  return payload as T
}

function App() {
  const [companies, setCompanies] = useState<Company[]>([])
  const [rankings, setRankings] = useState<Rankings | null>(null)
  const [warehouse, setWarehouse] = useState<WarehouseStatus | null>(null)
  const [selected, setSelected] = useState('AAPL')
  const [asset, setAsset] = useState<WarehouseAsset | null>(null)
  const [page, setPage] = useState<'Overview' | 'Ranking' | 'Company' | 'Data' | 'Strategy Lab' | 'Portfolio'>('Ranking')
  const [sector, setSector] = useState('Tous')
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [rankingDate, setRankingDate] = useState('')
  const [riskMax, setRiskMax] = useState('')
  const [yieldMin, setYieldMin] = useState('')
  const [momentumMin, setMomentumMin] = useState('')

  useEffect(() => {
    Promise.all([
      readLocal<{ companies: Company[] }>('/api/universe'),
      readLocal<WarehouseStatus>('/api/warehouse/status'),
    ]).then(([universe, warehouseData]) => {
      setCompanies(universe.companies)
      setWarehouse(warehouseData)
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Données locales indisponibles.'))
  }, [])

  const effectiveDate = rankingDate || rankings?.as_of || ''

  useEffect(() => {
    let active = true
    readLocal<WarehouseAsset>(`/api/warehouse/assets/${selected}${effectiveDate ? `?as_of=${effectiveDate}` : ''}`)
      .then((data) => { if (active) setAsset(data) })
      .catch(() => { if (active) setAsset(null) })
    return () => { active = false }
  }, [selected, effectiveDate])

  useEffect(() => {
    let active = true
    readLocal<Rankings>(`/api/analytics/rankings${rankingDate ? `?as_of=${rankingDate}` : ''}`)
      .then((data) => { if (active) setRankings(data) })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : 'Données indisponibles.') })
    return () => { active = false }
  }, [rankingDate])

  const sectors = ['Tous', ...Array.from(new Set(companies.map((company) => company.sector))).sort()]
  const rows = (rankings?.rows ?? []).filter((row) =>
    (sector === 'Tous' || row.sector === sector) &&
    (!query || `${row.ticker} ${row.name}`.toLowerCase().includes(query.toLowerCase())) &&
    (!riskMax || (row.volatility != null && row.volatility * 100 <= Number(riskMax))) &&
    (!yieldMin || (row.earnings_yield != null && row.earnings_yield * 100 >= Number(yieldMin))) &&
    (!momentumMin || (row.momentum_3m != null && row.momentum_3m * 100 >= Number(momentumMin)))
  )

  return (
    <main>
      <aside>
        <div className="brand"><span>FIN</span>LAB</div>
        <p className="eyebrow">DATA-DRIVEN RESEARCH</p>
        <nav>
          {(['Overview', 'Ranking', 'Company', 'Data', 'Strategy Lab', 'Portfolio'] as const).map((item) => (
            <button key={item} className={page === item ? 'nav-active' : ''} onClick={() => setPage(item)}>
              <span>{item === 'Overview' ? '◫' : item === 'Ranking' ? '◆' : item === 'Company' ? '●' : '▦'}</span>{item === 'Overview' ? 'Vue générale' : item === 'Ranking' ? 'Classement' : item === 'Company' ? 'Analyse' : item === 'Data' ? 'Data Center' : item}
            </button>
          ))}
        </nav>
        <div className="watchlist-head"><span>UNIVERS</span><b>{companies.length}</b></div>
        <div className="ticker-list">
          {companies.map((company) => (
            <button key={company.ticker} className={selected === company.ticker ? 'ticker selected' : 'ticker'} onClick={() => { setSelected(company.ticker); setAsset(null); setPage('Company') }}>
              <span><strong>{company.ticker}</strong><small>{company.name}</small></span>
            </button>
          ))}
        </div>
      </aside>

      <section className="workspace">
        <header>
          <div><p className="eyebrow">LOCAL WAREHOUSE</p><h1>{page === 'Overview' ? 'Vue générale' : page === 'Ranking' ? 'Classement quantitatif' : page === 'Company' ? 'Analyse fondamentale' : page === 'Data' ? 'Data Center' : page}</h1></div>
          {rankings && (page === 'Ranking' || page === 'Overview') && <div className="as-of"><span>POINT-IN-TIME</span><strong>{rankings.as_of}</strong><small>{rankings.freshness?.status}</small></div>}
        </header>

        {page === 'Strategy Lab' && <StrategyLab />}
        {page === 'Portfolio' && <PortfolioPanel />}
        {page === 'Data' && <DataCenterControls />}
        {error && <div className="error">{error}</div>}

        {warehouse && (
          <div className="metrics">
            <article><span>Entreprises</span><strong>{warehouse.datasets.entity.toLocaleString('fr-FR')}</strong></article>
            <article><span>Faits SEC</span><strong>{warehouse.datasets.fact.toLocaleString('fr-FR')}</strong></article>
            <article><span>Clôtures</span><strong>{warehouse.datasets.price_daily.toLocaleString('fr-FR')}</strong></article>
            <article><span>Filings</span><strong>{warehouse.datasets.filing.toLocaleString('fr-FR')}</strong></article>
          </div>
        )}

        {page === 'Ranking' && (
          <>
            <div className="toolbar">
              <label>Recherche<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ticker ou entreprise" /></label>
              <label>Date du classement<input type="date" value={rankingDate} onChange={(event) => { setError(''); setAsset(null); setRankings(null); setRankingDate(event.target.value) }} /></label>
              <label>Secteur<select value={sector} onChange={(event) => setSector(event.target.value)}>{sectors.map((item) => <option key={item}>{item}</option>)}</select></label>
              <label>Volatilité max. %<input type="number" value={riskMax} onChange={(event) => setRiskMax(event.target.value)} /></label>
              <label>Rendement bénéficiaire min. %<input type="number" value={yieldMin} onChange={(event) => setYieldMin(event.target.value)} /></label>
              <label>Momentum 3m min. %<input type="number" value={momentumMin} onChange={(event) => setMomentumMin(event.target.value)} /></label>
              <p>{rows.length} résultats · score descriptif, pas une prédiction de profit</p>
            </div>
            <div className="table-shell">
              <table>
                <thead><tr><th>Rang</th><th>Titre</th><th>Secteur</th><th>Clôture disponible</th><th>Score</th><th>Catégorie</th><th>Momentum 3m</th><th>Volatilité</th><th>Drawdown</th><th>Qualité données</th></tr></thead>
                <tbody>{rows.map((row, index) => (
                  <tr key={row.ticker} onClick={() => { setSelected(row.ticker); setAsset(null); setPage('Company') }}>
                    <td>{index + 1}</td><td><strong>{row.ticker}</strong><small>{row.name}</small></td><td>{row.sector}</td>
                    <td>{money(row.price)}<small>{row.price_date}</small></td><td><b>{row.score?.toFixed(1) ?? '—'}</b></td><td><span className={`status ${row.category.toLowerCase().replaceAll(' ', '-')}`}>{row.category}</span></td>
                    <td className={(row.momentum_3m ?? 0) >= 0 ? 'positive' : 'negative'}>{percent(row.momentum_3m)}</td>
                    <td>{percent(row.volatility)}</td><td className="negative">{percent(row.max_drawdown)}</td><td>{row.data_status}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </>
        )}

        {page === 'Company' && asset && (!effectiveDate || asset.as_of === effectiveDate) && (
          <div className="company-view">
            {asset.available ? (
              <>
                <div className="company-header"><div><p className="eyebrow">{asset.company?.sector}</p><h2>{asset.company?.name}</h2><small>{asset.ticker} · CIK {asset.company?.cik}</small></div>{warehouse && <div className="score-ring"><strong>{rankings?.rows.find((row) => row.ticker === asset.ticker)?.score?.toFixed(1) ?? '—'}</strong><span>score</span></div>}</div>
                <section className="panel"><h3>Explication du classement historique</h3><p>Facteurs pondérés : valeur, qualité, croissance, momentum et risque. Les rangs sectoriels utilisent le marché global quand le secteur compte moins de trois observations valides.</p><div className="fact-list">{Object.entries(rankings?.rows.find((row) => row.ticker === asset.ticker)?.factor_scores ?? {}).map(([factor, score]) => <div key={factor}><strong>{factor}</strong><span>{score == null ? 'Données insuffisantes' : `${score.toFixed(1)} / 100`}</span><small>{score == null ? '' : score > 55 ? 'Signal relatif positif' : score < 45 ? 'Signal relatif négatif' : 'Neutre'}</small></div>)}</div><p>Le classement ne garantit pas un rendement futur. Les univers historiques incomplets et les révisions des fournisseurs limitent son interprétation.</p></section>
                <div className="company-grid">
                  <article><span>Dernier filing</span><strong>{asset.latest_filing?.form ?? '—'}</strong><small>{asset.latest_filing?.filed_date ?? '—'}</small></article>
                  <article><span>Prix disponible</span><strong>{asset.prices.length ? money(asset.prices[asset.prices.length - 1].close) : '—'}</strong><small>{asset.prices.length ? asset.prices[asset.prices.length - 1].date : 'Pas de prix'}</small></article>
                  <article><span>Facts récents</span><strong>{asset.facts.length}</strong><small>observations SEC</small></article>
                  <article><span>Devise</span><strong>{asset.company?.currency ?? '—'}</strong><small>{asset.company?.exchange ?? 'Lieu non renseigné'}</small></article>
                </div>
                <div className="content-grid">
                  <section className="panel"><h3>Historique des clôtures</h3>{asset.prices.length ? <svg className="sparkline" viewBox="0 0 700 220" preserveAspectRatio="none"><polyline fill="none" stroke="#62c4a3" strokeWidth="3" points={asset.prices.slice(-90).map((price, index, values) => { const closes = values.map((item) => item.close); const min = Math.min(...closes); const max = Math.max(...closes); const x = values.length === 1 ? 0 : index * 700 / (values.length - 1); const y = max === min ? 110 : 210 - (price.close - min) * 190 / (max - min); return `${x},${y}` }).join(' ')} /></svg> : <div className="empty">Aucune clôture disponible</div>}</section>
                  <section className="panel"><h3>Dernières observations SEC</h3><div className="fact-list">{asset.facts.slice(0, 12).map((fact, index) => <div key={`${fact.tag}-${fact.period_end}-${index}`}><strong>{fact.tag}</strong><span>{fact.value.toLocaleString('fr-FR')} {fact.unit}</span><small>{fact.period_end} · publié {fact.filed_date}</small></div>)}</div></section>
                </div>
              </>
            ) : <div className="empty">Données réelles indisponibles pour {asset.ticker}: {asset.reason}</div>}
          </div>
        )}

        {page === 'Overview' && rankings && (
          <div className="content-grid"><section className="panel"><h3>Top 10 — recherche quantitative</h3>{rankings.rows.slice(0, 10).map((row) => <div className="rank-list" key={row.ticker}><strong>{row.ticker}</strong><span>{row.score?.toFixed(1) ?? '—'}</span><small>{row.category}</small></div>)}</section><section className="panel"><h3>Cadre méthodologique</h3><p>Les scores combinent rendement bénéficiaire, marge nette, croissance du revenu, momentum et volatilité des données réellement disponibles. Ils ne constituent pas une recommandation d’achat ou une probabilité de profit.</p><small>{rankings.method} · as of {rankings.as_of}</small></section></div>
        )}

        {page === 'Data' && warehouse && (
          <div className="content-grid"><section className="panel"><h3>Couverture du warehouse</h3><div className="fact-list"><div><strong>Faits SEC</strong><span>{warehouse.datasets.fact.toLocaleString('fr-FR')}</span><small>{warehouse.fundamental_range[0]} → {warehouse.fundamental_range[1]}</small></div><div><strong>Prix</strong><span>{warehouse.datasets.price_daily.toLocaleString('fr-FR')}</span><small>{warehouse.price_range[0]} → {warehouse.price_range[1]}</small></div><div><strong>Filings</strong><span>{warehouse.datasets.filing.toLocaleString('fr-FR')}</span></div></div></section><section className="panel"><h3>Provenance</h3><p>{warehouse.price_label}</p><p>Publication : {warehouse.published_at}</p><p>SEC pour les fondamentaux; Yahoo ou Stooq/Alpaca seulement au Data Engine; aucune donnée externe appelée par cette vue Analytics.</p></section></div>
        )}
      </section>
    </main>
  )
}

export default App
