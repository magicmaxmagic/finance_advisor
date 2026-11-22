import { useEffect, useMemo, useRef, useState } from 'react'
import { ColorType, createChart, HistogramSeries, LineSeries, type Time, type UTCTimestamp } from 'lightweight-charts'

export type Point = { date: string; close: number; volume?: number | null }
const ranges = { '1M': 21, '3M': 63, '6M': 126, '1Y': 252, '3Y': 756, '5Y': 1260, MAX: Infinity }
type Overlay = 'SMA20' | 'SMA50' | 'SMA200' | 'SPY' | 'Volume'
const palette: Record<Overlay, string> = { SMA20: '--chart-blue', SMA50: '--chart-purple', SMA200: '--chart-green', SPY: '--muted', Volume: '--line-strong' }
const utc = (date: string) => Math.floor(Date.parse(date) / 1000) as UTCTimestamp
const noPoints: Point[] = []

export function FinancialChart({
  points, benchmark = noPoints, label, currency, comparisonRebased = true, initialOverlays = [],
  initialRange = '1Y', showPriceTools = true, units, comparisonLabel = 'SPY',
}: {
  points: Point[]; benchmark?: Point[]; label: string; currency?: string | null
  comparisonRebased?: boolean; initialOverlays?: Overlay[]; initialRange?: keyof typeof ranges
  showPriceTools?: boolean; units?: string; comparisonLabel?: string
}) {
  const container = useRef<HTMLDivElement>(null)
  const [range, setRange] = useState<keyof typeof ranges>(initialRange)
  const [overlays, setOverlays] = useState<Overlay[]>(initialOverlays)
  const [cursor, setCursor] = useState<number | null>(null)
  const ordered = useMemo(() => [...new Map(points.filter(point => Number.isFinite(point.close) && Number.isFinite(utc(point.date)))
    .map(point => [point.date, point])).values()].sort((a,b) => a.date.localeCompare(b.date)), [points])
  const data = useMemo(() => ordered.slice(-ranges[range]), [ordered, range])
  const overlaySeries = useMemo(() => {
    const prefix = ordered.reduce<number[]>((acc, point) => { acc.push(acc[acc.length - 1] + point.close); return acc }, [0])
    const byDate = new Map(benchmark.filter(point => Number.isFinite(point.close)).map(point => [point.date, point.close]))
    const matched = data.find(point => byDate.has(point.date) && byDate.get(point.date) !== 0)
    const first = ordered.length - data.length
    return overlays.filter(key => key !== 'Volume').map(key => {
      const factor = comparisonRebased && matched ? matched.close / byDate.get(matched.date)! : 1
      return { key, values: data.map((point,index) => {
        if (key === 'SPY') return byDate.has(point.date) ? byDate.get(point.date)! * factor : null
        const width = Number(key.slice(3)), end = first + index + 1
        return end >= width ? (prefix[end] - prefix[end-width]) / width : null
      }) }
    })
  }, [data, ordered, benchmark, overlays, comparisonRebased])
  const active = data[cursor ?? data.length - 1]
  const format = useMemo(() => Intl.NumberFormat('en-US', currency
    ? { style: 'currency', currency, notation: showPriceTools ? 'standard' : 'compact', maximumFractionDigits: 2 }
    : { maximumFractionDigits: 2 }), [currency, showPriceTools])

  useEffect(() => {
    if (!container.current || !data.length) return
    const target = container.current
    const css = getComputedStyle(target)
    const color = (token: string) => css.getPropertyValue(token).trim()
    const chart = createChart(target, {
      width: target.clientWidth, height: 260,
      layout: { background: { type: ColorType.Solid, color: color('--surface') }, textColor: color('--muted') },
      grid: { vertLines: { color: color('--line'), visible: false }, horzLines: { color: color('--line') } },
      crosshair: { vertLine: { color: color('--muted') }, horzLine: { color: color('--muted') } },
      rightPriceScale: { borderColor: color('--line'), minimumWidth: 56 },
      timeScale: { borderColor: color('--line') },
      localization: { priceFormatter: (n: number) => format.format(n) + (units ?? '') },
    })
    const main = chart.addSeries(LineSeries, { color: color('--accent'), lineWidth: 2, priceLineVisible: false })
    main.setData(data.map(point => ({ time: utc(point.date), value: point.close })))
    overlaySeries.forEach(series => {
      if (!series.values.some(n => n != null)) return
      const line = chart.addSeries(LineSeries, { color: color(palette[series.key]), lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
      line.setData(data.map((point,index) => {
        const value = series.values[index]
        return value == null ? { time: utc(point.date) } : { time: utc(point.date), value }
      }))
    })
    if (overlays.includes('Volume')) {
      const volume = chart.addSeries(HistogramSeries, { color: color('--line-strong'), priceFormat: { type: 'volume' }, priceScaleId: '', priceLineVisible: false, lastValueVisible: false })
      volume.priceScale().applyOptions({ scaleMargins: { top: .8, bottom: 0 } })
      volume.setData(data.map(point => point.volume == null ? { time: utc(point.date) } : { time: utc(point.date), value: point.volume }))
    }
    const indexByTime: Map<Time,number> = new Map(data.map((point,index) => [utc(point.date),index]))
    chart.subscribeCrosshairMove(event => setCursor(event.time == null ? null : indexByTime.get(event.time) ?? null))
    chart.timeScale().fitContent()
    const resize = new ResizeObserver(([entry]) => { if (entry.contentRect.width > 0) chart.resize(entry.contentRect.width,260) })
    resize.observe(target)
    return () => { resize.disconnect(); chart.remove() }
  }, [data, format, units, overlaySeries, overlays])

  if (!data.length) return <div className="empty">No published observations for this date.</div>
  return <section className="panel price-chart">
    <div className="section-heading"><div><h2>{label}</h2><p>{showPriceTools ? 'Observed closes' : 'Observed values'}{currency ? ` · ${currency}` : ''}</p></div>
      {showPriceTools && <div className="segments" aria-label="Chart timeframe">{(Object.keys(ranges) as (keyof typeof ranges)[]).map(key =>
        <button key={key} type="button" aria-pressed={range === key} onClick={() => { setRange(key); setCursor(null) }}>{key}</button>)}</div>}
    </div>
    <div className="chart-reading"><strong>{active ? format.format(active.close) + (units ?? '') : 'Unavailable'}</strong><span>{active?.date}</span>
      {overlaySeries.map(series => {
        const offset = cursor ?? data.length-1
        const n = series.values[offset]
        return n != null ? <span key={series.key} style={{ color: `var(${palette[series.key]})` }}>
          {series.key === 'SPY' ? comparisonLabel : series.key} {format.format(n)}{units}</span> : null
      })}
    </div>
    <div className="chart-canvas" ref={container} role="img"
      aria-label={`${label}, ${data.length} observed dates from ${data[0]?.date} to ${data.at(-1)?.date}. Use the session slider for precise values.`} />
    <label className="chart-slider">Inspect session<input type="range" min="0" max={data.length-1} value={cursor ?? data.length-1}
      aria-valuetext={`${active?.date}, ${active?.close}`} onChange={event => setCursor(Number(event.target.value))} /></label>
    <div className="segments" aria-label="Chart overlays">{(['SMA20','SMA50','SMA200','SPY','Volume'] as const).map(key =>
      (showPriceTools || key === 'SPY') && (key !== 'SPY' || benchmark.length > 0) &&
      (key !== 'Volume' || data.some(point => point.volume != null)) &&
      <button key={key} type="button" aria-pressed={overlays.includes(key)}
        onClick={() => { setOverlays(current => current.includes(key) ? current.filter(item => item !== key) : [...current,key]); setCursor(null) }}>
        {key === 'SPY' ? comparisonLabel : key}</button>)}</div>
    {overlays.includes('SPY') && comparisonRebased && <small>{comparisonLabel} closes are rebased to the stock’s first shared observation. Price return excludes reinvested dividends.</small>}
    <small>Charting by <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView</a>.</small>
  </section>
}
