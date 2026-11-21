import { useEffect, useState } from 'react'
import './App.css'

type Company = { cik: number; ticker: string; name: string; sector: string }
type Test = { name: string; threshold: string; detail: string; passed: boolean | null; reference: string }
type Row = {
  ticker: string
  sector: string
  status: 'Buy candidate' | 'Watch' | 'Insufficient data'
  reason: string
  price: number | null
  day_change: number | null
  earnings_yield: number | null
  ceiling_gap: number | null
  yield_price_ceiling: number | null
  transaction_time: string | null
  tests: Test[]
}
type Dashboard = { mode: string; session: { label?: string; is_open?: boolean }; refreshed_at?: string; rows: Row[] }

type WarehouseStatus = { datasets: { entity: number; filing: number; security: number; fact: number; price_daily: number }; fundamental_range: [string | null, string | null]; price_range: [string | null, string | null] }
type WarehouseAsset = { ticker: string; available: boolean; reason?: string; company?: { cik: number; name: string; sector: string; currency: string | null; exchange: string | null; security_id: string }; latest_filing?: { accession_number: string; form: string; filed_date: string } | null; facts: Array<{ taxonomy: string; tag: string; unit: string; period_end: string; duration_bucket: string; value: number; filed_date: string }>; prices: Array<{ date: string; close: number; volume: number | null; source: string | null }> }
type DashboardWarehouseStatus = WarehouseStatus

const statusLabels: Record<Row['status'], string> = {
  'Buy candidate': 'Candidate à acheter',
  Watch: 'À surveiller',
  'Insufficient data': 'Données insuffisantes',
}

const money = (value: number | null) => value == null ? '—' : `$${value.toFixed(2)}`
const percent = (value: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`

function App() {
  const [companies, setCompanies] = useState<Company[]>([])
  const [selected, setSelected] = useState(['AAPL', 'MSFT', 'JPM'])
  const [mode, setMode] = useState<'demo' | 'live'>('demo')
  const [demoPrice, setDemoPrice] = useState(99)
  const [data, setData] = useState<Dashboard | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [warehouseStatus, setWarehouseStatus] = useState<DashboardWarehouseStatus | null>(null)
  const [warehouseAsset, setWarehouseAsset] = useState<WarehouseAsset | null>(null)

  useEffect(() => {
    fetch('/api/universe').then((response) => response.json()).then((payload) => setCompanies(payload.companies)).catch(() => setError('Impossible de joindre le backend.'))
  }, [])

  useEffect(() => {
    let active = true
    const loadStatus = async () => {
      try {
        const response = await fetch('/api/warehouse/status')
        if (response.ok && active) setWarehouseStatus(await response.json())
      } catch {
        // Status remains available at its last successful snapshot.
      }
    }
    loadStatus()
    const timer = window.setInterval(loadStatus, 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  useEffect(() => {
    const ticker = selected[0]
    if (!ticker) return
    fetch(`/api/warehouse/assets/${ticker}`)
      .then((response) => response.json())
      .then((payload) => setWarehouseAsset(payload))
      .catch(() => setWarehouseAsset(null))
  }, [selected])

  useEffect(() => {
    let active = true
    const load = async () => {
      setLoading(true)
      setError('')
      const params = new URLSearchParams()
      selected.forEach((ticker) => params.append('tickers', ticker))
      params.set('demo', String(mode === 'demo'))
      params.set('demo_price', String(demoPrice))
      try {
        const response = await fetch(`/api/market/live?${params}`)
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const payload = await response.json()
        if (active) setData(payload)
      } catch (exception) {
        if (active) setError(exception instanceof Error ? exception.message : 'Erreur inconnue')
      } finally {
        if (active) setLoading(false)
      }
    }
    load()
    const timer = window.setInterval(load, 15_000)
    return () => { active = false; window.clearInterval(timer) }
  }, [selected, mode, demoPrice])

  const toggle = (ticker: string) => setSelected((current) => current.includes(ticker) ? current.filter((item) => item !== ticker) : [...current, ticker])

  return (
    <main>
      <aside>
        <div className="brand"><span>FIN</span>LAB</div>
        <p className="eyebrow">RESEARCH WORKSTATION</p>
        <nav>
          <button className="nav-active"><span>◫</span> Marché en direct</button>
          <button disabled><span>◫</span> Weekly brief</button>
          <button disabled><span>◫</span> Company</button>
          <button className="nav-active"><span>◫</span> Data Center</button>
        </nav>
        <div className="watchlist-head"><span>WATCHLIST</span><b>{selected.length}</b></div>
        <div className="ticker-list">
          {companies.map((company) => (
            <button key={company.ticker} className={selected.includes(company.ticker) ? 'ticker selected' : 'ticker'} onClick={() => toggle(company.ticker)}>
              <span><strong>{company.ticker}</strong><small>{company.name}</small></span>
              {selected.includes(company.ticker) && <i>✓</i>}
            </button>
          ))}
        </div>
      </aside>

      <section className="workspace">
        <header>
          <div>
            <p className="eyebrow">MARKET INTELLIGENCE</p>
            <h1>Marché en direct</h1>
          </div>
          <div className="controls">
            <div className="segmented">
              <button className={mode === 'demo' ? 'active' : ''} onClick={() => setMode('demo')}>Démo</button>
              <button className={mode === 'live' ? 'active' : ''} onClick={() => setMode('live')}>Live</button>
            </div>
            {mode === 'demo' && <button className="price-control" onClick={() => setDemoPrice((value) => value <= 100 ? 101 : 99)}>${demoPrice.toFixed(0)}</button>}
          </div>
        </header>

        {mode === 'demo' && <div className="notice"><b>DÉMONSTRATION FICTIVE</b><span>Les données et prix sont simulés.</span></div>}
        {mode === 'live' && warehouseStatus && <div className="notice"><b>DONNÉES INGÉRÉES</b><span>{warehouseStatus.datasets.fact.toLocaleString('fr-FR')} faits · {warehouseStatus.datasets.price_daily.toLocaleString('fr-FR')} prix · {warehouseStatus.datasets.filing.toLocaleString('fr-FR')} filings</span></div>}
        {mode === 'live' && warehouseAsset?.available && <div className="data-center">
          <div className="table-title"><h2>{warehouseAsset.company?.name}</h2><small>{warehouseAsset.ticker} · données locales</small></div>
          <div className="step-meta" style={{ padding: '14px 18px' }}>
            <span>{warehouseAsset.facts.length} facts récents</span>
            <span>{warehouseAsset.prices.length} prix disponibles</span>
            <span>{warehouseAsset.latest_filing?.form ?? 'Filing'} · {warehouseAsset.latest_filing?.filed_date ?? '—'}</span>
            <span>{warehouseAsset.company?.sector ?? 'Secteur indisponible'}</span>
          </div>
        </div>}
        <div className="data-center">
          <div className="table-title"><h2>Data Center</h2><small>Warehouse local</small></div>
          {warehouseStatus ? (
            <div className="step-meta" style={{ padding: '14px 18px' }}>
              <span>{warehouseStatus.datasets.fact.toLocaleString('fr-FR')} faits</span>
              <span>{warehouseStatus.datasets.price_daily.toLocaleString('fr-FR')} prix</span>
              <span>{warehouseStatus.datasets.filing.toLocaleString('fr-FR')} filings</span>
              <span>{warehouseStatus.datasets.entity.toLocaleString('fr-FR')} entreprises</span>
            </div>
          ) : <div className="empty">Chargement du warehouse…</div>}
        </div>
        {error && <div className="error">{error}</div>}

        <div className="metrics">
          <article><span>Candidats</span><strong>{data?.rows.filter((row) => row.status === 'Buy candidate').length ?? 0}</strong></article>
          <article><span>Valeurs suivies</span><strong>{data?.rows.length ?? 0}</strong></article>
          <article><span>Horizon</span><strong>3–12 mois</strong></article>
          <article><span>Séance</span><strong>{data?.session.label ?? '—'}</strong></article>
        </div>

        <div className="table-shell">
          <div className="table-title"><h2>Watchlist</h2><small>{loading ? 'Actualisation…' : data?.refreshed_at ? `Actualisé ${new Date(data.refreshed_at).toLocaleTimeString('fr-FR')}` : ''}</small></div>
          <table>
            <thead><tr><th>Titre</th><th>Statut</th><th>Prix</th><th>Variation jour</th><th>Rendement BPA</th><th>Plafond 4%</th><th>Motif</th></tr></thead>
            <tbody>
              {data?.rows.map((row) => (
                <tr key={row.ticker}>
                  <td><strong>{row.ticker}</strong><small>{row.sector}</small></td>
                  <td><span className={`status ${row.status.toLowerCase().replaceAll(' ', '-')}`}>{statusLabels[row.status]}</span></td>
                  <td>{money(row.price)}</td>
                  <td className={(row.day_change ?? 0) >= 0 ? 'positive' : 'negative'}>{percent(row.day_change)}</td>
                  <td>{percent(row.earnings_yield)}</td>
                  <td>{money(row.yield_price_ceiling)}</td>
                  <td className="reason">{row.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!data?.rows.length && <div className="empty">Sélectionnez au moins une valeur dans la watchlist.</div>}
        </div>

        {data?.rows.map((row) => (
          <details className="detail" key={row.ticker}>
            <summary><strong>{row.ticker}</strong><span>{statusLabels[row.status]}</span></summary>
            <div className="tests">
              {row.tests.map((test) => (
                <article key={test.name}><span className={test.passed === true ? 'pass' : test.passed === false ? 'fail' : 'unknown'}>{test.passed === true ? '✓' : test.passed === false ? '×' : '?'}</span><div><b>{test.name}</b><p>{test.detail}</p><small>{test.reference}</small></div></article>
              ))}
            </div>
          </details>
        ))}
      </section>
    </main>
  )
}

export default App
