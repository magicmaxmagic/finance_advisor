import { useState } from 'react'
import { IncomeSankey, type IncomeStatement } from './IncomeSankey'
import { cash, pct } from './format'
import { annualChange, compactMoney } from './financialAmounts'

type CalendarEvent = {
  kind: string; label: string; event_date: string; event_at?: string | null; timezone?: string | null
  publication_timestamp?: string | null; status: string; source: string
  source_url: string | null; statement_id: string | null; evidence: string | null
}
export type PerformanceData = {
  currency: string; labels: Record<string, string>; statements: IncomeStatement[]
  events: CalendarEvent[]; segment_status: string
}

const displayOrder = ['revenue', 'cost_of_revenue', 'gross_profit', 'operating_expenses', 'research',
  'sales_marketing', 'administration', 'selling_general_admin', 'operating_income', 'nonoperating', 'pretax', 'tax', 'net_income']
function EventDate({ event }: { event: CalendarEvent }) {
  if (event.event_at && event.timezone) return <time dateTime={event.event_at}>{new Intl.DateTimeFormat('en-US', {
    dateStyle: 'medium', timeStyle: 'short', timeZone: event.timezone,
  }).format(new Date(event.event_at))} · {event.timezone.replaceAll('_', ' ')}</time>
  return <time dateTime={event.event_date}>{event.event_date}</time>
}

export function FinancialPerformance({ data, company, initialPeriod }: { data: PerformanceData; company: string; initialPeriod?: string | null }) {
  const linked = data.statements.find(row => row.id === initialPeriod)
  const [selected, setSelected] = useState(linked?.id)
  const [kind, setKind] = useState<'quarter' | 'annual'>(linked?.period_kind ?? (data.statements.some(item => item.period_kind === 'quarter') ? 'quarter' : 'annual'))
  const [quant, setQuant] = useState(false)
  const [allEvents, setAllEvents] = useState(false)
  const periods = data.statements.filter(item => item.period_kind === kind)
  const statement = periods.find(item => item.id === selected) ?? periods[0]
  const prior = statement && periods.find(item => {
    const days = (Date.parse(statement.period_end) - Date.parse(item.period_end)) / 86400000
    return days >= 350 && days <= 380
  })
  const names = ['revenue', 'gross_profit', 'operating_income', 'net_income']
  const title = `How ${company} makes money`
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
    <div className="performance-title"><h3>{title}</h3>{statement && <p>{kind === 'quarter' ? 'Fiscal quarter' : 'Fiscal year'} ended {statement.period_end} · {data.currency}</p>}</div>
    {!statement && <p>No comparable consolidated statement has been published for this period type and research date.</p>}
    {statement && <>
      <div className="performance-kpis">
        {names.map(key => {
          const margin = statement.margins[key]
          const change = annualChange(statement.values[key]?.value, prior?.values[key]?.value)
          return <div key={key}><span>{data.labels[key]}</span>
            <strong>{statement.values[key] ? compactMoney(statement.values[key].value, data.currency) : 'Unavailable'}</strong>
            <small>{margin !== undefined ? `${pct(margin)} margin` : 'Total reported revenue'}
              {change !== null && ` · ${change >= 0 ? '+' : ''}${pct(change)} YoY`}</small></div>
        })}
      </div>
      {statement.chart_status === 'available' ? <IncomeSankey key={statement.id} statement={statement} prior={prior} labels={data.labels}
        currency={data.currency} title={title} quant={quant} /> : <p role="status">{chartIssue}
          {statement.missing.length > 0 && ` Missing: ${statement.missing.map(key => data.labels[key]).join(', ')}.`}</p>}
      <p className="performance-summary">{margin != null && <>Operating profit was {pct(margin)} of revenue. </>}
        {margin != null && priorMargin != null && prior && (margin === priorMargin ? <>Operating margin was unchanged from the comparable period ending {prior.period_end}. </> :
          <>That is {Math.abs((margin - priorMargin) * 100).toFixed(1)} percentage points {margin > priorMargin ? 'higher than' : 'lower than'} the comparable period ending {prior.period_end}. </>)}
        {statement.values.nonoperating?.value !== undefined && statement.values.nonoperating.value !== 0 && <>{compactMoney(Math.abs(statement.values.nonoperating.value), data.currency)} {statement.values.nonoperating.value < 0 ? 'in net nonoperating expense reduced' : 'in net nonoperating income increased'} profit before tax.</>}
      </p>
      {!statement.revenue_sources?.length && <p className="performance-source">{data.segment_status}</p>}
      <details className="statement-disclosure" open={quant || statement.chart_status !== 'available'}><summary>Income statement and filing evidence</summary>
      {statement.revenue_sources?.length ? <div className="table-wrap"><table className="statement-table">
        <caption>Reported revenue sources</caption>
        <thead><tr><th scope="col">Category</th><th scope="col">Revenue</th>{quant && <th scope="col">XBRL concept and dimensions</th>}</tr></thead>
        <tbody>{statement.revenue_sources.map(row => <tr key={row.id}><th scope="row">{row.label}</th><td>{cash(row.amount, data.currency)}</td>
          {quant && <td>{[row.concept,row.segment,row.product].filter(Boolean).join(' · ')}</td>}</tr>)}</tbody>
      </table></div> : null}
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
      </details>
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
