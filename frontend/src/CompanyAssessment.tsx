import { useState } from 'react'

type Contribution = {
  metric: string
  group: string
  raw: number
  score: number
  cohort_median: number
  cohort_count: number
  direction: string
  weight: number
  points_from_neutral: number
}

type Group = {
  score: number | null
  coverage: number
  available: string[]
  missing: string[]
  optional_metrics: string[]
  reason: string | null
  slots: { required_one_of: string[]; selected: Contribution | null }[]
}

type Comparison = {
  historical_count: number
  historical_median: number | null
  historical_percentile: number | null
  sector_status: string
  valid_peer_count: number
  history: { as_of: string; value: number }[]
}

export type Assessment = {
  ticker: string
  as_of: string
  source_date: string | null
  published_at: string
  warehouse_epoch: string
  comparisons: Record<string, Comparison>
  fundamental_factors: {
    annual_period: string | null
    factors: Record<string, { value: number | null; status: string; definition: string }>
    filing_evidence: { canonical: string; source_tag: string; period_end: string; filed_date: string; accession_number: string; value: number }[]
  }
  market_factors: { factor: string; value: number | null; data_status: string; definition: string; source_date: string | null; source: string | null }[]
  assessment: {
    model_version: string
    opportunity_score: number | null
    fundamental_score: number | null
    technical_score: number | null
    risk_resilience_score: number | null
    breakdown: Record<string, Record<string, Group>>
    weights: Record<string, number>
    contributions: Contribution[]
    positive_contributors: Contribution[]
    negative_contributors: Contribution[]
    contradictions: string[]
    normalization: string
    weight_status: string
    price_history_status: string
    eligibility_status: string
    options_context: { reason: string }
  }
}

const groups: Record<string, string[]> = {
  Valuation: ['trailing_pe', 'ttm_pe', 'earnings_yield', 'ttm_earnings_yield', 'ev_to_operating_ebitda', 'ev_to_ebit', 'ev_to_revenue', 'price_to_book', 'price_to_sales', 'price_to_fcf', 'fcf_yield'],
  Profitability: ['roa', 'roe', 'roic', 'gross_margin', 'operating_margin', 'net_margin', 'ebitda_margin'],
  Growth: ['revenue_growth', 'earnings_growth', 'fcf_growth', 'growth_acceleration', 'margin_change', 'earnings_variability'],
  'Financial health': ['equity_to_assets', 'debt_to_assets', 'reported_debt_to_assets', 'net_debt_to_ebitda', 'debt_to_equity', 'current_ratio', 'cash_to_debt', 'interest_coverage'],
  'Cash flow': ['cash_flow_quality', 'fcf_margin'],
}

const names: Record<string, string> = {
  trailing_pe: 'Fiscal-year P/E', ttm_pe: 'Trailing 12-month P/E', roa: 'Return on assets',
  roe: 'Return on equity', roic: 'Return on invested capital', ev_to_operating_ebitda: 'EV / EBITDA',
  fcf_margin: 'Free cash flow margin', fcf_yield: 'Free cash flow yield', price_to_fcf: 'Price / free cash flow',
  price_to_sma200: 'Price vs. 200-day average',
}
const plain: Record<string, string> = {
  trailing_pe: 'How much investors pay for each dollar of net income in the latest published fiscal year. Compare with the company’s earlier multiples before judging valuation.',
  ttm_pe: 'Market value divided by earnings from four consecutive reported quarters.',
  annualized_volatility: 'How much the closing price usually moves, scaled to a year. Larger moves mean more uncertainty.',
  price_to_sma200: 'Measures how far the price sits above or below its longer-term trend.',
  max_drawdown: 'The largest fall from a previous peak over the available price window.',
  interest_coverage: 'How many times operating income covers the interest bill.',
  current_ratio: 'Reported short-term assets divided by short-term liabilities.',
  fcf_yield: 'Cash left after capital spending per dollar of market value.',
}
const label = (key: string) => names[key] ?? key.replaceAll('_', ' ').replace(/^./, value => value.toUpperCase())
const number = (value: number | null | undefined) => value == null
  ? 'Unavailable'
  : value.toLocaleString('en-US', { maximumFractionDigits: 3 })

function formatted(key: string, value: number | null) {
  if (value == null) return 'Unavailable'
  if (/^annualized_volatility$|^max_drawdown$|^downside_volatility$|^var_95$|^cvar_95$|^earnings_variability$|^distance_from_52w_high$|^sma200_slope$|^price_to_sma200$|^ttm_earnings_yield$|^momentum_12_minus_1$|^relative_momentum_12m$|^margin_change$|yield$|margin$|growth$|^return_|^ro[ae]$|^roic$|_to_assets$/.test(key)) {
    return `${number(value * 100)}%`
  }
  if (/^ev_to_|^price_to_|^ttm_pe$|^trailing_pe$|^current_ratio$|^interest_coverage$|^debt_to_equity$|^cash_to_debt$|^net_debt_to_ebitda$|^relative_volume20$/.test(key)) return `${number(value)}×`
  return number(value)
}

export function CompanyAssessment({ data }: { data: Assessment }) {
  const [quant, setQuant] = useState(false)
  const { assessment: score, comparisons, fundamental_factors: facts } = data
  const dimensions = { fundamental: score.fundamental_score, technical: score.technical_score, risk: score.risk_resilience_score }
  const assigned = new Set(Object.values(groups).flat())
  function metric(key: string, item: { value: number | null; status: string; definition: string }, date: string | null, source: string) {
    const comparison = comparisons[key]
    const contributions = score.contributions.filter(entry => entry.metric === key)
    return <details className="metric-detail" key={key}>
      <summary><span>{label(key)}</span><strong>{formatted(key, item.value)}</strong></summary>
      <p>{quant ? item.definition : plain[key] ?? item.definition}</p>
      {item.value == null && <p>{item.status === 'not_applicable' ? 'This ratio does not apply to the issuer’s business model.' : 'Required inputs or a supported denominator are unavailable.'}</p>}
      {comparison && <p>Historical median {comparison.historical_median == null ? 'unavailable' : formatted(key, comparison.historical_median)}
        {comparison.historical_percentile != null && ` · Percentile ${number(comparison.historical_percentile)}`}
        {' '}({comparison.historical_count} observations). Historical sector comparison unavailable.</p>}
      {contributions.map(entry => <p key={entry.group}>{label(entry.group)} · {number(entry.score)}/100
        {' '}· {entry.points_from_neutral >= 0 ? '+' : ''}{number(entry.points_from_neutral)} score points from midpoint
        {quant && ` · Final weight ${number(entry.weight * 100)}% · Cohort median ${formatted(key, entry.cohort_median)} · n=${entry.cohort_count}`}</p>)}
      {quant && <p>{source} · Observation date {date ?? 'unavailable'}</p>}
      {quant && comparison && <details><summary>Historical observations</summary><ul>{comparison.history.map(point =>
        <li key={point.as_of}>{point.as_of} · {formatted(key, point.value)}</li>)}</ul></details>}
    </details>
  }

  return <section className="panel assessment-details">
    <div className="section-heading"><div><h2>Explore the evidence</h2><p>See how each conclusion was reached.</p></div>
      <div className="segments" aria-label="Evidence view"><button type="button" aria-pressed={!quant} onClick={() => setQuant(false)}>Simple</button>
        <button type="button" aria-pressed={quant} onClick={() => setQuant(true)}>Quant</button></div></div>
    {quant && <p className="epoch-value">{score.model_version} · {data.warehouse_epoch} · {score.normalization} {score.weight_status}</p>}
    <details><summary>Score breakdown</summary>
      {Object.entries(dimensions).map(([dimension, value]) => <details key={dimension}>
        <summary>{label(dimension === 'risk' ? 'risk_resilience' : dimension)} · {number(value)}{value != null && ' / 100'} · {number(score.weights[dimension] * 100)}% weight</summary>
        {Object.entries(score.breakdown[dimension]).map(([name, group]) => <div key={name}>
          <h4>{label(name)} · {number(group.score)}{group.score != null && ' / 100'}</h4>
          <p>Required evidence coverage {number(group.coverage * 100)}%</p>
          {group.reason && <p>{group.reason}</p>}
          <ul>{group.slots.map(slot => <li key={slot.required_one_of.join('|')}>
            {slot.selected ? `${label(slot.selected.metric)} · ${formatted(slot.selected.metric, slot.selected.raw)} → ${number(slot.selected.score)}/100`
              : `Requires ${slot.required_one_of.map(label).join(' or ')}`}
          </li>)}</ul>
          {quant && <p>Available · {group.available.map(label).join(', ') || '—'}<br/>
            Missing · {group.missing.map(label).join(', ') || '—'}<br/>Optional · {group.optional_metrics.map(label).join(', ') || '—'}</p>}
        </div>)}
      </details>)}
    </details>
    {Object.entries(groups).map(([group, keys]) => <details key={group}>
      <summary>{group}</summary>{keys.filter(key => facts.factors[key]).map(key => metric(key, facts.factors[key], facts.annual_period, 'SEC Company Facts'))}
    </details>)}
    {Object.keys(facts.factors).some(key => !assigned.has(key)) && <details><summary>Other reported indicators</summary>
      {Object.entries(facts.factors).filter(([key]) => !assigned.has(key)).map(([key,value]) => metric(key, value, facts.annual_period, 'SEC Company Facts'))}</details>}
    <details><summary>Technical indicators and market risk</summary>
      {data.market_factors.map(value => metric(value.factor, { value: value.value, definition: value.definition, status: value.data_status }, value.source_date, value.source ?? 'Market history'))}
    </details>
    {quant && <details><summary>SEC filing evidence</summary>
      <div className="table-wrap"><table><thead><tr><th>Source tag</th><th>Value</th><th>Period end</th><th>Filed</th><th>Accession</th></tr></thead>
        <tbody>{facts.filing_evidence.map(fact => <tr key={`${fact.canonical}-${fact.period_end}-${fact.accession_number}`}>
          <th scope="row">{fact.source_tag}</th><td>{number(fact.value)}</td><td>{fact.period_end}</td><td>{fact.filed_date}</td><td>{fact.accession_number}</td>
        </tr>)}</tbody></table></div>
    </details>}
    <details><summary>Options data</summary><p>{score.options_context.reason}</p></details>
  </section>
}
