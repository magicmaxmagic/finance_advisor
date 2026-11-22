import { useEffect, useState } from 'react'
import { metricName, pct } from './format'
import { FinancialChart } from './DeferredChart'

type Statistic = { mean: number | null; median: number | null; confidence_95: number[] | null; observations: number; effective_n: number | null; standard_deviation: number | null }
type Signature = { regime?: Record<string, Statistic>; ic: Statistic; rank_stability: Statistic | null; portfolio: Record<string, number | null> | null; quintiles?: Statistic[]; holm_p_value?: number | null; monotonicity?: Statistic; coverage?: { eligible: number; available: number; matched: number } }
type Legacy = { average_ic: number | null; ic_count: number; folds: number; portfolio_metrics: Record<string, number | null> | null }
type Evidence = { status: string; label: string; reason: string }
type Series = { windows: { date:string; ic:number|null; turnover:number|null; available:number|null; matched:number|null }[]
  portfolio: { date:string; wealth:number; benchmark:number; drawdown:number; benchmark_drawdown:number }[]; reason?:string|null }
type Result = { headline?: Evidence; model_evidence?: Record<string,Record<string,Evidence>>; universe_count?:number|null
  params?: { cost_bps:number }; version?: string; model_version?: string; start?: string; end?: string; reason?: string; warehouse_epoch?: string; limitations?: string[]; summary?: Record<string, { models: Record<string, Signature>; factors: Record<string, Signature> } | Record<string, Legacy>> }

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
  const [history, setHistory] = useState<{key:string; data?:Series; error?:string}>()
  const [show, setShow] = useState<'equity'|'drawdown'>('equity')
  const key = `${horizon}/${chosen}`
  const activeHistory = history?.key === key ? history : undefined
  const v3 = report?.version?.startsWith('signal-research-v3')
  useEffect(() => {
    if (!v3) return
    const controller = new AbortController()
    fetch(`/api/analytics/model-validation/series?horizon=${horizon}&series=${encodeURIComponent(chosen)}`,{signal:controller.signal})
      .then(response => { if (!response.ok) throw Error('Archived series is unavailable.'); return response.json() as Promise<Series> })
      .then(data => { if (!controller.signal.aborted) setHistory({key,data}) })
      .catch(reason => { if (!controller.signal.aborted) setHistory({key,error:String(reason)}) })
    return () => controller.abort()
  },[v3,horizon,chosen,key])
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
  const evidence = report.model_evidence?.[horizon]?.[chosen] ?? report.model_evidence?.[horizon]?.opportunity ?? report.headline
  const archive = activeHistory?.data
  const ic = archive?.windows.flatMap(window => window.ic == null ? [] : [{ date:window.date, close:window.ic }]) ?? []
  const bins = Array.from({length:10},(_,index) => ({
    from: -1+index*.2, to: -1+(index+1)*.2,
    count: ic.filter(point => point.close >= -1+index*.2 && (index === 9 || point.close < -1+(index+1)*.2)).length,
  }))
  const maximum = Math.max(1,...bins.map(bucket=>bucket.count))
  const fees = report.params?.cost_bps
  const turnover = archive?.windows.find(window=>window.turnover != null)?.turnover
  return <>
    <section className="panel validation-lead"><span className="eyebrow">HISTORICAL EVIDENCE</span><h2>Does the scoring model actually work?</h2>
      <strong className={`evidence-chip ${evidence?.status ?? 'insufficient'}`}>{evidence?.label ?? 'Insufficient evidence'}</strong>
      <p>{evidence?.reason ?? 'A dependence-aware study is unavailable for this selection.'}</p>
      <p>Research scores summarize relative valuation, business quality, market conditions, and resilience. Rank IC measures how those ranks related to later returns. The score is not a probability.</p>
      <p className="warning">Surviving-universe study. Historical membership, sector classifications, and delisting settlement coverage are limited.</p>
      {report.reason && <p>{report.reason}</p>}
      {report.warehouse_epoch && <small>{report.version ?? report.model_version} · {report.start} to {report.end}{report.universe_count != null && ` · ${report.universe_count} configured securities`}</small>}
      {fees != null && <p>Trading cost {fees} basis points per side{turnover != null && ` · ${pct(turnover)} two-sided turnover per completed window`}.</p>}
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
        <section className="panel"><div className="section-heading"><div><h2>Predictive evidence over time</h2><p>{selectedRank}</p></div>
          <label className="date-control">Research series<select value={chosen} onChange={event => setChosen(event.target.value)}>
            {Object.keys(current.models).map(name => <option value={name} key={name}>{titles[name] ?? metricName(name)}</option>)}
            <optgroup label="Factors">{Object.keys(current.factors).map(name => <option value={name} key={name}>{metricName(name)}</option>)}</optgroup>
          </select></label></div>
          {activeHistory?.error && <p role="alert">{activeHistory.error}</p>}
          {!activeHistory && <p role="status">Loading archived windows…</p>}
          {ic.length > 1 && <FinancialChart points={ic} label="Rank IC by decision date" showPriceTools={false} initialRange="MAX"/>}
          {archive && ic.length === 0 && <p>Rank IC is unavailable for this series.</p>}
          {ic.length > 0 && <div className="v4-context"><h3>Rank IC distribution</h3><p>{ic.length} usable decision windows. Near zero means little rank association.</p>
            <div className="v4-histogram" aria-label="IC histogram">{bins.map(bucket => <div key={bucket.from}
              title={`${bucket.from.toFixed(1)} to ${bucket.to.toFixed(1)}: ${bucket.count} windows`}>
              <span>{bucket.count}</span><progress max={maximum} value={bucket.count} aria-label={`IC bin ${bucket.from.toFixed(1)} to ${bucket.to.toFixed(1)}`}/>
              <small>{bucket.from.toFixed(1)}</small></div>)}</div></div>}
        </section>
        {archive?.portfolio.length ? <section className="panel"><h2>Portfolio and benchmark</h2>
          <p>Net archived model and SPY paths use the report’s trade costs and matching session windows.</p>
          <div className="segments"><button type="button" aria-pressed={show === 'equity'} onClick={() => setShow('equity')}>Equity</button>
            <button type="button" aria-pressed={show === 'drawdown'} onClick={() => setShow('drawdown')}>Drawdown</button></div>
          <FinancialChart points={archive.portfolio.map(point=>({date:point.date,close:show==='equity'?point.wealth:point.drawdown}))}
            benchmark={archive.portfolio.map(point=>({date:point.date,close:show==='equity'?point.benchmark:point.benchmark_drawdown}))}
            units={show==='drawdown'?'%':undefined} label={show==='equity'?'Equity indexed to 100':'Drawdown comparison'}
            comparisonRebased={false} initialOverlays={['SPY']} initialRange="MAX" showPriceTools={false}/>
        </section> : archive?.reason && <section className="panel"><h2>Portfolio path</h2><p>{archive.reason}</p></section>}
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
