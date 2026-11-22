import { useState } from 'react'

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const result = await response.json()
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail))
  return result as T
}

type StrategyResult = { metrics: Record<string, number | null>; equity: Array<{ date: string; value: number }>; caveats: string[] }

export function StrategyLab() {
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [tickers, setTickers] = useState('SPY')
  const [strategy, setStrategy] = useState('equal_weight')
  const [result, setResult] = useState<StrategyResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function run() {
    setBusy(true); setError(''); setResult(null)
    try { setResult(await post<StrategyResult>('/api/analytics/backtest', { start, end, tickers: tickers.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean), strategy })) }
    catch (reason) { setError(String(reason)) }
    finally { setBusy(false) }
  }
  const values = result?.equity.map((point) => point.value) ?? []
  const low = values.length ? Math.min(...values) : 0
  const high = values.length ? Math.max(...values) : 1
  const points = values.map((value, index) => `${index * 620 / Math.max(values.length - 1, 1)},${150 - (value - low) * 140 / Math.max(high - low, 1)}`).join(' ')
  return <section className="panel"><h2>Strategy Lab</h2><p>Simulation historique locale · signaux exécutés à l’ouverture suivante · 5 pb de frais et 5 pb de slippage par côté.</p>
    <div className="toolbar"><label>Début<input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></label><label>Fin<input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></label><label>Titres séparés par virgules<input value={tickers} onChange={(e) => setTickers(e.target.value)} /></label><label>Stratégie<select value={strategy} onChange={(e) => setStrategy(e.target.value)}><option value="equal_weight">Pondération égale mensuelle</option><option value="buy_hold">Achat et conservation</option><option value="momentum">Momentum 12 moins 1</option><option value="value">Valeur</option><option value="quality">Qualité</option><option value="multifactor">Multifacteur</option></select></label><button disabled={busy || !start || !end} onClick={run}>{busy ? 'Calcul…' : 'Simuler'}</button></div>
    {error && <p className="error">{error}</p>}{result && <><svg viewBox="0 0 620 160" role="img" aria-label="Valeur historique du portefeuille"><polyline points={points} fill="none" stroke="#52d7b4" strokeWidth="2" /></svg><p>{result.equity[0]?.date} → {result.equity.at(-1)?.date}</p><table><tbody>{Object.entries(result.metrics).map(([key, value]) => <tr key={key}><td>{key}</td><td>{value == null ? '—' : value.toFixed(4)}</td></tr>)}</tbody></table>{result.caveats.map((text) => <p key={text}>{text}</p>)}</>}
  </section>
}

export function PortfolioPanel() {
  const [csv, setCsv] = useState(() => localStorage.getItem('finlab-portfolio-csv') ?? 'date,security_id,side,quantity,price,currency,fx_to_base,fee\n')
  const [asOf, setAsOf] = useState('')
  const [base, setBase] = useState('USD')
  const [fx, setFx] = useState('')
  const [scenario, setScenario] = useState('')
  const [amount, setAmount] = useState('')
  const [result, setResult] = useState<unknown>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  function updateCsv(text: string) { setCsv(text); localStorage.setItem('finlab-portfolio-csv', text) }
  async function run() {
    setBusy(true); setError(''); setResult(null)
    try { setResult(await post('/api/portfolio/analyze', { csv_data: csv, as_of: asOf, base_currency: base, foreign_fx: fx ? Number(fx) : undefined, scenario_security: scenario || undefined, scenario_amount: scenario ? Number(amount) : undefined })) }
    catch (reason) { setError(String(reason)) }
    finally { setBusy(false) }
  }
  return <section className="panel"><h2>Portfolio Intelligence</h2><p>Saisissez les transactions buy/sell ou importez un CSV. security_id accepte un identifiant stable ou un ticker unique. fx_to_base est le taux à la date de chaque transaction. Le CSV est conservé dans ce navigateur.</p>
    <input type="file" accept=".csv,text/csv" onChange={async (e) => { const file = e.target.files?.[0]; if (file) updateCsv(await file.text()) }} />
    <textarea aria-label="Transactions CSV" rows={10} value={csv} onChange={(e) => updateCsv(e.target.value)} style={{ width: '100%' }} />
    <div className="toolbar"><label>Date de valorisation<input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} /></label><label>Devise de base<select value={base} onChange={(e) => setBase(e.target.value)}><option>USD</option><option>CAD</option></select></label><label>FX de valorisation étrangère vers base<input type="number" min="0" step="any" value={fx} onChange={(e) => setFx(e.target.value)} /></label><label>Scénario : ticker à ajouter<input value={scenario} onChange={(e) => setScenario(e.target.value)} /></label><label>Apport dans la devise de base<input type="number" min="0" value={amount} onChange={(e) => setAmount(e.target.value)} /></label><button disabled={busy || !asOf} onClick={run}>{busy ? 'Calcul…' : 'Analyser'}</button></div>
    {error && <p className="error">{error}</p>}{result != null && <pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(result, null, 2)}</pre>}
  </section>
}


type EngineState = { updated_at: string | null; pipeline_state: string; steps: Array<{ name: string; state: string; detail: string }> }
type QualityState = { scope: string; rows: Array<{ ticker: string; status: string; missing_sessions?: string[] }> }

export function DataCenterControls() {
  const [status, setStatus] = useState<EngineState | null>(null)
  const [quality, setQuality] = useState<QualityState | null>(null)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  async function inspect() {
    setBusy(true)
    try {
      const responses = await Promise.all([fetch('/api/data/status'), fetch('/api/warehouse/quality')])
      const payloads = await Promise.all(responses.map((response) => response.json()))
      if (!responses.every((response) => response.ok)) throw new Error('Publication locale indisponible.')
      setStatus(payloads[0]); setQuality(payloads[1])
    } catch (reason) { setMessage(String(reason)) }
    finally { setBusy(false) }
  }
  async function refresh() {
    setBusy(true)
    try { const response = await post<{ message: string }>('/api/data/refresh', {}); setMessage(response.message) }
    catch (reason) { setMessage(String(reason)) }
    finally { setBusy(false) }
  }
  return <section className="panel"><h3>Synchronisation et qualité</h3><button disabled={busy} onClick={inspect}>Vérifier l’état local</button> <button disabled={busy} onClick={refresh}>Demander une synchronisation</button>
    <p>{message}</p>{status && <><p>{status.pipeline_state} · {status.updated_at ?? 'Aucun statut publié'}</p>{status.steps.map((step) => <p key={step.name}>{step.name}: {step.state} · {step.detail}</p>)}</>}
    {quality && <><p>{quality.scope}</p><table><thead><tr><th>Titre</th><th>État</th><th>Séances manquantes</th></tr></thead><tbody>{quality.rows.map((row) => <tr key={row.ticker}><td>{row.ticker}</td><td>{row.status}</td><td>{row.missing_sessions?.length ?? '—'}</td></tr>)}</tbody></table></>}
  </section>
}
