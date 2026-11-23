import { useState } from 'react'
import { metricName, pct, scoreText } from './format'

export type DecisionContext = {
  version: string
  dimensions: Record<string, number | null>
  conditions: Record<string, { rule: string; observed: boolean | null; missing: string[]; inputs: Record<string, number | null>;
    watch: string; history: Record<string, { status: string; windows: number; effective_windows: number | null;
      interval: number[] | null; net_mean_difference: number | null; adjusted_p?: number | null;
      study_version: string | null; study_end: string | null }> }>
  horizons: Record<string, number>; holding_review: string; contradictions: string[]
}
const horizons = [['long', 'Long-term'], ['medium', 'Medium-term'], ['tactical', 'Tactical']] as const
const titles: Record<string, string> = { business_quality: 'Business quality', valuation: 'Valuation attractiveness', growth: 'Growth',
  cash_flow: 'Cash-flow quality', market_timing: 'Market timing', market_risk_resilience: 'Market risk resilience',
  financial_risk_resilience: 'Financial risk resilience' }
const states: Record<string, string> = { insufficient: 'Insufficient held-out evidence', inconclusive: 'Predictive evidence inconclusive',
  higher_returns: 'Higher historical returns in the evaluated sample', lower_returns: 'Lower historical returns in the evaluated sample' }

export function DecisionResearch({ data, quant }: { data: DecisionContext; quant: boolean }) {
  const [horizon, setHorizon] = useState<string>('long')
  const dimensions = Object.entries(data.dimensions)
  return <div className="v4-context"><h3>Investment decision research</h3>
    <label>Research horizon <select aria-label="Research horizon" value={horizon} onChange={event => setHorizon(event.target.value)}>
      {horizons.map(([key, title]) => <option key={key} value={key}>{title} · {data.horizons[key]} sessions</option>)}
    </select></label>
    <p>The horizon selects the comparison window for the tested conditions. The scores below summarize current evidence.</p>
    <dl className="publication">{dimensions.map(([key, score]) => <div key={key}>
      <dt>{titles[key] ?? metricName(key)}</dt><dd>{score == null ? 'Unavailable' : `${scoreText(score)}/100`}</dd>
    </div>)}</dl>
    {Object.entries(data.conditions).map(([key, item]) => {
      const history = item.history[horizon]
      const title = key === 'entry' ? 'Entry conditions' : 'Exit and thesis review'
      return <details key={key} className="v4-context" aria-label={title}><summary>{title} · {item.observed == null ? 'Evidence missing' : item.observed ? 'Condition present' : 'Condition absent'}</summary>
        <p>{item.rule}</p><p>{states[history.status]} · {history.windows} windows.</p>
        {history.net_mean_difference != null && <p>Flagged minus unflagged net forward return {pct(history.net_mean_difference)}
          {history.interval && ` · 95% interval ${history.interval.map(pct).join(' to ')}`}.</p>}
        {item.missing.length > 0 && <p>Missing {item.missing.map(metricName).join(', ')}.</p>}
        <p>{item.watch}</p>
        {quant && <p>{Object.entries(item.inputs).map(([name, value]) => `${metricName(name)} ${name === 'fundamental_score' ? scoreText(value) : pct(value)}`).join(' · ')}
          {history.study_version && ` · ${history.study_version} through ${history.study_end}`}
          {history.adjusted_p != null && ` · adjusted p ${history.adjusted_p.toFixed(3)}`}</p>}
      </details>
    })}
    <h4>Holding review</h4><p>{data.holding_review}</p>
    {data.contradictions.map(text => <p key={text}>{text}</p>)}
  </div>
}
