import { useEffect, useRef, useState } from 'react'
import { FinancialChart } from './DeferredChart'
import { pct } from './format'
import { financialGlossary } from './financialGlossary'

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

type Diagnostic = { code: string; message: string }

type Group = {
  score: number | null
  coverage: number
  available: string[]
  missing: string[]
  optional_metrics: string[]
  reason: string | null
  slots: { required_one_of: string[]; selected: Contribution | null; blockers?: Record<string, Diagnostic> }[]
}

type Comparison = {
  historical_count: number
  historical_median: number | null
  historical_percentile: number | null
  sector_status: string
  valid_peer_count: number
  history: { as_of: string; value: number }[]
}

type FilingEvidence = { canonical: string; source_tag: string; period_start?: string | null; period_end: string; filed_date: string; accession_number: string; value: number; operation?: string }

export type Assessment = {
  ticker: string
  as_of: string
  source_date: string | null
  published_at: string
  warehouse_epoch: string
  reported_share_market_cap?: number | null
  market_cap_inputs?: Record<string, string | number> | null
  market_cap_diagnostic?: Diagnostic | null
  share_class_evidence?: {
    class_member: string; listed_symbol: string | null; shares: number; period_end: string;
    filed_date: string; value: number | null; reason?: string; source_url: string; accession_number: string
  }[] | null
  decision_research?: Record<string, {rule: string; observed: boolean | null}>
  comparisons: Record<string, Comparison>
  financial_context?: {
    history: Record<string,{ observations:number; minimum:number|null; maximum:number|null; median_relation:string; recent_change:number|null; recent_four_minus_earlier_four:number|null }>
    market: { status:string; benchmark?:string; trend?:string; price_return_12m?:number; realized_volatility_60?:number; drawdown_from_252_session_high?:number; observation_date?:string }
    peers: { status:string; comparisons:Record<string,{ cohort:string; label:string; count:number; median:number; percentile:number }> }
    predictive_evidence: { status:string; end?:string; factors:Record<string,{ ic:number|null; interval:number[]|null; observations:number|null; holm_p:number|null }> }
    events: { kind:string; label:string; source:string|null; event_date:string; timing:string; publication_timestamp:string|null; evidence?:string|null; source_url?:string|null; split_factor?:number|null; amount_per_share?:number|null; limitation:string }[]
    limitations:string[]
  }
  fundamental_factors: {
    annual_period: string | null
    factors: Record<string, { value: number | null; status: string; definition: string; diagnostic?: Diagnostic | null }>
    filing_evidence: FilingEvidence[]
    ttm_quarter_evidence?: FilingEvidence[]
    ttm_bridge_evidence?: FilingEvidence[]
    limitations?: string[]
  }
  market_factors: { factor: string; value: number | null; data_status: string; definition: string; source_date: string | null; source: string | null; diagnostic?: Diagnostic }[]
  assessment: {
    model_version: string
    input_version?: string
    opportunity_score: number | null
    fundamental_score: number | null
    technical_score: number | null
    risk_resilience_score: number | null
    market_risk_score?: number | null
    financial_risk_score?: number | null
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
  'Financial health': ['equity_to_assets', 'debt_to_assets', 'reported_debt_to_assets', 'net_debt_to_ebitda', 'debt_to_equity', 'current_ratio', 'cash_to_debt', 'interest_coverage', 'nonoperating_interest_coverage'],
  'Cash flow': ['cash_flow_quality', 'fcf_margin'],
}

const names: Record<string, string> = {
  trailing_pe: 'Fiscal-year P/E', ttm_pe: 'Trailing 12-month P/E', roa: 'Return on assets',
  roe: 'Return on equity', roic: 'Return on invested capital', ev_to_operating_ebitda: 'EV / EBITDA',
  fcf_margin: 'Free cash flow margin', fcf_yield: 'Free cash flow yield', price_to_fcf: 'Price / free cash flow',
  price_to_sma200: 'Price vs. 200-day average',
  nonoperating_interest_coverage: 'Nonoperating interest coverage',
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
const indicatorName = (key: string) => names[key] ?? key.replaceAll('_', ' ').replace(/^./, value => value.toUpperCase())
const number = (value: number | null | undefined) => value == null
  ? 'Unavailable'
  : value.toLocaleString('en-US', { maximumFractionDigits: 3 })

function formatIndicator(key: string, value: number | null) {
  if (value == null) return 'Unavailable'
  if (/^annualized_volatility$|^max_drawdown$|^downside_volatility$|^var_95$|^cvar_95$|^earnings_variability$|^distance_from_52w_high$|^sma200_slope$|^price_to_sma200$|^ttm_earnings_yield$|^momentum_12_minus_1$|^relative_momentum_12m$|^margin_change$|yield$|margin$|growth$|^return_|^ro[ae]$|^roic$|_to_assets$/.test(key)) {
    return `${number(value * 100)}%`
  }
  if (/^ev_to_|^price_to_|^ttm_pe$|^trailing_pe$|^current_ratio$|interest_coverage$|^debt_to_equity$|^cash_to_debt$|^net_debt_to_ebitda$|^relative_volume20$/.test(key)) return `${number(value)}×`
  return number(value)
}

const views = ['Valuation', 'Business quality', 'Technical', 'Risks', 'Score breakdown', 'Market & events'] as const
type View = typeof views[number]
type Metric = { value: number | null; status: string; definition: string; date: string | null; source: string; diagnostic?: Diagnostic | null }
const marketRisk = ['annualized_volatility','max_drawdown','downside_volatility','beta','var_95','cvar_95','sharpe','sortino']
const businessKeys = [...groups.Profitability, ...groups.Growth, ...groups['Cash flow']]
const dependencies: Record<string,string[]> = {
  trailing_pe: ['net_income'], ttm_pe: ['net_income'], earnings_yield: ['net_income'], ttm_earnings_yield: ['net_income'],
  ev_to_operating_ebitda: ['operating_income','depreciation_amortization','total_debt','preferred_equity','noncontrolling_interest','cash'],
  ev_to_ebit: ['operating_income','total_debt','preferred_equity','noncontrolling_interest','cash'],
  ev_to_revenue: ['revenue','total_debt','preferred_equity','noncontrolling_interest','cash'],
  fcf_yield: ['cfo','capex'], price_to_fcf: ['cfo','capex'], fcf_growth: ['cfo','capex'],
  roe: ['net_income','equity'], roa: ['net_income','total_assets'],
  revenue_growth: ['revenue'], earnings_growth: ['net_income'], operating_margin: ['operating_income','revenue'],
  current_ratio: ['current_assets','current_liabilities'], interest_coverage: ['operating_income','interest_expense'],
  nonoperating_interest_coverage: ['operating_income','nonoperating_interest_expense'],
}
const focusMetrics: Record<string,string[]> = {
  Valuation: ['trailing_pe','ttm_pe','ev_to_operating_ebitda','ev_to_revenue','price_to_fcf','fcf_yield'],
  'Business quality': ['roic','roe','operating_margin','ebitda_margin','revenue_growth','earnings_growth','fcf_growth','debt_to_assets'],
  Technical: ['ma50','ma200','price_to_sma200','sma200_slope','return_3m','rsi','macd'],
  Risks: ['annualized_volatility','max_drawdown','beta','net_debt_to_ebitda','reported_debt_to_assets','interest_coverage','nonoperating_interest_coverage','current_ratio'],
}
const valued = (key: string, value: number | null) => formatIndicator(key, value)
const label = indicatorName

export function CompanyAssessment({ data }: { data: Assessment }) {
  const [view, setView] = useState<View>('Valuation')
  const [quant, setQuant] = useState(() => localStorage.getItem('finlab-quant-view') === 'true')
  const [selected, select] = useState<string | null>(null)
  const [all, setAll] = useState(false)
  const [allEvents, setAllEvents] = useState(false)
  const [history, setHistory] = useState('revenue')
  const dialog = useRef<HTMLDialogElement>(null)
  const { assessment: score, comparisons, fundamental_factors: facts } = data
  const annual = Object.fromEntries(Object.entries(facts.factors).map(([key, metric]) => [key,
    { ...metric, date: facts.annual_period, source: 'SEC Company Facts' }]))
  const market = Object.fromEntries(data.market_factors.map(metric => [metric.factor,
    { ...metric, status: metric.data_status, date: metric.source_date, source: metric.source ?? 'Published prices' }]))
  const metrics: Record<string, Metric> = { ...annual, ...market }
  const assigned = new Set(Object.values(groups).flat())
  const keys = view === 'Valuation' ? groups.Valuation
    : view === 'Business quality' ? [...businessKeys, ...groups['Financial health'], ...Object.keys(annual).filter(key => !assigned.has(key))]
    : view === 'Technical' ? Object.keys(market)
    : [...marketRisk, ...groups['Financial health']]
  const dimensionValues = { fundamental: score.fundamental_score, technical: score.technical_score, risk: score.risk_resilience_score }
  const active = selected ? metrics[selected] : undefined
  const comparison = selected ? comparisons[selected] : undefined
  const inputs = selected
    ? (selected.startsWith('ttm_') ? [...(facts.ttm_quarter_evidence ?? []), ...(facts.ttm_bridge_evidence ?? [])] : facts.filing_evidence)
      .filter(row => (dependencies[selected] ?? [selected]).includes(row.canonical))
    : []
  const context = data.financial_context
  const historical = selected ? context?.history[selected] : undefined
  const peer = selected ? context?.peers.comparisons[selected] : undefined
  const evidence = selected ? context?.predictive_evidence.factors[selected] : undefined
  const contribution = score.contributions.filter(item => item.metric === selected)
  const distance = market.price_to_sma200?.value
  const momentum = market.return_3m?.value
  const relative = market.relative_momentum_12m?.value

  function changeMode(value: boolean) {
    setQuant(value)
    localStorage.setItem('finlab-quant-view', String(value))
  }

  useEffect(() => {
    if (selected && dialog.current && !dialog.current.open) dialog.current.showModal()
  }, [selected])

  const pe = comparisons.trailing_pe
  const historicalValuation = pe?.historical_percentile != null && pe.historical_median != null ? (
    <>The historical median fiscal-year P/E is <strong>{valued('trailing_pe', pe.historical_median)}</strong>.
      The current multiple is at the <strong>{number(pe.historical_percentile)}th percentile</strong> of its observed history.</>
  ) : <>{annual.trailing_pe?.diagnostic?.message ?? 'Comparable historical P/E observations are unavailable for this cutoff.'}</>
  const sourceHistory = facts.filing_evidence.filter(row => row.canonical === history && row.period_start)
    .map(row => ({ date: row.period_end, close: row.value })).sort((a,b) => a.date.localeCompare(b.date))
  const periods = new Map(facts.filing_evidence.filter(row => row.canonical === 'capex' && row.period_start)
    .map(row => [`${row.period_start}/${row.period_end}`,row.value]))
  const fcf = facts.filing_evidence.flatMap(row => {
    const capex = periods.get(`${row.period_start}/${row.period_end}`)
    return row.canonical === 'cfo' && row.period_start && capex != null ? [{ date: row.period_end, close: row.value-capex }] : []
  }).sort((a,b) => a.date.localeCompare(b.date))
  const trend = history === 'fcf' ? fcf : sourceHistory

  return <section className="panel v4-evidence">
    <div className="section-heading"><div><h2>Research details</h2><p>Choose a topic, then open any metric to see its evidence.</p></div>
      <div className="segments" aria-label="Evidence view"><button type="button" aria-pressed={!quant} onClick={() => changeMode(false)}>Simple</button>
        <button type="button" aria-pressed={quant} onClick={() => changeMode(true)}>Quant</button></div></div>
    <div className="topic-nav" aria-label="Research topic">{views.map(topic => <button type="button" key={topic} aria-pressed={view === topic}
      onClick={() => { setView(topic); setAll(false) }}>{topic}</button>)}</div>
    {quant && <p className="epoch-value">{score.model_version}{score.input_version && ` · ${score.input_version}`} · {score.normalization} {score.weight_status}</p>}
    {view === 'Valuation' && <div className="v4-context"><strong>Valuation in context</strong><p>{historicalValuation}</p></div>}
    {view === 'Valuation' && data.share_class_evidence?.length ? <details className="v4-context">
      <summary>Why · Share-class evidence</summary>
      <ul>{data.share_class_evidence.map(item => <li key={item.class_member}>
        <strong>{item.listed_symbol ?? 'Class without a verified listed symbol'}</strong> · {number(item.shares)} reported shares at {item.period_end}
        {item.reason && ` · ${item.reason.replaceAll('_', ' ').toLowerCase()}`}
        {quant && <p>{item.class_member} · filed {item.filed_date} · {item.accession_number}
          {item.source_url.startsWith('https://www.sec.gov/') && <> · <a href={item.source_url}>SEC filing</a></>}</p>}
      </li>)}</ul>
    </details> : null}
    {view === 'Technical' && <div className="v4-context"><strong>Price behavior</strong><p>
      {distance == null ? 'The 200-session trend is unavailable.' : distance === 0 ? 'The stock closed at its 200-session average.' : `The close is ${pct(Math.abs(distance))} ${distance < 0 ? 'below' : 'above'} its 200-session average.`}
      {momentum != null && ` The 63-session price return was ${pct(momentum)}.`}
      {relative != null && (relative === 0 ? ' Its 252-session price return matched SPY.'
        : ` Its 252-session price return was ${number(Math.abs(relative) * 100)} percentage points ${relative < 0 ? 'below' : 'above'} SPY.`)}
      {' '}Use these observations to assess price trends separately from valuation.</p></div>}
    {view === 'Risks' && <div className="v4-context"><strong>Financial risk, market risk and uncertainty</strong>
      <p>Market Risk Score <strong>{number(score.market_risk_score)}</strong> · Financial Risk Score <strong>{number(score.financial_risk_score)}</strong>. Higher scores indicate greater relative resilience.</p>
      <p>The combined resilience score requires both. See Score breakdown for required measures and comparison counts.</p></div>}
    {view === 'Risks' && data.decision_research && <div className="v4-context">
      <strong>Exploratory research checks</strong>
      <dl className="publication">{Object.entries(data.decision_research).map(([key, result]) =>
        <div key={key}><dt>{key === 'entry' ? 'Entry conditions' : 'Thesis review'} · {result.observed == null ? 'Unavailable' : result.observed ? 'Observed' : 'Not observed'}</dt>
          <dd>{result.rule}</dd></div>)}</dl>
      <p>These conditions are research hypotheses. Portfolio holdings and costs can change the decision.</p>
    </div>}
    {view === 'Market & events' ? <div className="v4-context">
      <h3>U.S. equity market context</h3>
      {context?.market.status === 'available' ? <p>SPY {context.market.trend} · 12-month price return {pct(context.market.price_return_12m)}
        {' '}· 60-session annualized volatility {pct(context.market.realized_volatility_60)}
        {' '}· Distance from 252-session high {pct(context.market.drawdown_from_252_session_high)} · {context.market.observation_date}.</p>
        : <p>Comparable market observations are unavailable at this cutoff.</p>}
      <p>Release-vintage macro data and timestamped sentiment are unavailable in the local publication.</p>
      <h3>Recorded company events</h3>
      {context?.events.length ? <><ul>{context.events.slice(0, allEvents ? undefined : 5).map((event,index) => <li key={`${event.event_date}-${index}`}>
        <strong>{event.label}</strong> · {event.event_date} · {event.source ?? 'Source missing'} · {event.timing.replaceAll('_',' ')}
        {event.split_factor != null && <span> · {number(event.split_factor)}-for-1 split factor</span>}
        {event.publication_timestamp && <span> · {event.publication_timestamp}</span>}
        {event.source_url?.startsWith('https://www.sec.gov/') && <> · <a href={event.source_url} target="_blank" rel="noreferrer">SEC filing</a></>}
        {quant && <p>{event.evidence ? `${event.evidence} · ` : ''}{event.limitation}</p>}
      </li>)}</ul>{context.events.length > 5 && <button className="text-button" type="button" aria-expanded={allEvents}
        onClick={() => setAllEvents(!allEvents)}>{allEvents ? 'Show latest five events' : `Show all ${context.events.length} events`}</button>}</>
        : <p>No supported filing or corporate-action records were published for this company at the cutoff.</p>}
      {quant && context?.limitations.map(item => <p key={item}>{item}</p>)}
    </div> : view === 'Score breakdown' ? <div className="v4-score-groups">
      {Object.entries(dimensionValues).map(([dimension,value]) => <article className="v4-score-group" key={dimension}>
        <h3>{label(dimension)} <span>{number(value)}{value != null && ' / 100'}</span></h3>
        <p>{number(score.weights[dimension] * 100)}% combined score weight</p>
        {Object.entries(score.breakdown[dimension]).map(([group, result]) => <div key={group} className="v4-group">
          <strong>{label(group)} · {number(result.score)}{result.score != null && ' / 100'}</strong>
          <p>{number(result.coverage*100)}% required evidence coverage{result.reason && ` · ${result.reason}`}</p>
          {result.slots.map(slot => slot.selected ? <button key={slot.selected.metric} type="button" className="text-button"
            onClick={() => select(slot.selected!.metric)}>{label(slot.selected.metric)} · {number(slot.selected.score)}/100</button>
            : <div key={slot.required_one_of.join('|')}><p>Requires {slot.required_one_of.map(label).join(' or ')}</p>
              <ul>{Object.entries(slot.blockers ?? {}).map(([key, blocker]) => <li key={key}>
                <strong>{label(key)}</strong> · {blocker.message}{quant && ` (${blocker.code})`}
              </li>)}</ul></div>)}
          {quant && result.slots.some(slot => slot.selected && Object.keys(slot.blockers ?? {}).length) && <ul>
            {result.slots.flatMap(slot => slot.selected ? Object.entries(slot.blockers ?? {}) : []).map(([key, blocker]) =>
              <li key={key}>{label(key)} · {blocker.message} ({blocker.code})</li>)}
          </ul>}
          {quant && result.missing.length > 0 && <p>Missing inputs · {result.missing.map(label).join(', ')}</p>}
        </div>)}
      </article>)}
      <p>FinLab’s default weights are 60% fundamentals, 25% technical and 15% resilience. The weights are configurable and remain unvalidated.</p>
    </div> : <>
      <div className="v4-metric-grid">
        {[...new Set(all ? [...(focusMetrics[view] ?? []), ...keys] : focusMetrics[view] ?? keys)].filter(key => metrics[key]).map(key =>
          <button key={key} type="button" className="v4-metric" aria-label={`Explain ${label(key)}`} onClick={() => select(key)}>
            <span>{label(key)}</span><strong>{valued(key, metrics[key].value)}</strong>
            <small>{metrics[key].value == null ? 'See coverage' : 'View evidence'}</small>
          </button>)}
      </div>
      <button className="text-button" type="button" onClick={() => setAll(!all)}>{all ? 'Show key metrics' : 'Show all metrics'}</button>
      {view === 'Business quality' && <div className="v4-context"><label>Financial trend <select value={history} onChange={event => setHistory(event.target.value)}>
        <option value="revenue">Revenue</option><option value="net_income">Net income</option><option value="fcf">Free cash flow</option>
      </select></label>
      {trend.length > 1 ? <FinancialChart points={trend} label="Reported fiscal-year observations" currency="USD" showPriceTools={false} initialRange="MAX"/>
        : <p>At least two comparable fiscal-year observations are needed.</p>}</div>}
    </>}
    {quant && <p className="v4-provenance">Warehouse {data.warehouse_epoch} · Published {data.published_at} · Cutoff {data.as_of}</p>}
    <dialog ref={dialog} onClose={() => select(null)} className="evidence-drawer" aria-labelledby="metric-heading">
      {active && selected && <div className="drawer-body">
        <div className="section-heading"><h2 id="metric-heading">{label(selected)}</h2>
          <button type="button" onClick={() => dialog.current?.close()}>Close evidence</button></div>
        <strong className="drawer-value">{valued(selected, active.value)}</strong>
        <p>{financialGlossary[selected]?.meaning ?? plain[selected] ?? active.definition}</p>
        {active.value == null && <p className="warning">{active.diagnostic?.message ?? (active.status === 'not_applicable' ? 'This indicator does not apply to the issuer’s business model.' : 'Required inputs, price history, or a supported denominator are unavailable.')}
          {quant && active.diagnostic && ` (${active.diagnostic.code})`}</p>}
        <dl className="publication">
          <dt>Measurement period</dt><dd>{selected.startsWith('ttm_')
            ? (inputs.length ? `Trailing year ending ${inputs[0].period_end} · ${inputs[0].operation ? 'fiscal-year and YTD bridge' : 'four reported quarters'}` : 'Unavailable')
            : active.date ?? 'Unavailable'}</dd>
          <dt>Source</dt><dd>{active.source}</dd>
          <dt>Assessment cutoff</dt><dd>{data.as_of}</dd>
          {comparison && <><dt>Historical median</dt><dd>{valued(selected, comparison.historical_median)}</dd>
          <dt>Historical percentile</dt><dd>{number(comparison.historical_percentile)} · {comparison.historical_count} observations</dd>
          <dt>Historical peers</dt><dd>{comparison.valid_peer_count ? `${comparison.valid_peer_count} valid peers` : 'Comparable historical peer data unavailable'}</dd></>}
          {historical && historical.observations > 0 && <><dt>Observed range</dt><dd>{valued(selected,historical.minimum)} to {valued(selected,historical.maximum)}</dd></>}
          {peer && <><dt>Current {peer.cohort} peers</dt><dd>{peer.label} · {peer.count} peers · median {valued(selected,peer.median)}
            {' '}· percentile {number(peer.percentile)}. Uses current published classifications.</dd></>}
        </dl>
        {historical && ['above', 'at', 'below'].includes(historical.median_relation) &&
          <p>The current value is {historical.median_relation} its own sampled median.
            {historical.recent_four_minus_earlier_four != null && historical.recent_four_minus_earlier_four !== 0 &&
              ` The median of its four most recent comparisons was ${historical.recent_four_minus_earlier_four > 0 ? 'higher' : 'lower'} than the median of the preceding four.`}</p>}
        {contribution.length ? contribution.map(entry => <div className="v4-context" key={entry.group}>
          <strong>{label(entry.group)} · {number(entry.score)}/100</strong>
          <p>{entry.points_from_neutral >= 0 ? '+' : ''}{number(entry.points_from_neutral)} points relative to the 50 midpoint.
            {quant && ` Final weight ${number(entry.weight*100)}%. Cohort median ${valued(selected,entry.cohort_median)}. Sample ${entry.cohort_count}. Direction ${entry.direction}.`}</p>
        </div>) : <p>This metric supplied no direct contribution to the combined score at this cutoff.</p>}
        {evidence ? <div className="v4-context"><strong>Archived historical research</strong>
          <p>21-session study through {context?.predictive_evidence.end} · Rank IC {number(evidence.ic)} · {evidence.observations ?? 0} windows
            {' '}· 95% interval {evidence.interval?.map(value => number(value)).join(' to ') ?? 'unavailable'}. Retrospective aggregate across companies.</p>
          {quant && <p>Holm-adjusted p-value {number(evidence.holm_p)}. Archived evidence is separate from the company score.</p>}
        </div> : <p>{context?.predictive_evidence.status === 'outcomes_after_assessment'
          ? 'The archived study includes outcomes after this cutoff, so its results are withheld here.'
          : 'Direct historical validation evidence is unavailable for this metric.'}</p>}
        <div className="segments"><button type="button" aria-pressed={!quant} onClick={() => changeMode(false)}>Simple</button>
          <button type="button" aria-pressed={quant} onClick={() => changeMode(true)}>Quant</button></div>
        {quant && <><h3>Formula and provenance</h3><p>{active.definition}</p>
          <p>{score.normalization} Model {score.model_version} · {data.warehouse_epoch}</p>
          {inputs.length > 0 && <ul>{inputs.slice(0,12).map(input => <li key={`${input.canonical}-${input.period_end}-${input.accession_number}`}>
            {input.operation && `${label(input.operation)} · `}{label(input.canonical)} · {number(input.value)} · {input.period_end} · Filed {input.filed_date} · {input.source_tag} · {input.accession_number}
          </li>)}</ul>}
          {data.market_cap_inputs && /pe$|yield$|^ev_to_|^price_to_(fcf|book|sales)/.test(selected) &&
            <p>Reported-share market value {number(data.reported_share_market_cap)} USD. Inputs: {Object.entries(data.market_cap_inputs).map(([key,value]) => `${label(key)} ${value}`).join(' · ')}</p>}
          {selected in annual && facts.limitations?.map(reason => <p key={reason}>{reason}</p>)}</>}
        {comparison?.history && comparison.history.length > 1 && <FinancialChart points={[...comparison.history]
          .sort((a,b) => a.as_of.localeCompare(b.as_of)).map(row => ({ date: row.as_of, close: valued(selected,row.value).endsWith('%') ? row.value * 100 : row.value }))}
          units={active.value != null ? valued(selected,active.value).match(/[×%]$/)?.[0] : undefined}
          label="Historical comparison observations" showPriceTools={false} initialRange="MAX"/>}
      </div>}
    </dialog>
  </section>
}
