import { cash, pct, scoreText } from './format'
import type { Discovery, Security } from './MarketViews'

const multiple = (value: number | null) => value == null ? '—' : `${value.toFixed(2)}×`
const groups: { label: string; metrics: [string, string, (row: Security) => string][] }[] = [
  { label: 'Research assessment', metrics: [
    ['Opportunity','Combined relative evidence on a 0–100 scale.',r=>scoreText(r.opportunity)],
    ['Fundamentals','Relative valuation, quality, growth and applicable cash flow evidence.',r=>scoreText(r.fundamental)],
    ['Technical','Price trend and momentum relative to the benchmark.',r=>scoreText(r.technical)],
    ['Risk resilience','Relative market and business risk evidence. Higher is more favorable under the scoring policy.',r=>scoreText(r.risk)],
  ]},
  { label: 'Valuation and business', metrics: [
    ['Fiscal-year P/E','Reported-share market value divided by positive fiscal-year net income.',r=>multiple(r.pe)],
    ['EV / EBITDA','Complete enterprise value divided by positive reported operating income plus D&A.',r=>multiple(r.ev_ebitda)],
    ['FCF yield','Annual operating cash flow less capital spending, divided by market value.',r=>pct(r.fcf_yield)],
    ['ROIC','Operating income after tax divided by average invested capital.',r=>pct(r.roic)],
    ['Revenue growth','Fiscal-year revenue change from a comparable prior period.',r=>pct(r.revenue_growth)],
    ['EBITDA margin','Operating income plus D&A divided by fiscal-year revenue.',r=>pct(r.ebitda_margin)],
    ['Debt / assets','Verified combined debt divided by total assets.',r=>pct(r.leverage)],
  ]},
  { label: 'Market conditions', metrics: [
    ['Price vs. SMA200','Last close relative to the 200-session simple moving average.',r=>pct(r.trend)],
    ['Three-month momentum','Price return over 63 observed trading sessions.',r=>pct(r.momentum)],
    ['Annualized volatility','Historical standard deviation of price returns, annualized.',r=>pct(r.volatility)],
    ['Maximum drawdown','Largest decline from a previous peak in the available price window.',r=>pct(r.drawdown)],
  ]},
]

export function Compare({ data, selected, toggle, openCompany, discover }: {
  data: Discovery; selected: string[]; toggle: (ticker: string) => void
  openCompany: (ticker: string) => void; discover: () => void
}) {
  const rows = selected.map(ticker => data.rows.find(row => row.ticker === ticker))
  const candidates = data.rows.filter(row => !selected.includes(row.ticker)).sort((a,b) => a.ticker.localeCompare(b.ticker))
  return <section className="panel comparison-view">
    <div className="section-heading"><div><h2>Compare alternatives</h2><p>Research cutoff {data.as_of}. Use the same published date when comparing companies.</p></div>
      <button type="button" onClick={discover}>Open Discover</button></div>
    <div className="toolbar">
      <label>Add company<select aria-label="Add comparison company" value="" disabled={selected.length >= 4}
        onChange={event => event.target.value && toggle(event.target.value)}>
        <option value="">Select a company</option>{candidates.map(row => <option key={row.ticker} value={row.ticker}>{row.ticker} · {row.name}</option>)}
      </select></label>
      <span className="pill">{selected.length} / 4 selected</span>
      {selected.map(ticker => <button key={ticker} type="button" aria-label={`Remove ${ticker} from comparison`} onClick={() => toggle(ticker)}>{ticker} ×</button>)}
    </div>
    {selected.length < 2 ? <p className="empty">Choose at least two companies here or add them from Discover and Company Research.</p> : <>
      <div className="table-wrap" data-companies={selected.length} tabIndex={0} aria-label="Company comparison results">
        <table><thead><tr><th scope="col">Evidence</th>{selected.map((ticker,index) => <th scope="col" key={ticker}>
          <button type="button" onClick={() => openCompany(ticker)}>{ticker}</button>
          <small>{rows[index]?.name ?? 'Unavailable at this cutoff'}</small>
        </th>)}</tr></thead>
        <tbody><tr><th scope="row">Last observed close</th>{selected.map((ticker,index) => <td key={ticker}>
          {cash(rows[index]?.price, rows[index]?.currency)}<small>{rows[index]?.price_date ?? 'No quote'}</small></td>)}</tr>
        {groups.flatMap(group => [
          <tr className="comparison-group" key={group.label}><th colSpan={selected.length+1}>{group.label}</th></tr>,
          ...group.metrics.map(([metric, meaning, render]) => <tr key={metric}><th scope="row">
            <details><summary>{metric}</summary><p>{meaning}</p></details></th>
            {selected.map((ticker,index) => <td key={ticker}>{rows[index] ? render(rows[index]!) : '—'}</td>)}</tr>),
        ])}</tbody></table>
      </div>
      <p>Unavailable values are shown as —. Differences in business models and supported reporting inputs can limit ratio comparisons.</p>
    </>}
  </section>
}
