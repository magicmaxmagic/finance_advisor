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
type EngineState = { updated_at:string|null; pipeline_state:string; steps:Job[]; recent_jobs?:Job[] }
type QualityState = { scope: string; rows: { ticker: string; status: string; missing_sessions?: string[] }[] }

export function DataCenterControls({ publication }: { publication?: string }) {
  const [status, setStatus] = useState<EngineState | null>(null)
  const [quality, setQuality] = useState<QualityState | null>(null)
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
      const responses = await Promise.all([fetch('/api/data/status'), fetch('/api/warehouse/quality')])
      if (!responses.every(response => response.ok)) throw new Error('Published quality details are unavailable.')
      const [engine,coverage] = await Promise.all(responses.map(response => response.json()))
      setStatus(engine); setQuality(coverage)
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
  const successful = [...jobs,...(status?.steps ?? [])].filter(step => step.state === 'done' && ['prices','price_corrections'].includes(step.name))
    .map(step=>step.finished_at).filter(Boolean).sort().at(-1)
  return <section className="panel"><h2>Synchronization health</h2>
    <div className="v4-context"><strong className={needsAttention ? 'warning' : ''}>{statusText}</strong>
      <p>{successful ? `Last successful price synchronization ${new Date(successful).toLocaleString('en-US')}.`
        : publication ? `Latest successful warehouse publication ${new Date(publication).toLocaleString('en-US')}.` : 'No successful job timestamp is available.'}</p>
      {needsAttention && <p>Open job details to see what prevented the most recent update. The last validated publication remains available.</p>}
    </div>
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
    {quality && <details open><summary>Security coverage</summary><p>{quality.scope}</p>
      <div className="table-wrap"><table><thead><tr><th>Ticker</th><th>State</th><th>Missing sessions</th></tr></thead>
        <tbody>{quality.rows.map(row => <tr key={row.ticker}><th scope="row">{row.ticker}</th><td>{row.status}</td><td>{row.missing_sessions?.length ?? '—'}</td></tr>)}</tbody>
      </table></div></details>}
  </section>
}
