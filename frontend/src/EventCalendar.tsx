import { useEffect, useMemo, useState } from 'react'

type Event = { id: string; ticker: string; kind: string; label: string; event_date: string; event_at: string | null; timezone: string | null;
  status: string; source: string; source_url: string | null; statement_period_end: string | null }
type Window = { start: string; end: string; events: Event[] }
const day = (value: Date) => value.toISOString().slice(0, 10)
const move = (date: string, days: number) => day(new Date(Date.parse(date + 'T12:00:00Z') + days * 86400000))
const firstOfMonth = (date: string, offset = 0) => {
  const [y, m] = date.split('-').map(Number)
  return day(new Date(Date.UTC(y, m - 1 + offset, 1)))
}
const current = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
const kinds = { earnings_release: 'Earnings announcements', earnings_call: 'Earnings calls', investor_event: 'Investor events',
  sec_filing: 'SEC filings', dividend: 'Dividends', corporate_action: 'Corporate actions' }
const labels: Record<string, string> = { confirmed: 'Confirmed', estimated: 'Estimated', historical: 'Historical', unverified_occurrence: 'Occurrence unverified' }

export function EventCalendar({ watchlist, companies, cutoff }: { watchlist: string[]; companies: { ticker: string; name: string }[]; cutoff: string }) {
  const knownThrough = cutoff || current()
  const [view, setView] = useState<'month' | 'week' | 'upcoming'>('month')
  const [anchor, setAnchor] = useState(knownThrough)
  const [filter, setFilter] = useState('')
  const [kind, setKind] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [retry, setRetry] = useState(0)
  const weekStart = move(anchor, -((new Date(anchor + 'T12:00:00Z').getUTCDay() + 6) % 7))
  const start = view === 'month' ? firstOfMonth(anchor) : view === 'week' ? weekStart : knownThrough
  const end = view === 'month' ? move(firstOfMonth(anchor, 1), -1) : view === 'week' ? move(start, 6) : move(start, 62)
  const url = useMemo(() => {
    const params = new URLSearchParams({ start, end, as_of: knownThrough })
    if (kind) params.set('kind', kind)
    if (filter === 'watchlist') {
      for (const ticker of watchlist) if (companies.some(company => company.ticker === ticker)) params.append('ticker', ticker)
      if (!params.has('ticker')) return null
    } else if (filter) params.append('ticker', filter)
    return `/api/analytics/calendar?${params}`
  }, [start, end, knownThrough, kind, filter, watchlist, companies])
  const [result, setResult] = useState<{ url: string; data?: Window; error?: string }>()
  const visible = result?.url === url ? result : undefined
  useEffect(() => {
    if (!url) return
    const controller = new AbortController()
    fetch(url, { signal: controller.signal }).then(async response => {
      if (!response.ok) throw Error('The local calendar could not be read.')
      return response.json() as Promise<Window>
    }).then(data => setResult({ url, data }), error => {
      if (!controller.signal.aborted) setResult({ url, error: String(error.message ?? error) })
    })
    return () => controller.abort()
  }, [url, retry])

  const days = Array.from({ length: 1 + (Date.parse(end) - Date.parse(start)) / 86400000 }, (_, index) => move(start, index))
  const offset = view === 'month' ? (new Date(start + 'T12:00:00Z').getUTCDay() + 6) % 7 : 0
  const catalog = new Map(companies.map(company => [company.ticker, company.name]))
  const events = visible?.data?.events ?? []
  const entries = selected ? events.filter(event => event.event_date === selected) : events

  function page(amount: number) {
    setAnchor(view === 'month' ? firstOfMonth(anchor, amount) : move(anchor, amount * 7))
    setSelected(null)
  }
  return <section className="panel calendar-view">
    <h2>Financial events</h2>
    <p>Events known by {knownThrough}. Dates show the source’s precision. Future events appear when a source has reported them.</p>
    <div className="toolbar">
      <div role="group" aria-label="Calendar view">{(['month','week','upcoming'] as const).map(key =>
        <button type="button" key={key} aria-pressed={view === key} onClick={() => { setView(key); setSelected(null) }}>{key[0].toUpperCase()+key.slice(1)}</button>)}</div>
      <label>Company <select aria-label="Company" value={filter} onChange={e => { setFilter(e.target.value); setSelected(null) }}>
        <option value="">All tracked companies</option><option value="watchlist">Watchlist</option>
        {[...companies].sort((a,b) => a.ticker.localeCompare(b.ticker)).map(company => <option key={company.ticker} value={company.ticker}>{company.ticker} · {company.name}</option>)}
      </select></label>
      <label>Event type <select aria-label="Event type" value={kind} onChange={e => { setKind(e.target.value); setSelected(null) }}><option value="">All event types</option>
        {Object.entries(kinds).map(([key, title]) => <option key={key} value={key}>{title}</option>)}</select></label>
    </div>
    <div className="toolbar" aria-label="Calendar dates">
      {view !== 'upcoming' && <button type="button" onClick={() => page(-1)} aria-label="Previous period">‹</button>}
      <strong>{start} to {end}</strong>
      {view !== 'upcoming' && <button type="button" onClick={() => page(1)} aria-label="Next period">›</button>}
    </div>
    {visible?.error ? <p role="alert">{visible.error} <button type="button" onClick={() => { setResult(undefined); setRetry(retry + 1) }}>Retry</button></p>
      : !url ? <p>Add a company to your watchlist to filter its events here.</p>
      : !visible?.data ? <p role="status">Loading published events…</p>
      : <>
        {view !== 'upcoming' && <div className="calendar-grid" aria-label="Days in the selected period">
          {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map(name => <small key={name}>{name}</small>)}
          {Array.from({length:offset},(_,index) => <span aria-hidden="true" key={`empty-${index}`} />)}
          {days.map(date => {
            const count = events.filter(event => event.event_date === date).length
            return <button key={date} type="button" onClick={() => setSelected(selected === date ? null : date)}
              aria-label={`${date}, ${count} events`} aria-pressed={selected === date}><span>{Number(date.slice(-2))}</span>
              {count > 0 && <small>{count}</small>}</button>
          })}
        </div>}
        <h3>{selected ? `Events on ${selected}` : view === 'upcoming' ? 'Upcoming events' : 'Events in this period'}</h3>
        {!entries.length ? <p>No matching published events.</p> : <ul className="calendar-list">{entries.map(event => {
          const timestamp = event.event_at && event.timezone ? new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short', timeZone: event.timezone }).format(new Date(event.event_at)) : event.event_date
          const params = new URLSearchParams({ as_of: knownThrough })
          if (event.statement_period_end) { params.set('financial', '1'); params.set('period', `quarter:${event.statement_period_end}`) }
          return <li key={event.id}><div><strong>{event.ticker} · {event.label}</strong><span>{catalog.get(event.ticker)}</span>
            <span>{timestamp}{event.timezone && ` · ${event.timezone}`} · {labels[event.status] ?? event.status}</span></div>
            <div><a href={`/?${params}#company-research/${encodeURIComponent(event.ticker)}`}>{event.statement_period_end ? 'Financial performance' : 'Company research'}</a>
              {event.source_url?.startsWith('https://') ? <a href={event.source_url} target="_blank" rel="noreferrer">{event.source}</a> : <span>{event.source}</span>}</div>
          </li>
        })}</ul>}
      </>}
  </section>
}
