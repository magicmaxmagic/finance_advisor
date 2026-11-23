import { useEffect, useMemo, useState } from 'react'
import { FinancialChart } from './DeferredChart'
import type { Point } from './FinancialChart'
import { cash, pct } from './format'
import { type Currency, type Ledger, type Trade, loadLedger, parseTrades, serverLedger, serializeTrades, validateTrade } from './portfolioLedger'
import { PortfolioPanel as AdvancedCalculator } from './ResearchTools'

type Position = { security_id: string; quantity: number; market_value: number; cost_basis: number; unrealized_pnl: number; price_date: string; weight: number }
type Holdings = { market_value: number; unrealized_pnl: number; realized_pnl: number; holdings: Position[]
  base_currency: Currency; sector_allocation: Record<string,number>; currency_exposure: Record<string,number>
  risk?: { annualized_volatility?: number; fx_assumption?: string } }
type Analysis = { result: Holdings | { before: Holdings; after: Holdings; assumption: string }; assumptions: string[] }
type Draft = { ticker: string; side: 'buy'|'sell'; date: string; quantity: string; price: string; currency: Currency; fee: string; rate: string }
type Directory = { ticker: string; name: string }[]

const tickerName = (symbol: string) => symbol.split(':').at(-1) ?? symbol
const baseAmount = (h: Holdings) => h.holdings.reduce((total,item) => total+item.cost_basis,0)
const failure = (reason: unknown) => reason instanceof Error ? reason.message : String(reason)
const freshDraft = (ticker: string, date: string): Draft => ({ ticker, date, side:'buy', quantity:'', price:'', currency:'USD', fee:'', rate:'' })

async function analyze(ledger: Ledger, date: string, fx: string, options: { signal?: AbortSignal; scenario?: string; amount?: number } = {}) {
  const response = await fetch('/api/portfolio/analyze', {
    method:'POST', headers:{ 'Content-Type':'application/json' }, signal: options.signal,
    body: JSON.stringify({ csv_data:serializeTrades(ledger.trades,true), as_of:date, base_currency:ledger.base,
      foreign_fx:fx ? Number(fx) : undefined, scenario_security:options.scenario || undefined, scenario_amount: options.amount }),
  })
  const body = await response.json()
  if (!response.ok) throw Error(typeof body.detail === 'string' ? body.detail : 'Check the transaction dates, prices and exchange rates.')
  return body as Analysis
}

export function Portfolio({ companies, initialTicker, latest }: { companies: Directory; initialTicker: string; latest: string }) {
  const [ledger, setLedger] = useState<Ledger>({ base:'USD', trades:[] })
  const [revision, setRevision] = useState<number | null>(null)
  const [request, setRequest] = useState(0)
  const [loadError, setLoadError] = useState('')
  const [legacyBackup, setLegacyBackup] = useState<Ledger>()
  const [draft, setDraft] = useState<Draft>(() => freshDraft(initialTicker,latest))
  const [edit, setEdit] = useState<string | null>(null)
  const [remove, setRemove] = useState<Trade | null>(null)
  const [detail, setDetail] = useState<string | null>(null)
  const [asOf, setAsOf] = useState('')
  const [fx, setFx] = useState('')
  const [result, setResult] = useState<{ key: string; value?: Analysis; error?: string }>()
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [scenario, setScenario] = useState(initialTicker)
  const [amount, setAmount] = useState('')
  const [scenarioResult, setScenarioResult] = useState<Analysis>()
  const [history, setHistory] = useState<{ key:string; points:Point[]; unavailable:number }>()
  const [historyBusy, setHistoryBusy] = useState(false)
  const date = asOf || latest
  const trades = useMemo(() => ledger.trades.filter(trade => trade.date <= date),[ledger.trades,date])
  const key = JSON.stringify([ledger,date,fx])
  const valued = result?.key === key ? result : undefined
  const position = valued?.value?.result
  const portfolio = position && !('after' in position) ? position : undefined
  useEffect(() => {
    const controller = new AbortController()
    async function load() {
      let stored = await serverLedger(controller.signal)
      let legacy: Ledger | undefined
      try { legacy = loadLedger() }
      catch (reason) { if (!controller.signal.aborted) setMessage(`Browser backup needs attention. ${failure(reason)}`) }
      if (legacy && stored.revision === 0 && (legacy.trades.length || legacy.base !== 'USD')) {
        try {
          stored = await serverLedger(controller.signal, { expected_revision:0, ledger:legacy })
          if (!controller.signal.aborted) setMessage('Browser transactions copied to the local portfolio store.')
        } catch (reason) {
          if (controller.signal.aborted) return
          stored = await serverLedger(controller.signal)
          if (stored.revision === 0) throw reason
        }
      }
      if (!controller.signal.aborted) {
        setRevision(stored.revision); setLedger(stored.ledger)
        if (legacy?.trades.length && JSON.stringify(legacy) !== JSON.stringify(stored.ledger)) setLegacyBackup(legacy)
      }
    }
    load().catch(reason => { if (!controller.signal.aborted) setLoadError(failure(reason)) })
    return () => controller.abort()
  }, [request])

  useEffect(() => {
    if (!date || !trades.length) return
    const controller = new AbortController()
    analyze(ledger,date,fx,{ signal:controller.signal }).then(
      value => { if (!controller.signal.aborted) setResult({ key,value }) },
      reason => { if (!controller.signal.aborted) setResult({ key,error:failure(reason) }) },
    )
    return () => controller.abort()
  }, [key,date,trades.length,ledger,fx])

  function update(field: keyof Draft, value: string) { setDraft(current => ({ ...current, [field]:value })) }
  async function commit(next: Ledger, description: string) {
    if (revision === null) throw Error('Load the saved portfolio before making changes.')
    if (next.trades.some(trade => trade.side === 'sell')) {
      const cutoff = next.trades.reduce((max,trade) => trade.date > max ? trade.date : max, date)
      await analyze(next,cutoff,fx)
    }
    const stored = await serverLedger(undefined, { expected_revision:revision, ledger:next })
    setLedger(stored.ledger); setRevision(stored.revision); setMessage(description); setScenarioResult(undefined)
  }
  async function save(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true); setError(''); setMessage('')
    try {
      const typed = draft.ticker.trim().toUpperCase()
      const entered = companies.find(company => company.name.toUpperCase() === typed || company.ticker === typed)?.ticker ?? typed
      const existing = ledger.trades.find(trade => trade.id === edit)
      const trade: Trade = { id:edit ?? crypto.randomUUID(), ticker:entered,date:draft.date || date,side:draft.side,
        quantity:Number(draft.quantity),price:Number(draft.price),currency:draft.currency,fee:Number(draft.fee),
        rate:draft.currency === ledger.base ? 1 : Number(draft.rate) }
      validateTrade(trade,ledger.base)
      if (trade.date > latest) throw Error('Select a transaction date within the published price history.')
      if (existing && tickerName(existing.ticker) === entered) trade.ticker = existing.ticker
      else {
        const identity = await fetch(`/api/warehouse/assets/${encodeURIComponent(entered)}?as_of=${trade.date}`)
        if (!identity.ok) throw Error('The published company directory is unavailable.')
        const match = await identity.json() as { available: boolean; company?: { security_id: string; currency: string | null } }
        if (!match.available || !match.company) throw Error('This ticker was not found in the local company directory.')
        if (match.company.currency && match.company.currency !== trade.currency) throw Error(`The published quote uses ${match.company.currency}. Confirm the trade currency.`)
        trade.ticker = match.company.security_id
      }
      await commit({ ...ledger,trades:edit ? ledger.trades.map(item => item.id === edit ? trade : item) : [...ledger.trades,trade] },
        edit ? 'Transaction updated.' : 'Transaction saved.')
      setEdit(null); setDraft(freshDraft(initialTicker,date))
    } catch (reason) { setError(failure(reason)) }
    finally { setBusy(false) }
  }

  async function importCsv(file: File) {
    setBusy(true); setError(''); setMessage('')
    try {
      if (file.size > 1_000_000) throw Error('CSV must be smaller than 1 MB.')
      const incoming = parseTrades(await file.text(),ledger.base)
      if (incoming.some(trade => trade.date > latest)) throw Error('Imported dates must be within the published price history.')
      await commit({ ...ledger,trades:[...ledger.trades,...incoming] },`${incoming.length} transactions imported.`)
    } catch (reason) { setError(failure(reason)) }
    finally { setBusy(false) }
  }
  async function deleteTrade() {
    if (!remove) return
    setBusy(true); setError('')
    try {
      await commit({ ...ledger,trades:ledger.trades.filter(trade => trade.id !== remove.id) },'Transaction removed.')
      if (edit === remove.id) { setEdit(null); setDraft(freshDraft(initialTicker,date)) }
      setRemove(null)
    } catch (reason) { setError(failure(reason)) }
    finally { setBusy(false) }
  }
  async function loadHistory() {
    setHistoryBusy(true)
    try {
      const dates = Array.from({length:6},(_,index) => {
        const time = new Date(date+'T00:00:00Z')
        return new Date(Date.UTC(time.getUTCFullYear(),time.getUTCMonth()-index,0)).toISOString().slice(0,10)
      }).reverse().filter(day => trades.some(trade => trade.date <= day))
      const outcomes = await Promise.all(dates.map(async day => {
        try {
          const response = await analyze(ledger,day,'')
          return 'after' in response.result ? null : { date:day,close:response.result.market_value }
        } catch { return null }
      }))
      const points = outcomes.filter((value): value is Point => value != null)
      setHistory({ key,points,unavailable:outcomes.length-points.length })
    } finally { setHistoryBusy(false) }
  }
  async function simulate(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setScenarioResult(undefined)
    try { setScenarioResult(await analyze(ledger,date,fx,{ scenario,amount:Number(amount) })) }
    catch (reason) { setError(failure(reason)) }
    finally { setBusy(false) }
  }

  return <>
    <section className="panel">
      <div className="section-heading"><div><h2>Portfolio overview</h2><p>Transactions are stored in a separate database on this device. Valuation uses the local published warehouse.</p></div>
        <label className="date-control">Valuation date<input type="date" max={latest} value={date} onChange={event => {
          setAsOf(event.target.value); setScenarioResult(undefined)
        }} /></label></div>
      {revision === null && (loadError ? <p role="alert">{loadError} <button type="button" onClick={() => { setLoadError(''); setRequest(request + 1) }}>Reload portfolio</button></p> : <p role="status">Loading the local portfolio…</p>)}
      <div className="toolbar"><label>Portfolio currency<select value={ledger.base} disabled={revision === null || ledger.trades.length > 0}
        onChange={event => {
          const next = { ...ledger,base:event.target.value as Currency }
          void commit(next,'Portfolio currency saved.').catch(reason => setError(failure(reason)))
        }}>
        <option>USD</option><option>CAD</option></select></label>
        {trades.some(trade => trade.currency !== ledger.base) && <label>Valuation FX (portfolio currency per foreign unit)
          <input value={fx} type="number" min="0.000001" step="any" onChange={event => { setFx(event.target.value); setScenarioResult(undefined) }}/></label>}
      </div>
      {revision !== null && !trades.length && <p>No transactions at this valuation date. Record a buy below to get started.</p>}
      {!!trades.length && !valued && <p role="status">Valuing positions against published closes…</p>}
      {valued?.error && <p role="alert" className="warning">Valuation unavailable. {valued.error}</p>}
      {portfolio && <div className="status-grid portfolio-totals">
        {([['Market value',portfolio.market_value],['Open-position cost basis',baseAmount(portfolio)],['Unrealized P/L',portfolio.unrealized_pnl],['Realized P/L',portfolio.realized_pnl]] as const)
          .map(([label,value]) => <article key={label}><span>{label}</span><strong>{cash(value,ledger.base)}</strong></article>)}
      </div>}
    </section>
    {portfolio && <section className="panel">
      <h2>Positions and risk</h2>
      <p>Annualized volatility {pct(portfolio.risk?.annualized_volatility)} · Largest position {portfolio.holdings.length ? pct(Math.max(...portfolio.holdings.map(item => item.weight))) : '—'}</p>
      {portfolio.risk?.fx_assumption && <p>{portfolio.risk.fx_assumption}.</p>}
      <div className="table-wrap"><table><thead><tr><th>Position</th><th>Quantity</th><th>Market value</th><th>Weight</th></tr></thead>
        <tbody>{portfolio.holdings.map(item => <tr key={item.security_id}><th scope="row"><button type="button"
          onClick={() => setDetail(detail === item.security_id ? null : item.security_id)}>{tickerName(item.security_id)}</button></th>
          <td>{item.quantity.toLocaleString()}</td><td>{cash(item.market_value,ledger.base)}</td><td>{pct(item.weight)}</td></tr>)}</tbody></table></div>
      {portfolio.holdings.filter(item => detail === item.security_id).map(item => <p key={item.security_id}>
        {tickerName(item.security_id)} · Quote {item.price_date} · Cost basis {cash(item.cost_basis,ledger.base)} · Unrealized {cash(item.unrealized_pnl,ledger.base)}
      </p>)}
      <div className="overview-bottom allocation">{([['Sector allocation',portfolio.sector_allocation],['Currency exposure',portfolio.currency_exposure]] as const).map(([label,values]) =>
        <div key={label}><h3>{label}</h3>{Object.entries(values).map(([name,value]) => <p key={name}>{name || 'Unavailable'} · {pct(value)}</p>)}</div>)}</div>
      <details><summary>Valuation assumptions</summary>{valued?.value?.assumptions.map(line => <p key={line}>{line}</p>)}</details>
      <section className="v4-context"><h3>Historical holdings value</h3><p>Month-end values reflect recorded transactions and cash flows. Prices or FX can leave gaps.</p>
        {trades.some(trade => trade.currency !== ledger.base) ? <p>Historical FX is unavailable for mixed-currency holdings.</p>
          : <button type="button" disabled={historyBusy} onClick={() => void loadHistory()}>{historyBusy ? 'Loading history…' : 'Load valuation history'}</button>}
        {history?.key === key && (history.points.length >= 2 ? <FinancialChart points={history.points} label="Historical holdings value"
          currency={ledger.base} showPriceTools={false} initialRange="MAX"/> : <p>Fewer than two historical valuations are available.</p>)}
        {history?.key === key && history.unavailable > 0 && <p>{history.unavailable} month-end valuations could not be computed.</p>}
      </section>
    </section>}
    <section className="panel" aria-label="Transaction editor">
      <h2>{edit ? 'Edit transaction' : 'Record a transaction'}</h2>
      <p>Add a buy to start a position or record a sale against your documented holdings.</p>
      <form className="toolbar" onSubmit={save}>
        <label>Company name or ticker<input list="portfolio-companies" value={draft.ticker} onChange={event => update('ticker',event.target.value)} required /></label>
        <datalist id="portfolio-companies">{companies.map(company => <option key={company.ticker} value={company.ticker}>{company.name}</option>)}</datalist>
        <label>Transaction type<select value={draft.side} onChange={event => update('side',event.target.value)}><option value="buy">Buy</option><option value="sell">Sell</option></select></label>
        <label>Transaction date<input type="date" max={latest} value={draft.date || date} onChange={event => update('date',event.target.value)} required/></label>
        <label>Quantity<input type="number" min="0.000001" step="any" value={draft.quantity} onChange={event => update('quantity',event.target.value)} required/></label>
        <label>Price per share<input type="number" min="0.000001" step="any" value={draft.price} onChange={event => update('price',event.target.value)} required/></label>
        <label>Trade currency<select value={draft.currency} onChange={event => update('currency',event.target.value)}><option>USD</option><option>CAD</option></select></label>
        <label>Fees (optional)<input type="number" min="0" step="any" value={draft.fee} onChange={event => update('fee',event.target.value)}/></label>
        {draft.currency !== ledger.base && <label>Trade FX (portfolio currency per trade unit)<input type="number" min="0.000001" step="any" required
          value={draft.rate} onChange={event => update('rate',event.target.value)}/></label>}
        <button className="primary" disabled={revision === null || busy || !latest} type="submit">{busy ? 'Working…' : 'Save transaction'}</button>
        {edit && <button type="button" onClick={() => { setEdit(null); setDraft(freshDraft(initialTicker,date)) }}>Cancel edit</button>}
      </form>
      {error && <p role="alert" className="error">{error}</p>}
      {message && <p role="status" className="positive">{message}</p>}
    </section>
    {!!ledger.trades.length && <section className="panel">
      <h2>Transaction history</h2>
      <div className="table-wrap"><table><thead><tr><th>Ticker</th><th>Date</th><th>Type</th><th>Quantity</th><th>Price</th><th>Actions</th></tr></thead>
        <tbody>{ledger.trades.map(trade => <tr key={trade.id}><th scope="row">{tickerName(trade.ticker)}</th><td>{trade.date}</td><td>{trade.side}</td>
        <td>{trade.quantity}</td><td>{cash(trade.price,trade.currency)}</td>
        <td><button type="button" disabled={busy} aria-label={`Edit ${tickerName(trade.ticker)} transaction`} onClick={() => {
          setEdit(trade.id); setDraft({...trade,ticker:tickerName(trade.ticker),quantity:String(trade.quantity),price:String(trade.price),fee:String(trade.fee),rate:String(trade.rate)})
          document.querySelector('[aria-label="Transaction editor"]')?.scrollIntoView()
        }}>Edit</button> <button type="button" disabled={busy} aria-label={`Remove ${tickerName(trade.ticker)} transaction`}
          onClick={() => setRemove(trade)}>Remove</button></td></tr>)}</tbody></table></div>
      {remove && <div role="group" aria-label="Confirm transaction removal" className="v4-context"><p>Remove the {tickerName(remove.ticker)} {remove.side} dated {remove.date}?</p>
        <button type="button" disabled={busy} onClick={() => void deleteTrade()}>Confirm removal</button>
        {' '}<button type="button" onClick={() => setRemove(null)}>Cancel</button></div>}
    </section>}
    {portfolio && <section className="panel"><h2>Evaluate portfolio impact</h2>
      <p>Compare the current value and concentration with a hypothetical addition at the published close.</p>
      <form className="toolbar" onSubmit={simulate}>
        <label>Scenario ticker<input list="portfolio-companies" value={scenario} onChange={event => setScenario(event.target.value.toUpperCase())} required /></label>
        <label>Additional investment ({ledger.base})<input type="number" min="0.01" step="any" value={amount} onChange={event => setAmount(event.target.value)} required /></label>
        <button type="submit" disabled={busy || !portfolio}>Evaluate addition</button>
      </form>
      {scenarioResult && 'after' in scenarioResult.result && <p>Before {cash(scenarioResult.result.before.market_value,ledger.base)}
        {' '}· After {cash(scenarioResult.result.after.market_value,ledger.base)}. {scenarioResult.result.assumption}</p>}
    </section>}
    <details className="panel"><summary>CSV import and advanced calculator</summary>
      <p>Columns: date, ticker, side, quantity, price, currency, fees, exchange_rate. Append an explicit rate for foreign-currency trades.</p>
      <label className="field">Import transaction CSV<input type="file" accept=".csv,text/csv" disabled={revision === null || busy || !latest}
        onChange={event => { const file = event.target.files?.[0]; if (file) void importCsv(file); event.target.value='' }} /></label>
      {ledger.trades.length > 0 && <details><summary>Export ledger CSV</summary>
        <a download="finlab-portfolio.csv" href={`data:text/csv;charset=utf-8,${encodeURIComponent(serializeTrades(ledger.trades))}`}>Download portfolio CSV</a>
        <textarea readOnly value={serializeTrades(ledger.trades)} rows={5} /></details>}
      {legacyBackup && <details><summary>Browser backup</summary><p>Some browser records differ from the saved portfolio. Review them before importing.</p>
        <textarea readOnly value={serializeTrades(legacyBackup.trades)} rows={5} /></details>}
      <details><summary>Open legacy CSV calculator</summary><AdvancedCalculator /></details>
    </details>
  </>
}
