import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import './App.css'
import { CompanyAssessment, type Assessment } from './CompanyAssessment'
import { FinancialChart } from './DeferredChart'
import type { Point } from './FinancialChart'
import { Overview, ScoreTiles, Screener, type Discovery } from './MarketViews'
import { cash, classification, metricName, pct, scoreText } from './format'
import { StrategyLab, DataCenterControls } from './ResearchTools'
import { Compare } from './Compare'
import { ResearchGlossary } from './ResearchGlossary'
import { FinancialPerformance, type PerformanceData } from './FinancialPerformance'

const ModelValidation = lazy(() => import('./ModelValidation').then(module => ({ default: module.ModelValidation })))
const Portfolio = lazy(() => import('./Portfolio').then(module => ({ default: module.Portfolio })))

type Company = { ticker: string; name: string }
type Warehouse = {
  published_at?: string; warehouse_epoch?: string
  publication_scope?: string; base_epoch?: string
  price_sources?: string[]; last_expected_session?: string; price_current_through_expected?: boolean
  datasets: Record<string, number>; fundamental_range: [string | null, string | null]; price_range: [string | null, string | null]
}
type Asset = {
  ticker: string; as_of: string; available: boolean; reason?: string
  company?: { name: string; sector: string | null; industry?: string | null; currency: string | null; exchange: string | null; security_id: string }
  latest_filing?: { form: string; filed_date: string; primary_doc_url: string | null }
  prices: Point[]
  facts: { tag: string; unit: string; value: number; filed_date: string; period_end: string; duration_bucket: string }[]
}
const navigation = [
  { title: 'Research', pages: [['Overview', '◫'], ['Discover', '◈'], ['Company Research', '◎'], ['Compare', '⇆'], ['Model Validation', '≋'], ['Strategies', '↗']] },
  { title: 'Personal', pages: [['Portfolio', '◧'], ['Watchlist', '☆']] },
  { title: 'Operations', pages: [['Data Center', '▤']] },
] as const
type Page = typeof navigation[number]['pages'][number][0]
const slug = (page: Page) => page.toLowerCase().replaceAll(' ', '-')
const titles: Record<Page, string> = {
  Overview: 'Market overview', Discover: 'Discover companies', 'Company Research': 'Company research',
  Compare: 'Company comparison', 'Model Validation': 'Model validation', Strategies: 'Strategy lab', Portfolio: 'Portfolio',
  Watchlist: 'Watchlist', 'Data Center': 'Data Center',
}

function readLocation(): { page: Page; ticker: string } {
  const [path, ticker] = location.hash.slice(1).split('/')
  const page = navigation.flatMap(group => group.pages.map(item => item[0])).find(item => slug(item) === path) ?? 'Overview'
  return { page, ticker: ticker && /^[A-Z0-9.-]{1,18}$/i.test(ticker) ? ticker.toUpperCase() : 'AAPL' }
}

async function request<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: unknown }
    throw new Error(typeof body.detail === 'string' ? body.detail : `Local service returned HTTP ${response.status}.`)
  }
  return await response.json() as T
}

function useRequest<T>(url: string | null) {
  const [result, setResult] = useState<{ url: string; data?: T; error?: string } | null>(null)
  const [attempt, retry] = useState(0)
  useEffect(() => {
    if (!url) return
    const controller = new AbortController()
    request<T>(url, controller.signal).then(
      data => { if (!controller.signal.aborted) setResult({ url, data }) },
      error => { if (!controller.signal.aborted) setResult({ url, error: String(error.message ?? error) }) },
    )
    return () => controller.abort()
  }, [url, attempt])
  const match = result?.url === url ? result : null
  return { data: match?.data, error: match?.error,
    retry: () => { setResult(null); retry(n => n + 1) } }
}

function CompanyFinancialPerformance({ identity, cutoff }: { identity: string; cutoff: string }) {
  const [open, setOpen] = useState(false)
  const performance = useRequest<PerformanceData>(open ? `/api/analytics/financial-performance/${encodeURIComponent(identity)}?as_of=${cutoff}` : null)
  return <section className="panel company-financial-performance" id="company-financial-performance" aria-labelledby="performance-heading">
    <header><div><h2 id="performance-heading">Financial performance</h2><p>See where revenue goes, how profit changes, and which filings support the numbers.</p></div>
      <button type="button" aria-expanded={open} aria-controls="company-financial-performance-details" onClick={() => setOpen(!open)}>{open ? 'Hide financial performance' : 'Open financial performance'}</button></header>
    <div id="company-financial-performance-details" hidden={!open}>
      {open && (performance.data ? <FinancialPerformance data={performance.data} /> :
        <Pending message="Loading financial statements and events…" error={performance.error} retry={performance.retry} />)}
    </div>
  </section>
}

function Pending({ message, error, retry }: { message: string; error?: string; retry: () => void }) {
  if (error) return <section className="panel empty" role="alert"><h2>Local data unavailable</h2><p>{error}</p><button type="button" onClick={retry}>Try again</button></section>
  return <div className="panel pending" role="status"><span>{message}</span><div/><div/><div/></div>
}

function App() {
  const [locationState, setLocationState] = useState(readLocation)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('finlab-nav-compact') === 'true')
  const [mobile, setMobile] = useState(false)
  const [cutoff, setCutoff] = useState(() => {
    const value = new URLSearchParams(location.search).get('as_of')
    return value && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : ''
  })
  const [query, setQuery] = useState('')
  const [watchlist, setWatchlist] = useState<string[]>(() => {
    try { const saved: unknown = JSON.parse(localStorage.getItem('finlab-watchlist') ?? '[]'); return Array.isArray(saved) ? saved.filter(v => typeof v === 'string') : [] }
    catch { return [] }
  })
  const [compare, setCompare] = useState<string[]>(() => {
    try { const saved: unknown = JSON.parse(localStorage.getItem('finlab-compare-v4') ?? '[]')
      return Array.isArray(saved) ? [...new Set(saved.filter((entry): entry is string => typeof entry === 'string'))].slice(0,4) : [] }
    catch { return [] }
  })
  const search = useRef<HTMLInputElement>(null)
  const main = useRef<HTMLElement>(null)
  const companyChart = useRef<HTMLDivElement>(null)
  const { page, ticker } = locationState
  useEffect(() => {
    const update = () => { setLocationState(readLocation()); setMobile(false); window.scrollTo(0, 0); main.current?.focus({ preventScroll: true }) }
    window.addEventListener('hashchange', update)
    window.addEventListener('popstate', update)
    return () => { window.removeEventListener('hashchange', update); window.removeEventListener('popstate', update) }
  }, [])
  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); search.current?.focus() }
      if (event.key === 'Escape') { setQuery(''); setMobile(false) }
    }
    window.addEventListener('keydown', shortcut)
    return () => window.removeEventListener('keydown', shortcut)
  }, [])
  const navigate = (next: Page, symbol = ticker) => {
    const hash = '#' + `${slug(next)}${next === 'Company Research' || next === 'Portfolio' ? `/${symbol}` : ''}`
    if (location.hash !== hash) window.history.pushState(null, '', hash)
    setLocationState(readLocation()); window.scrollTo(0, 0); main.current?.focus({ preventScroll: true })
    setQuery(''); setMobile(false)
  }
  const openCompany = (symbol: string) => navigate('Company Research', symbol)
  const toggleCompare = (symbol: string) => {
    const next = compare.includes(symbol) ? compare.filter(item => item !== symbol) : [...compare,symbol].slice(0,4)
    localStorage.setItem('finlab-compare-v4', JSON.stringify(next)); setCompare(next)
  }
  const toggleWatch = () => {
    const next = watchlist.includes(ticker) ? watchlist.filter(item => item !== ticker) : [...watchlist, ticker]
    localStorage.setItem('finlab-watchlist', JSON.stringify(next)); setWatchlist(next)
  }
  const universe = useRequest<{ companies: Company[] }>('/api/universe')
  const warehouse = useRequest<Warehouse>('/api/warehouse/status')
  const study = useRequest<{ version?: string; model_version?: string; end?: string; headline?: { label: string } }>(
    page === 'Overview' ? '/api/analytics/model-validation' : null)
  const engine = useRequest<{ pipeline_state: string }>(page === 'Overview' ? '/api/data/status' : null)
  const effective = cutoff || warehouse.data?.price_range[1] || ''
  const showDiscovery = ['Overview', 'Discover', 'Watchlist', 'Compare'].includes(page)
  const previousDate = effective && (page === 'Overview' || page === 'Discover')
    ? new Date(new Date(effective + 'T12:00:00Z').valueOf() - 7 * 86400000).toISOString().slice(0, 10)
    : ''
  const discovery = useRequest<Discovery>(showDiscovery && effective ? `/api/analytics/discovery?as_of=${effective}` : null)
  const previous = useRequest<Discovery>(previousDate && discovery.data
    ? `/api/analytics/discovery?as_of=${previousDate}` : null)
  const companyRoute = page === 'Company Research' && effective
  const asset = useRequest<Asset>(companyRoute ? `/api/warehouse/assets/${encodeURIComponent(ticker)}?as_of=${effective}` : null)
  const identity = asset.data?.company?.security_id
  const assessment = useRequest<Assessment>(companyRoute ? `/api/analytics/factors/${encodeURIComponent(ticker)}?as_of=${effective}` : null)
  const priceHistory = useRequest<{ prices: Point[]; benchmark: Point[] }>(companyRoute && identity
    ? `/api/warehouse/history/${encodeURIComponent(identity)}?as_of=${effective}` : null)
  const candidates = query.trim() ? (universe.data?.companies ?? []).filter(company =>
    `${company.ticker} ${company.name}`.toLowerCase().includes(query.trim().toLowerCase())).slice(0, 7) : []
  const trend = assessment.data?.market_factors.find(item => item.factor === 'price_to_sma200')?.value
  const momentum = assessment.data?.market_factors.find(item => item.factor === 'return_3m')?.value
  const valuation = assessment.data?.comparisons.trailing_pe
  const riskCoverage = assessment.data?.assessment.breakdown.risk
  const riskMissing = riskCoverage ? Object.entries(riskCoverage).filter(([,group]) => group.coverage < 1).map(([key]) => metricName(key)) : []
  const score = assessment.data?.assessment
  const supportedFundamentals = score && score.fundamental_score == null
    ? Object.entries(score.breakdown.fundamental)
      .filter(([, group]) => (group.score ?? 0) > 50)
      .sort(([, a], [, b]) => (b.score ?? 0) - (a.score ?? 0)).slice(0, 2)
    : []
  const prices = priceHistory.data?.prices ?? asset.data?.prices ?? []
  const latest = prices.at(-1), prior = prices.at(-2)
  const dailyChange = latest && prior && prior.close > 0 ? latest.close / prior.close - 1 : null
  const dataNeeded = showDiscovery
  const company = asset.data?.company
  const drivers = score?.positive_contributors ?? []
  const concerns = score?.negative_contributors ?? []

  return <div className={`app-shell ${collapsed ? 'rail' : ''}`}>
    <a className="skip-link" href="#main-content" onClick={event => { event.preventDefault(); main.current?.focus({ preventScroll: true }) }}>Skip to content</a>
    {mobile && <button type="button" className="nav-scrim" aria-label="Close navigation" onClick={() => setMobile(false)} />}
    <aside id="app-navigation" className={`sidebar ${mobile ? 'mobile-open' : ''}`}>
      <div className="brand"><button type="button" onClick={() => navigate('Overview')} aria-label="FinLab home">
        <span className="brand-mark">F</span><span className="nav-label">Fin<span>Lab</span></span>
      </button></div>
      <nav aria-label="Main navigation">{navigation.map(group => <div className="nav-group" key={group.title}>
        <p className="nav-label">{group.title}</p>
        {group.pages.map(([item, icon]) => <button key={item} type="button" title={item} aria-label={item}
          aria-current={page === item ? 'page' : undefined} onClick={() => navigate(item)}>
          <span aria-hidden="true" className="nav-icon">{icon}</span><span className="nav-label">{item}</span>
        </button>)}
      </div>)}</nav>
      <div className="sidebar-end"><small className="nav-label">Local research workspace</small><button type="button" title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} onClick={() => { setCollapsed(!collapsed); localStorage.setItem('finlab-nav-compact', String(!collapsed)) }}>{collapsed ? '▣' : '◫'}<span className="nav-label"> Collapse sidebar</span></button></div>
    </aside>

    <div className="workspace">
      <header className="topbar">
        <button type="button" className="mobile-toggle" aria-controls="app-navigation" aria-expanded={mobile}
          onClick={() => setMobile(!mobile)} aria-label="Toggle navigation">☰</button>
        <form className="global-search" role="search" onSubmit={event => { event.preventDefault(); if (candidates[0]) openCompany(candidates[0].ticker) }}>
          <label className="sr-only" htmlFor="global-search">Search companies</label>
          <span aria-hidden="true">⌕</span><input id="global-search" ref={search} type="search" value={query} placeholder="Search companies"
            autoComplete="off" onChange={event => setQuery(event.target.value)} aria-controls={query.trim() ? 'global-results' : undefined} />
          <kbd>⌘ K</kbd>
          {query.trim() && <div className="search-results" id="global-results" aria-label="Company matches">
            {candidates.length ? candidates.map(item => <button type="button" key={item.ticker} onClick={() => openCompany(item.ticker)}><strong>{item.ticker}</strong> {item.name}</button>)
              : <p>{universe.error ? 'Company directory unavailable.' : 'No matching companies.'}</p>}
          </div>}
        </form>
        <div className="topbar-date"><label className="date-control">As of<input type="date" aria-label="Research cutoff date" value={cutoff || effective} max={warehouse.data?.price_range[1] ?? undefined} onChange={event => setCutoff(event.target.value)} /></label>
        {cutoff && <button type="button" className="latest-button" onClick={() => setCutoff('')}>Latest</button>}</div>
      </header>

      <main id="main-content" className="page-content" tabIndex={-1} ref={main}>
        <div className="page-heading"><div><span className="eyebrow">{page === 'Data Center' ? 'DATA ENGINE' : 'FINLAB ANALYTICS'}</span>
          <h1>{titles[page]}</h1></div>
          {effective && <span className="pill history-label">{cutoff ? 'Historical cutoff' : 'Latest published'} · {effective}</span>}
        </div>

        {dataNeeded && !discovery.data && <Pending message="Loading published research…" error={warehouse.error ?? discovery.error} retry={warehouse.error ? warehouse.retry : discovery.retry} />}
        {page === 'Overview' && discovery.data && <Overview data={discovery.data} pipeline={engine.data?.pipeline_state} previous={previous.data} study={study.data}
          published={warehouse.data?.published_at} openCompany={openCompany} navigate={navigate} />}
        {(page === 'Discover' || page === 'Watchlist') && discovery.data && <Screener key={`${page}-${effective}`}
          data={discovery.data} saved={page === 'Watchlist' ? watchlist : undefined} openCompany={openCompany}
          compare={compare} toggleCompare={toggleCompare} openCompare={() => navigate('Compare')} prior={previous.data} />}
        {page === 'Compare' && discovery.data && <Compare data={discovery.data} selected={compare} toggle={toggleCompare}
          openCompany={openCompany} discover={() => navigate('Discover')} />}

        {page === 'Company Research' && !asset.data && <Pending message={`Loading ${ticker}…`} error={warehouse.error ?? asset.error} retry={warehouse.error ? warehouse.retry : asset.retry} />}
        {page === 'Company Research' && asset.data && !asset.data.available && <section className="panel empty"><h2>{ticker}</h2><p>{asset.data.reason}</p><button type="button" onClick={() => navigate('Discover')}>Browse companies</button></section>}
        {page === 'Company Research' && asset.data?.available && <>
          <div className="company-path"><button type="button" className="text-button" onClick={() => navigate('Discover')}>Browse companies</button>
            <button type="button" className="text-button" onClick={() => {
              companyChart.current?.focus({ preventScroll: true })
              companyChart.current?.scrollIntoView({ block: 'start' })
            }}>View price history</button>
            {compare.length >= 2 && <button type="button" onClick={() => navigate('Compare')}>View comparison · {compare.length}</button>}</div>
          <section className="panel company-heading">
            <div className="company-identity"><span className="ticker-mark">{ticker.slice(0, 2)}</span><div><h2>{company?.name ?? ticker}</h2><p>{ticker} · {company?.sector ?? 'Sector unavailable'}{company?.industry && company.industry !== company.sector ? ` · ${company.industry}` : ''}{company?.exchange ? ` · ${company.exchange}` : ''}</p></div></div>
            <div className="company-price"><strong>{cash(latest?.close, company?.currency)}</strong><span>{latest?.date ?? 'No published close'}{company?.currency && ` · ${company.currency}`}{dailyChange != null && <> · <b className={dailyChange >= 0 ? 'positive' : 'negative'}>{pct(dailyChange)}</b> vs. prior observed close</>}</span></div>
            <div className="company-actions"><button type="button" aria-pressed={watchlist.includes(ticker)} onClick={toggleWatch}>{watchlist.includes(ticker) ? '★ Saved' : '☆ Watchlist'}</button>
              <button type="button" aria-pressed={compare.includes(ticker)} disabled={compare.length >= 4 && !compare.includes(ticker)}
                onClick={() => toggleCompare(ticker)}>{compare.includes(ticker) ? 'Remove from comparison' : 'Add to comparison'}</button>
              <button type="button" onClick={() => navigate('Portfolio',ticker)}>Portfolio impact</button></div>
          </section>
          {assessment.data && score ? <>
            <section className="panel assessment-hero"><div><span className="eyebrow">RESEARCH SCORE</span><h2>Investment opportunity</h2>
              <div className="hero-score">{score.opportunity_score == null ? <strong className="score-unavailable">Unavailable</strong>
                : <>{scoreText(score.opportunity_score)} <span>/ 100</span></>}</div>
              <p>{score.opportunity_score == null ? 'The available evidence does not support a combined score for this date.'
                : `${classification(score.opportunity_score)}. ${drivers[0] ? `${metricName(drivers[0].metric)} is the largest positive contributor.` : 'Inspect the score breakdown for supporting evidence.'}`}</p>
              <small>Relative research score <button className="text-button" type="button" onClick={() => navigate('Model Validation')}>· Review predictive evidence</button></small></div>
              <ScoreTiles row={{ fundamental: score.fundamental_score, technical: score.technical_score, risk: score.risk_resilience_score }} breakdown={score.breakdown} />
              <div className="assessment-quick">
                <div><strong>Valuation</strong><span>{valuation?.historical_percentile != null ? `P/E at historical percentile ${valuation.historical_percentile.toFixed(0)}` : 'Comparable P/E history unavailable'}</span></div>
                <div><strong>Three-month momentum</strong><span>{momentum == null ? 'Unavailable' : `${momentum > 0 ? 'Up' : momentum < 0 ? 'Down' : 'Flat'} ${pct(Math.abs(momentum))}`}</span></div>
                <div><strong>Market Risk Score</strong><span>{scoreText(score.market_risk_score)}{score.market_risk_score != null && ' / 100'}</span></div>
                <div><strong>Financial Risk Score</strong><span>{scoreText(score.financial_risk_score)}{score.financial_risk_score != null && ' / 100'}</span></div>
              </div>
            </section>
            <h2>What the analysis tells us</h2>
            <div className="evidence-grid">
              <section className="panel"><h2 className="positive">Strengths</h2>{drivers.length || supportedFundamentals.length ? <ul>
                {supportedFundamentals.map(([key, group]) => <li key={key}>{metricName(key)} · {scoreText(group.score)}/100 in the evaluated group</li>)}
                {drivers.slice(0, 3 - supportedFundamentals.length).map(item => <li key={item.metric}>{metricName(item.metric)} · {scoreText(item.score)}/100</li>)}
              </ul> : <p>The score groups we can evaluate show no above-midpoint strengths.</p>}</section>
              <section className="panel"><h2 className="warning">Concerns</h2>
                {trend != null && trend < 0 && momentum != null && momentum < 0 && <p>The stock closed {pct(Math.abs(trend))} below its 200-session average and fell {pct(Math.abs(momentum))} over 63 sessions.</p>}
                {assessment.data.market_cap_diagnostic && score.fundamental_score == null && <p>{assessment.data.market_cap_diagnostic.message}</p>}
                {riskMissing.length > 0 && <p>Risk evidence gap in {riskMissing.join(' and ')}.</p>}
                {concerns.length > 0 && <ul>{concerns.map(item => <li key={item.metric}>{metricName(item.metric)} · {scoreText(item.score)}/100</li>)}</ul>}
                {trend == null && !riskMissing.length && !concerns.length && !assessment.data.market_cap_diagnostic && <p>Review the evidence before drawing a conclusion.</p>}</section>
              <section className="panel"><h2>What to watch</h2>{score.contradictions.length ? score.contradictions.map(item => <p key={item}>{item}</p>)
                : <p>{trend != null ? `Price is ${pct(Math.abs(trend))} ${trend >= 0 ? 'above' : 'below'} its 200-day average. Watch for a close on the other side of that level.` : 'Trend confirmation needs a full 200-session price window.'}</p>}</section>
            </div>
          </> : <Pending message="Evaluating fundamentals, market conditions, and risk…" error={assessment.error} retry={assessment.retry} />}
          <div className="company-price-history" ref={companyChart} tabIndex={-1}>
            <FinancialChart key={`prices-${ticker}`} points={prices} benchmark={priceHistory.data?.benchmark} label="Price history" currency={company?.currency} />
          </div>
          {priceHistory.error && <p role="status">Extended history unavailable. Showing recent prices. <button type="button" onClick={priceHistory.retry}>Retry history</button></p>}
          {identity && <CompanyFinancialPerformance key={`${identity}-${effective}`} identity={identity} cutoff={effective} />}
          {assessment.data && <CompanyAssessment key={`evidence-${ticker}`} data={assessment.data} />}
          <ResearchGlossary />
          <details className="panel"><summary>Latest filing and source observations</summary>
            <p>{asset.data.latest_filing ? `${asset.data.latest_filing.form} · filed ${asset.data.latest_filing.filed_date}` : 'No filing available at this cutoff.'}</p>
            <div className="table-wrap"><table><thead><tr><th>Observation</th><th>Period</th><th>Filed</th><th>Value</th></tr></thead>
              <tbody>{asset.data.facts.map((fact, index) => <tr key={`${fact.tag}-${fact.period_end}-${index}`}><th scope="row">{metricName(fact.tag)}</th><td>{fact.period_end} · {fact.duration_bucket}</td><td>{fact.filed_date}</td><td>{fact.value.toLocaleString('en-US')} {fact.unit}</td></tr>)}</tbody></table></div>
          </details>
        </>}

        {page === 'Model Validation' && <Suspense fallback={<p role="status">Loading research charts…</p>}><ModelValidation /></Suspense>}
        {page === 'Strategies' && <StrategyLab />}
        {page === 'Portfolio' && <Suspense fallback={<p role="status">Loading portfolio…</p>}><Portfolio key={ticker} companies={universe.data?.companies ?? []} initialTicker={ticker} latest={effective} /></Suspense>}
        {page === 'Data Center' && <>
          {warehouse.data ? <div className="status-grid">{Object.entries(warehouse.data.datasets).map(([key, count]) =>
            <article className="panel" key={key}><span>{({ entity: 'Tracked issuers', filing: 'SEC filings', security: 'Security records', fact: 'Financial observations', price_daily: 'Daily price observations' } as Record<string,string>)[key] ?? key}</span><strong>{count.toLocaleString('en-US')}</strong></article>)}</div>
            : <Pending message="Loading warehouse status…" error={warehouse.error} retry={warehouse.retry} />}
          {warehouse.data && <section className="panel"><h2>Current publication</h2><dl className="publication"><dt>Published</dt><dd>{warehouse.data.published_at ? new Date(warehouse.data.published_at).toLocaleString('en-US') : 'Unavailable'}</dd>
            {warehouse.data.publication_scope === 'evidence_only' && <><dt>Scope</dt><dd>Share-class and event evidence added. Core data remains pinned to {warehouse.data.base_epoch}.</dd></>}
            <dt>Price freshness</dt><dd>{warehouse.data.price_current_through_expected
              ? `Published through expected session ${warehouse.data.last_expected_session}`
              : warehouse.data.last_expected_session ? `Expected through ${warehouse.data.last_expected_session}` : 'Unavailable'}</dd>
            <dt>Price sources in publication</dt><dd>{warehouse.data.price_sources?.join(', ') || 'Unavailable'}</dd>
            <dt>Price coverage</dt><dd>{warehouse.data.price_range.join(' to ')}</dd><dt>Filing coverage</dt><dd>{warehouse.data.fundamental_range.join(' to ')}</dd>
            <dt>Snapshot epoch</dt><dd>{warehouse.data.warehouse_epoch}</dd></dl></section>}
          <DataCenterControls publication={warehouse.data?.published_at} asOf={effective || undefined} />
        </>}
      </main>
    </div>
  </div>
}

export default App
