import { useState } from 'react'
import { cash, classification, metricName, pct, scoreText } from './format'

export type Contributor = { metric: string; group: string; raw: number; score: number; points_from_neutral: number }
export type Security = {
  ticker: string; name: string; sector: string | null; currency: string | null; price: number | null
  price_date: string | null; opportunity: number | null; fundamental: number | null; technical: number | null
  risk: number | null; pe: number | null; ev_ebitda: number | null; trend: number | null; momentum: number | null
  volatility: number | null; valuation: number | null; profitability: number | null
  strengths: Contributor[]; concerns: Contributor[]; contradictions: string[]; eligible: boolean
}
export type Discovery = {
  as_of: string; model: string; epoch: string; captured_membership: boolean; rows: Security[]
  market: { benchmark: string; status: string; price_date?: string; price_return_12m?: number | null; regime?: string }
}
export function ScoreTiles({ row }: { row: Pick<Security, 'fundamental' | 'technical' | 'risk'> }) {
  return <div className="score-tiles">{[
    ['Fundamentals', row.fundamental], ['Technical momentum', row.technical], ['Risk resilience', row.risk],
  ].map(([label, value]) => <div className="score-tile" key={String(label)}>
    <span>{label}</span><strong>{scoreText(typeof value === 'number' ? value : null)}</strong>
    {typeof value === 'number' && <progress value={value} max="100" aria-label={String(label)} />}
  </div>)}</div>
}

export function Overview({ data, published, pipeline, openCompany, navigate, previous, study }: {
  data: Discovery; previous?: Discovery; study?: { version?: string; model_version?: string; end?: string }; published?: string; pipeline?: string; openCompany: (ticker: string) => void
  navigate: (page: 'Discover' | 'Model Validation' | 'Data Center') => void
}) {
  const opportunities = data.rows.filter(row => row.opportunity != null).sort((a,b) => b.opportunity! - a.opportunity!).slice(0, 3)
  const momentum = data.rows.filter(row => row.eligible && (row.momentum ?? 0) > 0).sort((a, b) => b.momentum! - a.momentum!)
  const conflict = data.rows.find(row => row.eligible && row.contradictions.length)
  const regime = data.market.regime === 'upward_trend' ? 'Upward trend' : data.market.regime === 'downward_trend' ? 'Downward trend'
    : data.market.regime === 'mixed' ? 'Mixed trend' : 'Trend unavailable'
  const earlier = previous?.epoch === data.epoch && previous.model === data.model
    && previous.captured_membership === data.captured_membership
    ? new Map(previous.rows.map(row => [row.ticker, row.opportunity])) : undefined
  const pipelineLabel = ({ deadletter: 'Needs attention', running: 'Sync in progress', pending: 'Queued',
    not_started: 'No status published', done: 'Last job completed', failed: 'Needs attention' } as Record<string, string>)[pipeline ?? '']
  return <>
    <section className="overview-hero panel">
      <div><span className="eyebrow">MARKET SNAPSHOT</span><h2>U.S. equities</h2>
        <p>Published daily closes through {data.market.price_date ?? data.as_of}.</p>
        <button type="button" className="primary" onClick={() => navigate('Discover')}>Explore companies</button>
      </div>
      <div className="market-reading"><span>S&amp;P 500 benchmark · SPY</span><strong>{pct(data.market.price_return_12m)}</strong>
        <span>12-month price return</span><span className="pill">{regime}</span>
      </div>
    </section>
    <div className="status-strip"><span><strong>{data.rows.length}</strong> companies in research universe</span>
      <span><strong>{data.rows.filter(row => row.opportunity != null).length}</strong> scored with sufficient evidence</span>
      <span>Data Engine · <strong>{pipelineLabel ?? pipeline?.replaceAll('_', ' ') ?? 'Status unavailable'}</strong></span>
    </div>
    <div className="section-heading"><div><h2>Top research opportunities</h2><p>Highest relative scores on {data.as_of}</p></div>
      <button type="button" onClick={() => navigate('Discover')}>View screener</button></div>
    <div className="opportunity-grid">{opportunities.length ? opportunities.map(row => <article className="panel opportunity" key={row.ticker}>
      <button type="button" className="company-link" onClick={() => openCompany(row.ticker)}><span className="ticker-mark">{row.ticker.slice(0, 2)}</span><span><strong>{row.name}</strong><small>{row.ticker} · {row.sector ?? 'Sector unavailable'}</small></span></button>
      <div className="opportunity-value"><strong>{scoreText(row.opportunity)} <small>/ 100</small></strong><span>{cash(row.price, row.currency)}</span></div>
      <p><span className="positive">Strength</span> {row.strengths[0] ? metricName(row.strengths[0].metric) : 'Unavailable'}</p>
      <p><span className="warning">Concern</span> {row.concerns[0] ? metricName(row.concerns[0].metric) : 'No below-midpoint contributor'}</p>
      <small>{classification(row.opportunity)}{earlier?.get(row.ticker) != null
        ? ` · ${((row.opportunity ?? 0) - earlier.get(row.ticker)!) >= 0 ? '+' : ''}${((row.opportunity ?? 0) - earlier.get(row.ticker)!).toFixed(1)} pts from prior week`
        : ' · Prior-week score unavailable'}</small>
    </article>) : <div className="empty">No company has enough evidence for an opportunity score at this cutoff.</div>}</div>
    <div className="overview-bottom">
      <section className="panel"><h2>Market insights</h2>
        <p>{momentum.length} eligible companies have positive three-month price momentum.</p>
        {momentum[0] && <button type="button" onClick={() => openCompany(momentum[0].ticker)}>{momentum[0].ticker} · {pct(momentum[0].momentum)}</button>}
        {conflict && <p><button type="button" className="text-button" onClick={() => openCompany(conflict.ticker)}>{conflict.ticker}</button> {conflict.contradictions[0]}</p>}
      </section>
      <section className="panel"><h2>Research activity</h2><p>Published {published ? new Date(published).toLocaleString('en-US') : 'date unavailable'}.</p>
        <p>{study?.end ? `Latest validation archive covers outcomes through ${study.end}.` : 'Validation archive date unavailable.'}</p>
        <p>Predictive evidence inconclusive.</p><button type="button" onClick={() => navigate('Model Validation')}>Review validation evidence</button> <button type="button" onClick={() => navigate('Data Center')}>View publication</button></section>
    </div>
  </>
}

const columns = {
  sector: ['Sector', (r: Security) => r.sector ?? '—'],
  price: ['Close', (r: Security) => cash(r.price, r.currency)],
  opportunity: ['Opportunity', (r: Security) => scoreText(r.opportunity)],
  fundamental: ['Fundamentals', (r: Security) => scoreText(r.fundamental)],
  technical: ['Technical', (r: Security) => scoreText(r.technical)],
  risk: ['Resilience', (r: Security) => scoreText(r.risk)],
  pe: ['Fiscal-year P/E', (r: Security) => r.pe == null ? '—' : `${r.pe.toFixed(1)}×`],
  ev_ebitda: ['EV / EBITDA', (r: Security) => r.ev_ebitda == null ? '—' : `${r.ev_ebitda.toFixed(1)}×`],
  trend: ['Price vs. SMA200', (r: Security) => pct(r.trend)],
  momentum: ['3M momentum', (r: Security) => pct(r.momentum)],
  volatility: ['Volatility', (r: Security) => pct(r.volatility)],
} as const
type Column = keyof typeof columns

export function Screener({ data, saved, openCompany }: { data: Discovery; saved?: string[]; openCompany: (ticker: string) => void }) {
  const [query, setQuery] = useState('')
  const [sector, setSector] = useState('')
  const [model, setModel] = useState<'v2' | 'legacy'>('v2')
  const [filters, setFilters] = useState({ min: '', pe: '', momentum: '', volatility: '', roa: '' })
  const [visible, setVisible] = useState<Column[]>(['sector', 'price', 'opportunity', 'fundamental', 'technical', 'risk'])
  const [sort, setSort] = useState<{ key: Column | 'name'; descending: boolean }>({ key: 'opportunity', descending: true })
  const [compact, setCompact] = useState(false)
  const [page, setPage] = useState(0)
  const [legacy, setLegacy] = useState<null | Record<string, number | null>>(null)
  const [legacyError, setLegacyError] = useState('')
  async function selectModel(value: 'v2' | 'legacy') {
    setModel(value); setPage(0)
    if (value === 'legacy' && legacy == null) {
      try {
        const response = await fetch(`/api/analytics/rankings?as_of=${data.as_of}`)
        if (!response.ok) throw new Error('Legacy ranks unavailable for this date.')
        const result = await response.json() as { rows: { ticker: string; score: number | null }[] }
        setLegacy(Object.fromEntries(result.rows.map(row => [row.ticker, row.score])))
      } catch (error) { setLegacyError(String(error)) }
    }
  }
  const rows = data.rows.map(row => model === 'legacy' ? { ...row, opportunity: legacy?.[row.ticker] ?? null } : row)
    .filter(row => (!saved || saved.includes(row.ticker)) && (!sector || row.sector === sector)
      && `${row.name} ${row.ticker}`.toLowerCase().includes(query.toLowerCase())
      && (!filters.min || row.opportunity != null && row.opportunity >= Number(filters.min))
      && (!filters.pe || row.pe != null && row.pe <= Number(filters.pe))
      && (!filters.momentum || row.momentum != null && row.momentum * 100 >= Number(filters.momentum))
      && (!filters.volatility || row.volatility != null && row.volatility * 100 <= Number(filters.volatility))
      && (!filters.roa || row.profitability != null && row.profitability * 100 >= Number(filters.roa)))
    .sort((a,b) => {
      const left = a[sort.key], right = b[sort.key]
      if (left == null) return right == null ? a.ticker.localeCompare(b.ticker) : 1
      if (right == null) return -1
      return (typeof left === 'number' && typeof right === 'number' ? left - right : String(left).localeCompare(String(right))) * (sort.descending ? -1 : 1)
    })
  const sectors = [...new Set(data.rows.flatMap(row => row.sector ?? []))].sort()
  const lastPage = Math.max(0, Math.ceil(rows.length / 15) - 1)
  const current = Math.min(page, lastPage)
  const header = (key: Column | 'name', name: string) => <th key={key} scope="col" aria-sort={sort.key === key ? sort.descending ? 'descending' : 'ascending' : 'none'}>
    <button type="button" onClick={() => setSort({ key, descending: sort.key !== key ? key !== 'name' && key !== 'sector' : !sort.descending })}>{name}{sort.key === key && (sort.descending ? ' ↓' : ' ↑')}</button>
  </th>
  return <section className="panel screener">
    <div className="toolbar">
      <label>Find a company<input type="search" placeholder="Name or ticker" value={query} onChange={event => { setQuery(event.target.value); setPage(0) }} /></label>
      <label>Sector<select value={sector} onChange={event => { setSector(event.target.value); setPage(0) }}><option value="">All sectors</option>{sectors.map(v => <option key={v}>{v}</option>)}</select></label>
      <label>Score<select value={model} onChange={event => void selectModel(event.target.value as 'v2' | 'legacy')}><option value="v2">Opportunity V2</option><option value="legacy">Legacy five-factor</option></select></label>
      <button type="button" aria-pressed={compact} onClick={() => setCompact(!compact)}>{compact ? 'Comfortable density' : 'Compact density'}</button>
    </div>
    <div className="screener-options">
      <details><summary>Advanced filters</summary><div className="toolbar">
        {([['min', 'Minimum score'], ['pe', 'Maximum P/E'], ['momentum', 'Min. momentum (%)'], ['volatility', 'Max. volatility (%)'], ['roa', 'Min. return on assets (%)']] as const).map(([key, label]) => <label key={key}>{label}<input type="number" value={filters[key]} onChange={event => { setFilters({ ...filters, [key]: event.target.value }); setPage(0) }} /></label>)}
        <button type="button" onClick={() => { setFilters({ min: '', pe: '', momentum: '', volatility: '', roa: '' }); setPage(0) }}>Reset filters</button>
      </div></details>
      <details><summary>Columns</summary><div className="toolbar">{(Object.keys(columns) as Column[]).map(key =>
        <label className="check-label" key={key}><input type="checkbox" checked={visible.includes(key)} onChange={() => setVisible(current => current.includes(key) ? current.filter(v => v !== key) : [...current, key])} />{columns[key][0]}</label>)}</div></details>
    </div>
    {model === 'legacy' && !legacy && <p role="status">{legacyError || 'Loading legacy ranks…'}</p>}
    <div className={`table-wrap ${compact ? 'compact' : ''}`} tabIndex={0} aria-label="Company results">
      <table><thead><tr>{header('name', 'Company')}{visible.map(key => header(key, key === 'opportunity' && model === 'legacy' ? 'Legacy score' : columns[key][0]))}</tr></thead>
      <tbody>{rows.slice(current * 15, (current + 1) * 15).map(row => <tr key={row.ticker}>
        <th scope="row"><button type="button" className="company-link" onClick={() => openCompany(row.ticker)}><span className="ticker-mark">{row.ticker.slice(0, 2)}</span><span><strong>{row.ticker}</strong><small>{row.name}</small></span></button></th>
        {visible.map(key => <td key={key} className={key === 'opportunity' ? 'emphasis' : ''}>{columns[key][1](row)}</td>)}
      </tr>)}</tbody></table>
    </div>
    {!rows.length && <p className="empty">{saved && !saved.length ? 'Save a company from its research page to start your watchlist.' : 'No companies match these filters.'}</p>}
    <div className="pagination"><span>{rows.length} {rows.length === 1 ? 'company' : 'companies'} · {data.as_of}</span><div>
      <button type="button" disabled={current === 0} onClick={() => setPage(current - 1)}>Previous</button>
      <span> {current + 1} / {lastPage + 1} </span>
      <button type="button" disabled={current >= lastPage} onClick={() => setPage(current + 1)}>Next</button>
    </div></div>
  </section>
}
