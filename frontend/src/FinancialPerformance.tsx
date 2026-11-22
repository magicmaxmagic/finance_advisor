import { useMemo, useState } from 'react'
import { cash, pct } from './format'

type Link = { source: string; target: string; amount: number; tone: 'cost' | 'income' | 'retained' }
type Statement = {
  id: string; period_kind: 'annual' | 'quarter'; period_start: string; period_end: string
  accession: string; filed_date: string; source_url: string | null; accepted_at: string | null
  chart_status: 'available' | 'incomplete' | 'unreconciled' | 'signed_statement'
  values: Record<string, { value: number; source: string }>
  checks: { label: string; status: string; difference: number | null }[]
  margins: Record<string, number>; missing: string[]; links: Link[]
}
type CalendarEvent = {
  kind: string; label: string; event_date: string; event_at?: string | null; timezone?: string | null
  publication_timestamp?: string | null; status: string; source: string
  source_url: string | null; statement_id: string | null; evidence: string | null
}
export type PerformanceData = {
  currency: string; labels: Record<string, string>; statements: Statement[]
  events: CalendarEvent[]; segment_status: string
}

const displayOrder = ['revenue', 'cost_of_revenue', 'gross_profit', 'operating_expenses', 'research',
  'sales_marketing', 'administration', 'selling_general_admin', 'operating_income', 'nonoperating', 'pretax', 'tax', 'net_income']
const stages = [
  ['revenue'],
  ['gross_profit', 'cost_of_revenue'],
  ['operating_income', 'research', 'sales_marketing', 'administration', 'selling_general_admin', 'operating_expenses', 'nonoperating_gain'],
  ['pretax', 'nonoperating_loss', 'tax_benefit'],
  ['net_income', 'tax'],
]
const extraLabels: Record<string, string> = {
  nonoperating_gain: 'Nonoperating income', nonoperating_loss: 'Nonoperating expense', tax_benefit: 'Income tax benefit',
}
const compact = (amount: number, currency: string) => new Intl.NumberFormat('en-US', {
  style: 'currency', currency, notation: 'compact', maximumFractionDigits: 2,
}).format(amount)

function drawFlows(links: Link[]) {
  const total = (id: string, key: 'source' | 'target') => links.reduce((sum, link) => sum + (link[key] === id ? link.amount : 0), 0)
  const columns = stages.map(column => column.map(id => ({ id, amount: Math.max(total(id, 'source'), total(id, 'target')) }))
    .filter(node => node.amount > 0))
  const scale = Math.min(...columns.map(column =>
    (435 - Math.max(0, column.length - 1) * 36) / column.reduce((sum, node) => sum + node.amount, 0)))
  const nodes = new Map<string, { x: number; y: number; height: number; amount: number }>()
  columns.forEach((column, index) => {
    let y = 53
    column.forEach(node => {
      nodes.set(node.id, { x: 16 + index * 207, y, height: node.amount * scale, amount: node.amount })
      y += node.amount * scale + 36
    })
  })
  const outgoing = new Map<string, number>()
  const incoming = new Map<string, number>()
  return { nodes, ribbons: links.map(link => {
    const source = nodes.get(link.source)!, target = nodes.get(link.target)!
    const start = source.y + (outgoing.get(link.source) ?? 0)
    const end = target.y + (incoming.get(link.target) ?? 0)
    const h = link.amount * scale, a = source.x + 8, b = target.x, mid = (a + b) / 2
    outgoing.set(link.source, start + h)
    incoming.set(link.target, end + h)
    return { ...link, path: `M ${a} ${start} C ${mid} ${start} ${mid} ${end} ${b} ${end}
      L ${b} ${end + h} C ${mid} ${end + h} ${mid} ${start + h} ${a} ${start + h} Z` }
  }) }
}

function IncomeFlow({ statement, labels, currency }: { statement: Statement; labels: Record<string, string>; currency: string }) {
  const [active, setActive] = useState<number | null>(null)
  const flow = useMemo(() => drawFlows(statement.links), [statement.links])
  const label = (key: string) => labels[key] ?? extraLabels[key] ?? key
  return <>
    <div className="income-flow-wrap" tabIndex={0} role="region" aria-label="Scrollable income statement flow chart">
      <svg viewBox="0 0 1025 522" role="group" aria-label="Revenue, spending, and profit Sankey">
        {['Revenue', 'Production', 'Operations', 'Before tax', 'After tax'].map((heading, index) =>
          <text key={heading} x={16 + index * 207} y="20" className="flow-heading">{heading}</text>)}
        {flow.ribbons.map((link, index) => <path key={`${link.source}-${link.target}`} d={link.path}
          className={`flow-ribbon flow-${link.tone}${active === index ? ' flow-active' : ''}`}
          tabIndex={0} role="button" aria-pressed={active === index}
          aria-label={`${label(link.source)} to ${label(link.target)}, ${cash(link.amount, currency)}`}
          onMouseEnter={() => setActive(index)} onMouseLeave={() => setActive(null)}
          onFocus={() => setActive(index)} onBlur={() => setActive(null)}
          onClick={() => setActive(index)} onKeyDown={event => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setActive(index) }
            if (event.key === 'Escape') setActive(null)
          }}><title>{label(link.source)} → {label(link.target)} · {cash(link.amount, currency)}</title></path>)}
        {[...flow.nodes].map(([key, point]) => <g key={key} pointerEvents="none">
          <rect x={point.x} y={point.y} width="8" height={Math.max(point.height, 1)} rx="3" />
          <text x={point.x} y={point.y - 9}>{label(key)} <tspan>{compact(point.amount, currency)}</tspan></text>
        </g>)}
      </svg>
    </div>
    <p className="income-flow-key">
      {active === null ? 'Hover or focus a flow to see its amount. Width represents the amount of revenue, expense, or profit.' :
        `${label(flow.ribbons[active].source)} → ${label(flow.ribbons[active].target)} · ${cash(flow.ribbons[active].amount, currency)}`}
    </p>
  </>
}

function EventDate({ event }: { event: CalendarEvent }) {
  if (event.event_at && event.timezone) return <time dateTime={event.event_at}>{new Intl.DateTimeFormat('en-US', {
    dateStyle: 'medium', timeStyle: 'short', timeZone: event.timezone,
  }).format(new Date(event.event_at))} · {event.timezone.replaceAll('_', ' ')}</time>
  return <time dateTime={event.event_date}>{event.event_date}</time>
}

export function FinancialPerformance({ data }: { data: PerformanceData }) {
  const [selected, setSelected] = useState<string>()
  const [kind, setKind] = useState<'quarter' | 'annual'>(data.statements.some(item => item.period_kind === 'quarter') ? 'quarter' : 'annual')
  const [quant, setQuant] = useState(false)
  const [allEvents, setAllEvents] = useState(false)
  const periods = data.statements.filter(item => item.period_kind === kind)
  const statement = periods.find(item => item.id === selected) ?? periods[0]
  const prior = statement && periods.find(item => {
    const days = (Date.parse(statement.period_end) - Date.parse(item.period_end)) / 86400000
    return days >= 350 && days <= 380
  })
  const names = ['revenue', 'cost_of_revenue', 'operating_income', 'net_income']
  const events = allEvents ? data.events : data.events.filter(event => event.kind !== 'sec_filing' || event.statement_id !== null)
  const chartIssue = statement && ({
    incomplete: 'The source does not support a complete revenue-to-income bridge.',
    unreconciled: 'Reported amounts do not reconcile. Review the signed values and filing before drawing conclusions.',
    signed_statement: 'This period contains a loss or a signed operating item. The signed statement below preserves its direction.',
    available: '',
  })[statement.chart_status]
  const margin = statement?.margins.operating_income, priorMargin = prior?.margins.operating_income
  return <div className="financial-performance-body">
    <div className="performance-controls">
      <div role="group" aria-label="Statement frequency">
        {(['quarter', 'annual'] as const).map(option => <button type="button" key={option} aria-pressed={kind === option}
          onClick={() => { setKind(option); setSelected(undefined) }}>{option === 'quarter' ? 'Quarterly' : 'Annual'}</button>)}
      </div>
      <label>Period ending <select value={statement?.id ?? ''} onChange={event => setSelected(event.target.value)}>
        {!periods.length && <option value="">No published statements</option>}
        {periods.map(item => <option key={item.id} value={item.id}>{item.period_end}</option>)}
      </select></label>
      <div role="group" aria-label="Financial performance detail">
        <button type="button" aria-pressed={!quant} onClick={() => setQuant(false)}>Simple</button>
        <button type="button" aria-pressed={quant} onClick={() => setQuant(true)}>Quant</button>
      </div>
    </div>
    {!statement && <p>No comparable consolidated statement has been published for this period type and research date.</p>}
    {statement && <>
      <div className="performance-kpis">
        {names.map(key => <div key={key}><span>{data.labels[key]}</span>
          <strong>{statement.values[key] ? compact(statement.values[key].value, data.currency) : 'Unavailable'}</strong></div>)}
      </div>
      <p className="performance-summary">{margin != null && <>Operating profit was {pct(margin)} of revenue. </>}
        {margin != null && priorMargin != null && prior && (margin === priorMargin ? <>Operating margin was unchanged from the comparable period ending {prior.period_end}. </> :
          <>That is {Math.abs((margin - priorMargin) * 100).toFixed(1)} percentage points {margin > priorMargin ? 'higher than' : 'lower than'} the comparable period ending {prior.period_end}. </>)}
        {statement.values.nonoperating?.value !== undefined && statement.values.nonoperating.value !== 0 && <>{compact(Math.abs(statement.values.nonoperating.value), data.currency)} {statement.values.nonoperating.value < 0 ? 'in net nonoperating expense reduced' : 'in net nonoperating income increased'} profit before tax.</>}
      </p>
      {statement.chart_status === 'available' ? <IncomeFlow key={statement.id} statement={statement} labels={data.labels} currency={data.currency} /> :
        <p role="status">{chartIssue} {statement.missing.length > 0 && `Missing: ${statement.missing.map(key => data.labels[key]).join(', ')}.`}</p>}
      <div className="table-wrap"><table className="statement-table">
        <caption>{quant ? 'Reported values and reconciliation inputs' : 'Consolidated income statement and comparison'}</caption>
        <thead><tr><th scope="col">Statement item</th><th scope="col">{statement.period_end} ({data.currency})</th>
          {prior && <th scope="col">{prior.period_end} ({data.currency})</th>}
          {quant && <th scope="col">Filing concept or derivation</th>}</tr></thead>
        <tbody>{displayOrder.filter(key => statement.values[key] || prior?.values[key] || statement.missing.includes(key)).map(key => (
          <tr key={key}><th scope="row">{data.labels[key]}</th><td>{cash(statement.values[key]?.value, data.currency)}</td>
            {prior && <td>{cash(prior.values[key]?.value, data.currency)}</td>}
            {quant && <td>{statement.values[key]?.source ?? 'Unavailable'}</td>}</tr>
        ))}</tbody>
      </table></div>
      {quant && <><h4>Reconciliation</h4><ul>{statement.checks.map(check => <li key={check.label}>
        {check.label} · {check.status}{check.difference != null && ` · difference ${cash(check.difference, data.currency)}`}
      </li>)}</ul></>}
      <p className="performance-source">{statement.source_url ? <a href={statement.source_url} target="_blank" rel="noreferrer">View SEC filing</a> : 'SEC filing'}
        {' · '}Filed {statement.filed_date}{quant && ` · accession ${statement.accession} · ${statement.period_start} to ${statement.period_end}`}
        {quant && (statement.accepted_at ? ` · accepted ${statement.accepted_at}` : ' · filing date precision')}</p>
      <p>{data.segment_status}</p>
    </>}
    <section className="performance-calendar" aria-label="Financial events">
      <div><h3>Financial events</h3><button type="button" aria-pressed={allEvents} onClick={() => setAllEvents(previous => !previous)}>
        {allEvents ? 'Show key events' : 'Include all SEC filings'}</button></div>
      {!events.length ? <p>No source-attributed events are available for this issuer at the selected date.</p> :
        <ul>{events.slice(0, allEvents ? 20 : 10).map(event => <li key={`${event.event_date}-${event.kind}-${event.evidence ?? event.label}`}>
          <div><strong>{event.label}</strong><span><EventDate event={event} /> · {event.status} · {event.source_url ?
            <a href={event.source_url} target="_blank" rel="noreferrer">{event.source}</a> : event.source}</span></div>
          {event.statement_id && <button type="button" onClick={() => {
            const target = data.statements.find(item => item.id === event.statement_id)
            if (target) { setKind(target.period_kind); setSelected(target.id); document.getElementById('company-financial-performance')?.scrollIntoView({ block: 'start' }) }
          }}>View statement</button>}
        </li>)}</ul>}
    </section>
  </div>
}
