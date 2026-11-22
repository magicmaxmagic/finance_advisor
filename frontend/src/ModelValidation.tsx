import { useEffect, useState } from 'react'
import { metricName, pct } from './format'

type Statistic = { mean: number | null; median: number | null; confidence_95: number[] | null; observations: number; effective_n: number | null; standard_deviation: number | null }
type Signature = { regime?: Record<string, Statistic>; ic: Statistic; rank_stability: Statistic | null; portfolio: Record<string, number | null> | null; quintiles?: Statistic[]; holm_p_value?: number | null; monotonicity?: Statistic; coverage?: { eligible: number; available: number; matched: number } }
type Legacy = { average_ic: number | null; ic_count: number; folds: number; portfolio_metrics: Record<string, number | null> | null }
type Result = { version?: string; model_version?: string; start?: string; end?: string; reason?: string; warehouse_epoch?: string; limitations?: string[]; summary?: Record<string, { models: Record<string, Signature>; factors: Record<string, Signature> } | Record<string, Legacy>> }

const value = (n: number | undefined | null) => n == null ? '—' : n.toFixed(3)
const titles: Record<string, string> = {
  opportunity: 'Opportunity · 60/25/15', fundamental: 'Fundamental only', technical: 'Technical only', risk: 'Resilience only',
  legacy: 'Legacy five-factor', five_factor: 'Legacy five-factor', equal_weight: 'Equal-weight universe', spy: 'SPY',
  risk_constraint: 'Risk as a constraint', entry_filter: 'Fundamental + entry filter', adaptive_oos: 'Walk-forward selection',
  fundamental_technical: 'Fundamental + technical', fundamental_risk: 'Fundamental + risk',
  fundamental_heavy: 'Fundamental heavy', technical_heavy: 'Technical heavy',
  technical_risk: 'Technical + risk', equal_dimensions: 'Equal dimension weights',
}

export function ModelValidation() {
  const [report, setReport] = useState<Result | null>(null)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const [horizon, setHorizon] = useState('21')
  const [chosen, setChosen] = useState('opportunity')
  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/analytics/model-validation', { signal: controller.signal }).then(response => {
      if (!response.ok) throw new Error('Could not read the archived validation report.')
      return response.json() as Promise<Result>
    }).then(setReport).catch(reason => { if (!controller.signal.aborted) setError(String(reason)) })
    return () => controller.abort()
  }, [retry])
  if (error) return <section className="panel empty" role="alert"><h2>Validation unavailable</h2><p>{error}</p><button type="button" onClick={() => { setError(''); setRetry(retry + 1) }}>Try again</button></section>
  if (!report) return <section className="panel pending" role="status">Loading archived validation…</section>
  const section = report.summary?.[horizon]
  const isV3 = report.version?.startsWith('signal-research-v3')
  const current = isV3 ? section as { models: Record<string, Signature>; factors: Record<string, Signature> } | undefined : undefined
  const legacy = !isV3 ? section as Record<string,Legacy> | undefined : undefined
  const order = ['opportunity', 'fundamental', 'legacy', 'five_factor', 'spy', 'equal_weight', 'adaptive_oos']
  const priority = (name: string) => order.includes(name) ? order.indexOf(name) : order.length
  const rows = current ? Object.entries(current.models).sort(([a],[b]) => priority(a) - priority(b)) : Object.entries(legacy ?? {}).map(([key,row]) => [key, {
    ic: { mean: row.average_ic, observations: row.ic_count, confidence_95: null },
    portfolio: row.portfolio_metrics,
  }] as [string, Signature])
  const selected = current?.models[chosen] ?? current?.factors[chosen]
  const selectedRank = current?.models[chosen] ? titles[chosen] ?? metricName(chosen) : metricName(chosen)
  return <>
    <section className="panel validation-lead"><span className="eyebrow">HISTORICAL EVIDENCE</span><h2>Predictive evidence inconclusive.</h2>
      <p>Research scores summarize relative valuation, business quality, market conditions, and resilience. Rank IC measures how those ranks related to later returns. The score is not a probability.</p>
      <p className="warning">Surviving-universe study. Historical membership, sector classifications, and delisting settlement coverage are limited.</p>
      {report.reason && <p>{report.reason}</p>}
      {report.warehouse_epoch && <small>{report.version ?? report.model_version} · {report.start} to {report.end}</small>}
    </section>
    {section && <>
      <section className="panel">
        <div className="section-heading"><div><h2>Model comparison</h2><p>Higher rank IC means stronger alignment between ranks and later returns.</p></div>
          <label className="date-control">Forward horizon<select value={horizon} onChange={event => setHorizon(event.target.value)}>
            <option value="21">1 month · 21 sessions</option><option value="63">3 months · 63 sessions</option>
            <option value="126">6 months · 126 sessions</option><option value="252">12 months · 252 sessions</option>
          </select></label>
        </div>
        <div className="table-wrap" tabIndex={0} aria-label="Model results"><table><thead><tr><th>Model</th><th>Rank IC</th><th>95% interval</th><th>Windows</th><th>Sharpe</th><th>Drawdown</th></tr></thead>
          <tbody>{rows.map(([key,row]) => <tr key={key}><th scope="row">
            {isV3 ? <button className="text-button" type="button" onClick={() => setChosen(key)}>{titles[key] ?? metricName(key)}</button> : titles[key] ?? metricName(key)}</th>
            <td>{value(row.ic.mean)}</td><td>{row.ic.confidence_95?.map(value).join(' to ') ?? '—'}</td><td>{row.ic.observations}</td>
            <td>{value(row.portfolio?.sharpe)}</td><td>{pct(row.portfolio?.max_drawdown)}</td></tr>)}</tbody></table></div>
        {!isV3 && <p>The archived V2 study does not estimate uncertainty. Run Validation V3 to see confidence intervals and factor attribution.</p>}
      </section>
      {current && <>
        <section className="panel"><h2>Score bucket outcomes</h2><p>{selectedRank} · Average net forward return, ordered from lowest to highest rank.</p>
          {selected?.quintiles ? <div className="buckets">{selected.quintiles.map((bucket, index) => <div key={index}>
            <span>Q{index + 1}</span><strong>{pct(bucket.mean)}</strong><small>{bucket.observations} windows</small>
          </div>)}</div> : <p>Quintiles unavailable for this portfolio or horizon.</p>}
          <p>Rank stability {value(selected?.rank_stability?.mean)} · Effective IC sample {value(selected?.ic.effective_n)} windows</p>
          {selected?.monotonicity?.mean != null && <p>Nondecreasing adjacent bucket steps {pct(selected.monotonicity.mean)}.</p>}
          {selected?.regime && <details><summary>Market regime results</summary>
            <div className="table-wrap"><table><thead><tr><th>Prior SPY trend</th><th>Mean rank IC</th><th>Windows</th><th>95% interval</th></tr></thead>
              <tbody>{Object.entries(selected.regime).map(([regime,outcome]) => <tr key={regime}>
                <th scope="row">{metricName(regime)}</th><td>{value(outcome.mean)}</td><td>{outcome.observations}</td><td>{outcome.confidence_95?.map(value).join(' to ') ?? '—'}</td>
              </tr>)}</tbody></table></div></details>}
        </section>
        <section className="panel"><h2>Individual factors</h2>
          <div className="table-wrap" tabIndex={0} aria-label="Factor results"><table><thead><tr><th>Factor</th><th>Mean IC</th><th>Median IC</th><th>95% interval</th><th>Holm p</th><th>Coverage</th></tr></thead>
            <tbody>{Object.entries(current.factors).map(([key,row]) => <tr key={key}>
              <th scope="row"><button className="text-button" type="button" onClick={() => setChosen(key)}>{metricName(key)}</button></th>
              <td>{value(row.ic.mean)}</td><td>{value(row.ic.median)}</td><td>{row.ic.confidence_95?.map(value).join(' to ') ?? '—'}</td>
              <td>{value(row.holm_p_value)}</td><td>{row.coverage ? `${row.coverage.available} / ${row.coverage.eligible}` : '—'}</td>
            </tr>)}</tbody></table></div>
        </section>
      </>}
    </>}
    {report.limitations && <details className="panel"><summary>Methodology and limitations</summary><ul>{report.limitations.map(item => <li key={item}>{item}</li>)}</ul>
      <p className="epoch-value">{report.warehouse_epoch}</p></details>}
  </>
}
