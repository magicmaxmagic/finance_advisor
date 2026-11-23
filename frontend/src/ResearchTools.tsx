import { useEffect, useState } from 'react'
import { FinancialChart } from './DeferredChart'
import { cash, pct } from './format'

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const result = await response.json()
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail))
  return result as T
}

type Equity = { date: string; value: number }[]
type StrategyResult = {
  start: string; end: string; strategy: string; tickers: string[]; metrics: Record<string, number | null>
  benchmark_metrics: Record<string, number | null>; equity: Equity; benchmark_equity: Equity; caveats: string[]; warehouse_epoch: string
}
const strategyLabels = {
  equal_weight: 'Monthly equal weight', buy_hold: 'Buy and hold', momentum: '12-minus-1 momentum',
  value: 'Value', quality: 'Quality', multifactor: 'Legacy multifactor',
} as const

const scalar = (n?: number | null) => n == null ? '—' : n.toLocaleString('en-US', { maximumFractionDigits: 3 })

export function StrategyLab() {
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [tickers, setTickers] = useState('SPY')
  const [strategy, setStrategy] = useState<keyof typeof strategyLabels>('equal_weight')
  const [result, setResult] = useState<StrategyResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [view, setView] = useState<'equity' | 'drawdown'>('equity')
  async function run() {
    setBusy(true); setError(''); setResult(null)
    try { setResult(await post('/api/analytics/backtest', { start, end, tickers: tickers.split(',').map(s => s.trim().toUpperCase()).filter(Boolean), strategy })) }
    catch (reason) { setError(String(reason)) }
    finally { setBusy(false) }
  }
  function chartSeries(series: Equity) {
    let peak = 0
    return series.map(point => {
      peak = Math.max(peak, point.value)
      return { date: point.date, close: view === 'equity' ? point.value / series[0].value * 100 : (point.value / peak - 1) * 100 }
    })
  }
  return <>
    <section className="panel">
      <h2>Run a historical simulation</h2><p>Signals execute at the next open. The simulation uses 5 basis points of fees and 5 basis points of slippage per trade side.</p>
      <form className="toolbar" onSubmit={event => { event.preventDefault(); void run() }}>
        <label>Start date<input type="date" required value={start} onChange={event => setStart(event.target.value)} /></label>
        <label>End date<input type="date" required min={start || undefined} value={end} onChange={event => setEnd(event.target.value)} /></label>
        <label>Tickers, separated by commas<input required value={tickers} onChange={event => setTickers(event.target.value)} /></label>
        <label>Strategy<select value={strategy} onChange={event => setStrategy(event.target.value as keyof typeof strategyLabels)}>
          {Object.entries(strategyLabels).map(([key,label]) => <option key={key} value={key}>{label}</option>)}
        </select></label>
        <button className="primary" disabled={busy || !start || !end || start >= end}>{busy ? 'Simulating…' : 'Run simulation'}</button>
      </form>
      {busy && <p role="status">Running against the local warehouse…</p>}{error && <p className="error" role="alert">{error}</p>}
    </section>
    {result && <>
      <section className="panel"><h2>{strategyLabels[result.strategy as keyof typeof strategyLabels] ?? result.strategy}</h2>
        <p>{result.start} to {result.end} · {result.tickers.join(', ')} · Benchmark SPY</p>
        <p>Historical simulation. The Model Validation section contains the separate walk-forward study.</p>
        <div className="table-wrap"><table><thead><tr><th>Measure</th><th>Strategy</th><th>SPY</th></tr></thead>
          <tbody>{Object.entries({ cagr: 'Annualized return', sharpe: 'Sharpe ratio', sortino: 'Sortino ratio', max_drawdown: 'Maximum drawdown', information_ratio: 'Information ratio' }).map(([key,label]) =>
            <tr key={key}><th scope="row">{label}</th><td>{key === 'cagr' || key === 'max_drawdown' ? pct(result.metrics[key]) : scalar(result.metrics[key])}</td>
            <td>{key === 'cagr' || key === 'max_drawdown' ? pct(result.benchmark_metrics[key]) : scalar(result.benchmark_metrics[key])}</td></tr>)}</tbody></table></div>
      </section>
      <div className="segments" aria-label="Strategy chart"><button type="button" aria-pressed={view === 'equity'} onClick={() => setView('equity')}>Equity curve</button>
        <button type="button" aria-pressed={view === 'drawdown'} onClick={() => setView('drawdown')}>Drawdown</button></div>
      <FinancialChart points={chartSeries(result.equity)} benchmark={chartSeries(result.benchmark_equity)}
        label={view === 'equity' ? 'Equity indexed to 100' : 'Drawdown (%)'} comparisonRebased={false}
        initialOverlays={['SPY']} initialRange="MAX" showPriceTools={false} units={view === 'drawdown' ? '%' : undefined} />
      <details className="panel"><summary>Assumptions and all results</summary>
        <ul>{result.caveats.map(text => <li key={text}>{text}</li>)}</ul>
        <pre>{JSON.stringify(result.metrics, null, 2)}</pre>
      </details>
    </>}
  </>
}

type Holdings = {
  base_currency: string; market_value: number; realized_pnl: number; unrealized_pnl: number
  holdings: { security_id: string; quantity: number; price_date: string; market_value: number; weight: number }[]
  risk?: { annualized_volatility?: number }
}
type PortfolioResult = { result: Holdings | { before: Holdings; after: Holdings }; assumptions: string[] }
export function PortfolioPanel() {
  const [csv, setCsv] = useState(() => localStorage.getItem('finlab-portfolio-csv') ?? 'date,security_id,side,quantity,price,currency,fx_to_base,fee\n')
  const [asOf, setAsOf] = useState('')
  const [base, setBase] = useState('USD')
  const [fx, setFx] = useState('')
  const [scenario, setScenario] = useState('')
  const [amount, setAmount] = useState('')
  const [result, setResult] = useState<PortfolioResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  function save(text: string) { setCsv(text); localStorage.setItem('finlab-portfolio-csv', text) }
  async function run() {
    setBusy(true); setError(''); setResult(null)
    try { setResult(await post('/api/portfolio/analyze', { csv_data: csv, as_of: asOf, base_currency: base, foreign_fx: fx ? Number(fx) : undefined,
      scenario_security: scenario || undefined, scenario_amount: scenario ? Number(amount) : undefined })) }
    catch (reason) { setError(String(reason)) }
    finally { setBusy(false) }
  }
  const values = result?.result
  const positions = values && ('after' in values ? values.after : values)
  return <>
    <section className="panel">
      <h2>Value a portfolio</h2><p>Upload or edit a transaction CSV. Its contents are saved in this browser.</p>
      <label className="field">Import transaction CSV<input type="file" accept=".csv,text/csv" onChange={async event => {
        const file = event.target.files?.[0]
        if (!file) return
        try { save(await file.text()); setError('') } catch { setError('Could not read the selected file.') }
      }} /></label>
      <label className="field">Transactions<textarea rows={5} value={csv} onChange={event => save(event.target.value)} /></label>
      <details><summary>CSV format</summary><p>Use buy or sell for side, a stable security ID or unique ticker, and the transaction-date FX rate for fx_to_base. Use 1 for transactions already in the base currency.</p></details>
      <form className="toolbar" onSubmit={event => { event.preventDefault(); void run() }}>
        <label>Valuation date<input type="date" value={asOf} onChange={event => setAsOf(event.target.value)} required /></label>
        <label>Base currency<select value={base} onChange={event => setBase(event.target.value)}><option>USD</option><option>CAD</option></select></label>
        <label>Foreign-to-base valuation FX<input type="number" min="0.000001" step="any" value={fx} onChange={event => setFx(event.target.value)} /></label>
        <label>Scenario ticker<input value={scenario} onChange={event => setScenario(event.target.value)} /></label>
        <label>Scenario investment<input type="number" min="0.01" step="any" required={!!scenario} value={amount} onChange={event => setAmount(event.target.value)} /></label>
        <button className="primary" disabled={busy || !asOf}>{busy ? 'Analyzing…' : 'Analyze holdings'}</button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
    </section>
    {positions && <section className="panel"><h2>{values && 'after' in values ? 'Scenario holdings' : 'Portfolio holdings'}</h2>
      {values && 'before' in values && <p>Before {cash(values.before.market_value, positions.base_currency)} · After {cash(values.after.market_value, positions.base_currency)}</p>}
      <p>Market value {cash(positions.market_value, positions.base_currency)} · Unrealized P/L {cash(positions.unrealized_pnl, positions.base_currency)} · Realized P/L {cash(positions.realized_pnl, positions.base_currency)}</p>
      <p>Estimated annual volatility {pct(positions.risk?.annualized_volatility)}</p>
      <div className="table-wrap"><table><thead><tr><th>Security</th><th>Quantity</th><th>Quote date</th><th>Value</th><th>Weight</th></tr></thead><tbody>
        {positions.holdings.map(item => <tr key={item.security_id}><th scope="row">{item.security_id}</th><td>{scalar(item.quantity)}</td><td>{item.price_date}</td>
          <td>{cash(item.market_value, positions.base_currency)}</td><td>{pct(item.weight)}</td></tr>)}</tbody></table></div>
      <details><summary>Assumptions and full results</summary><ul>{result?.assumptions.map(text => <li key={text}>{text}</li>)}</ul><pre>{JSON.stringify(values, null, 2)}</pre></details>
    </section>}
  </>
}

type Job = { name:string; state:string; detail:string; finished_at?:string|null; started_at?:string|null }
type EngineState = { updated_at:string|null; pipeline_state:string; steps:Job[]; recent_jobs?:Job[];
  sec_sync?: { state: string; stale: boolean; last_success_at?: string | null;
    pending: { cik: number; ticker: string; accession: string; filed_date: string; reason?: string; report_period?: string; inline_xbrl?: boolean }[];
    verified_recoveries?: Record<string, { ticker: string; accession: string; source_url: string; observation_count: number }>;
    issuer_errors: Record<string, { code: string; retryable: boolean }> } }
type QualityState = { scope: string; rows: { ticker: string; status: string; missing_sessions?: string[] }[] }
type FinancialMetricCoverage = { available: number; coverage_pct: number|null; main_missing_reason: string|null;
  source: string; structurally_unavailable_at_cutoff: number; unresolved_or_conditionally_recoverable: number;
  affected_securities: { ticker: string; security_id: string; reason: string }[] }
type FinancialCoverage = { samples: Record<string, { business_models: Record<string, { eligible: number; metric_coverage: Record<string, FinancialMetricCoverage> }> }> }
const auditedMeasures = {
  annual_statement: 'Annual statements', reported_shares: 'Reported share counts', reported_market_cap: 'Historical market capitalization',
  trailing_pe: 'Fiscal-year P/E', ttm_pe: 'Trailing 12-month P/E', ev_to_operating_ebitda: 'EV / EBITDA', roic: 'Return on invested capital',
  fcf_yield: 'Free cash flow yield', revenue_growth: 'Revenue growth', earnings_growth: 'Earnings growth',
  debt_to_equity: 'Debt / equity', interest_coverage: 'Interest coverage',
  nonoperating_interest_coverage: 'Nonoperating interest coverage', current_ratio: 'Current ratio',
  fundamental_score: 'Fundamental Score', market_risk_score: 'Market Risk Score', financial_risk_score: 'Financial Risk Score',
  opportunity_score: 'Opportunity Score',
}

export function DataCenterControls({ publication, asOf }: { publication?: string; asOf?: string }) {
  const [status, setStatus] = useState<EngineState | null>(null)
  const [quality, setQuality] = useState<QualityState | null>(null)
  const [financial, setFinancial] = useState<FinancialCoverage | null>(null)
  const [selectedMetric, setSelectedMetric] = useState<keyof typeof auditedMeasures>('opportunity_score')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/data/status', { signal: controller.signal }).then(response => {
      if (!response.ok) throw new Error('Engine status unavailable.')
      return response.json()
    }).then(setStatus).catch(error => { if (!controller.signal.aborted) setMessage(String(error)) })
    return () => controller.abort()
  }, [])
  async function inspect() {
    setBusy(true); setMessage('')
    try {
      const cutoff = asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''
      const results = await Promise.allSettled(([
        ['/api/data/status', setStatus], [`/api/warehouse/quality${cutoff}`, setQuality],
        [`/api/warehouse/financial-coverage${cutoff}`, setFinancial],
      ] as const).map(async ([url, apply]) => {
        const response = await fetch(url, { signal: AbortSignal.timeout(30_000) })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        apply(await response.json())
      }))
      if (results.some(result => result.status === 'rejected')) setMessage('Some coverage details are unavailable. Retry to check current data.')
    } catch (reason) { setMessage(String(reason)) }
    finally { setBusy(false) }
  }
  async function refresh() {
    setBusy(true); setConfirm(false)
    try { setMessage((await post<{ message: string }>('/api/data/refresh', {})).message) }
    catch (reason) { setMessage(String(reason)) }
    finally { setBusy(false) }
  }
  const state = status?.pipeline_state
  const needsAttention = state === 'failed' || state === 'deadletter'
  const statusText = !state ? 'Status unavailable' : state === 'not_started' ? 'No sync status yet'
    : needsAttention ? 'Needs attention' : state === 'running' ? 'Working' : state === 'done' ? 'Completed' : state.replaceAll('_',' ')
  const jobs = status?.recent_jobs ?? []
  const sec = status?.sec_sync
  const recoveries = Object.values(sec?.verified_recoveries ?? {})
  const [financialDate, sample] = Object.entries(financial?.samples ?? {})[0] ?? []
  const metrics = sample?.business_models.all.metric_coverage
  const affected = metrics?.[selectedMetric]
  const successful = [...jobs,...(status?.steps ?? [])].filter(step => step.state === 'done' && ['prices','price_corrections'].includes(step.name))
    .map(step=>step.finished_at).filter(Boolean).sort().at(-1)
  return <section className="panel"><h2>Synchronization health</h2>
    <div className="v4-context"><strong className={needsAttention ? 'warning' : ''}>{statusText}</strong>
      <p>{successful ? `Last successful price synchronization ${new Date(successful).toLocaleString('en-US')}.`
        : publication ? `Latest successful warehouse publication ${new Date(publication).toLocaleString('en-US')}.` : 'No successful job timestamp is available.'}</p>
      {needsAttention && <p>Open job details to see what prevented the most recent update. The last validated publication remains available.</p>}
    </div>
    {sec && <div className="v4-context">
      <strong>SEC statements</strong>
      <p>{sec.last_success_at ? `Last verified SEC synchronization ${new Date(sec.last_success_at).toLocaleString('en-US')}.` : 'No verified SEC synchronization has been recorded.'}
        {sec.stale && ' The verification status is stale.'}</p>
      <p>{sec.pending.length} pending statements · {Object.keys(sec.issuer_errors).length} issuer errors.</p>
      {recoveries.length > 0 && <details><summary>{recoveries.length} statements verified from primary SEC filings</summary>
        <ul>{recoveries.map(item => <li key={item.accession}><a href={item.source_url} target="_blank" rel="noreferrer">{item.ticker} {item.accession}</a> · {item.observation_count} consolidated observations</li>)}</ul>
      </details>}
      {(sec.pending.length > 0 || Object.keys(sec.issuer_errors).length > 0) && <details><summary>SEC issuer diagnostics</summary>
        <ul>{sec.pending.map(item => <li key={item.accession}>{item.ticker} · filed {item.filed_date}{item.report_period && ` · period ended ${item.report_period}`} · {item.accession}
          {item.inline_xbrl && ' · Inline XBRL confirmed'} · {item.reason === 'ACCESSION_MISSING_IN_COMPANY_FACTS' ? 'No normalized observations for this accession in SEC Company Facts' : item.reason ?? 'Awaiting verification'}</li>)}
          {Object.entries(sec.issuer_errors).map(([cik, failure]) => <li key={cik}>{cik} · {failure.code} · {failure.retryable ? 'Retryable' : 'Requires attention'}</li>)}</ul>
      </details>}
    </div>}
    <p>{status ? `Status checked ${status.updated_at ? new Date(status.updated_at).toLocaleString('en-US') : 'No status timestamp'}` : 'Loading Data Engine status…'}</p>
    <div className="toolbar"><button type="button" disabled={busy} onClick={inspect}>Inspect coverage</button>
      <button type="button" className="primary" disabled={busy || confirm} onClick={() => setConfirm(true)}>Request synchronization</button></div>
    {confirm && <div role="group" aria-label="Confirm synchronization"><p>Queue a synchronization for the local Data Engine?</p>
      <button type="button" onClick={refresh}>Confirm request</button> <button type="button" onClick={() => setConfirm(false)}>Cancel</button></div>}
    {message && <p role="status">{message}</p>}
    {status?.steps.length ? <details><summary>Ingestion job details</summary><ul>{status.steps.map(step => <li key={step.name}>{step.name} · {step.state} · {step.detail}</li>)}</ul></details> : null}
    <details><summary>Recent job history</summary>
      {jobs.length ? <div className="table-wrap"><table><thead><tr><th>Job</th><th>Completed</th><th>State</th><th>Details</th></tr></thead>
        <tbody>{[...jobs].reverse().map((job,index) => <tr key={`${job.started_at}-${job.name}-${index}`}><th scope="row">{job.name.replaceAll('_',' ')}</th>
          <td>{job.finished_at ? new Date(job.finished_at).toLocaleString('en-US') : '—'}</td><td>{job.state}</td><td className="screener-evidence">{job.detail}</td></tr>)}</tbody>
      </table></div> : <p>Job history appears as the current Data Engine publishes completed attempts.</p>}
    </details>
    {metrics && <details open><summary>Financial metric coverage</summary>
      <p>{financialDate} · {sample.business_models.all.eligible} eligible securities from the published warehouse</p>
      <div className="table-wrap"><table><thead><tr><th>Measure</th><th>Available</th><th>Structural</th><th>Input gaps</th><th>Main reason</th></tr></thead>
        <tbody>{Object.entries(auditedMeasures).map(([key, name]) => {
          const item = metrics[key]
          return item ? <tr key={key}><th scope="row">{name}</th><td>{item.available}</td>
            <td>{item.structurally_unavailable_at_cutoff}</td><td>{item.unresolved_or_conditionally_recoverable}</td>
            <td>{item.main_missing_reason ?? '—'}</td></tr> : null
        })}</tbody></table></div>
      <label>Affected securities <select value={selectedMetric} onChange={event => setSelectedMetric(event.target.value as keyof typeof auditedMeasures)}>
        {Object.entries(auditedMeasures).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
      </select></label>
      {affected && <p>{affected.source} · {affected.affected_securities.map(item => `${item.ticker} (${item.reason})`).join(', ') || 'No missing observations.'}</p>}
    </details>}
    {quality && <details open><summary>Security coverage</summary><p>{quality.scope}</p>
      <div className="table-wrap"><table><thead><tr><th>Ticker</th><th>State</th><th>Missing sessions</th></tr></thead>
        <tbody>{quality.rows.map(row => <tr key={row.ticker}><th scope="row">{row.ticker}</th><td>{row.status}</td><td>{row.missing_sessions?.length ?? '—'}</td></tr>)}</tbody>
      </table></div></details>}
  </section>
}
