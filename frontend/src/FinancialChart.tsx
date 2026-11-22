import { useEffect, useMemo, useRef, useState } from 'react'

export type Point = { date: string; close: number; volume?: number | null }
const ranges = { '1M': 21, '3M': 63, '6M': 126, '1Y': 252, '3Y': 756, '5Y': 1260, MAX: Infinity }
type Overlay = 'SMA20' | 'SMA50' | 'SMA200' | 'SPY' | 'Volume'

export function FinancialChart({ points, benchmark = [], label, currency, comparisonRebased = true, initialOverlays = [], initialRange = '1Y', showPriceTools = true }: {
  points: Point[]; benchmark?: Point[]; label: string; currency?: string | null; comparisonRebased?: boolean; initialOverlays?: Overlay[]; initialRange?: keyof typeof ranges; showPriceTools?: boolean
}) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const [range, setRange] = useState<keyof typeof ranges>(initialRange)
  const [overlays, setOverlays] = useState<Overlay[]>(initialOverlays)
  const [cursor, setCursor] = useState<number | null>(null)
  const [size, setSize] = useState(640)
  const data = points.slice(-ranges[range])
  const first = points.length - data.length
  const guides = useMemo(() => {
    const prefix = points.reduce<number[]>((acc, point) => { acc.push(acc.at(-1)! + point.close); return acc }, [0])
    const lookup = new Map(benchmark.map(point => [point.date, point.close]))
    const start = points.length - Math.min(points.length, ranges[range])
    const series = points.slice(start)
    const firstMatch = series.find(point => lookup.has(point.date))
    return overlays.flatMap<{ key: string; values: (number | null)[] }>(key => {
      if (key === 'Volume') return []
      if (key === 'SPY') {
        if (!firstMatch) return []
        const ratio = comparisonRebased ? firstMatch.close / lookup.get(firstMatch.date)! : 1
        return [{ key, values: series.map(point => lookup.has(point.date) ? lookup.get(point.date)! * ratio : null) }]
      }
      const days = Number(key.slice(3))
      return [{ key, values: series.map((_, index) => {
        const end = start + index + 1
        return end >= days ? (prefix[end] - prefix[end - days]) / days : null
      }) }]
    })
  }, [points, benchmark, range, overlays, comparisonRebased])

  useEffect(() => {
    if (!canvas.current) return
    const observer = new ResizeObserver(entries => setSize(entries[0].contentRect.width))
    observer.observe(canvas.current)
    return () => observer.disconnect()
  }, [points.length])

  useEffect(() => {
    const element = canvas.current
    if (!element || !data.length || size <= 0) return
    const ctx = element.getContext('2d')
    if (!ctx) return
    const ratio = window.devicePixelRatio
    const height = 228
    element.width = size * ratio
    element.height = height * ratio
    ctx.scale(ratio, ratio)
    const tokens = getComputedStyle(element)
    const color = (name: string) => tokens.getPropertyValue(name).trim()
    const left = 6, right = size - 52, top = 12, bottom = overlays.includes('Volume') ? 164 : 196
    const values = [...data.map(point => point.close), ...guides.flatMap(guide => guide.values.filter((v): v is number => v != null))]
    const low = Math.min(...values), high = Math.max(...values)
    const span = Math.max(high - low, Math.abs(high) * 0.01, 0.01)
    const y = (value: number) => bottom - (value - low) / span * (bottom - top)
    const x = (index: number) => left + (right - left) * index / Math.max(1, data.length - 1)
    ctx.font = '12px system-ui'
    for (let index = 0; index < 5; index++) {
      const value = low + span * index / 4
      ctx.beginPath(); ctx.moveTo(left, y(value)); ctx.lineTo(right, y(value))
      ctx.strokeStyle = color('--line'); ctx.stroke()
      ctx.fillStyle = color('--muted'); ctx.fillText(value.toLocaleString('en-US', { maximumFractionDigits: 0 }), right + 7, y(value) + 4)
    }
    const lines = [{ values: data.map(point => point.close), key: 'price' }, ...guides]
    const palette: Record<string, string> = { price: '--accent', SMA20: '--chart-blue', SMA50: '--chart-purple', SMA200: '--chart-green', SPY: '--muted' }
    if (overlays.includes('Volume')) {
      const volume = Math.max(1, ...data.map(point => point.volume ?? 0))
      ctx.fillStyle = color('--line-strong')
      data.forEach((point, index) => {
        if (point.volume != null) {
          const depth = point.volume / volume * 28
          ctx.fillRect(x(index), 202 - depth, Math.max(1, (right - left) / data.length - 1), depth)
        }
      })
    }
    lines.forEach(line => {
      ctx.beginPath()
      let open = false
      line.values.forEach((value, index) => {
        if (value == null) { open = false; return }
        if (open) ctx.lineTo(x(index), y(value))
        else ctx.moveTo(x(index), y(value))
        open = true
      })
      ctx.strokeStyle = color(palette[line.key]); ctx.lineWidth = line.key === 'price' ? 2 : 1.2; ctx.stroke()
    })
    ctx.fillStyle = color('--muted')
    const stamp = (day: string) => new Intl.DateTimeFormat('en', { month: 'short', year: '2-digit', timeZone: 'UTC' }).format(new Date(day))
    for (let tick = 0; tick <= (size < 460 ? 2 : 4); tick++) {
      const index = Math.round((data.length - 1) * tick / (size < 460 ? 2 : 4))
      if (data[index]) ctx.fillText(stamp(data[index].date), Math.min(right - 36, x(index)), 224)
    }
    if (cursor != null && data[cursor]) {
      ctx.beginPath(); ctx.moveTo(x(cursor), top); ctx.lineTo(x(cursor), bottom)
      ctx.strokeStyle = color('--muted'); ctx.stroke()
      ctx.beginPath(); ctx.arc(x(cursor), y(data[cursor].close), 4, 0, Math.PI * 2)
      ctx.fillStyle = color('--accent'); ctx.fill()
    }
  }, [data, cursor, guides, overlays, size])

  if (!points.length) return <div className="empty">No published price history for this date.</div>
  const active = data[cursor ?? data.length - 1]
  function toggle(key: Overlay) {
    setOverlays(current => current.includes(key) ? current.filter(v => v !== key) : [...current, key])
  }
  return <section className="panel price-chart">
    <div className="section-heading"><div><h2>{label}</h2><p>{showPriceTools ? 'Daily closes' : 'Daily marked values'}{currency ? ` · ${currency}` : ''}</p></div>
      <div className="segments" aria-label="Chart timeframe">{Object.keys(ranges).map(key => <button key={key} type="button" aria-pressed={range === key} onClick={() => { setRange(key as keyof typeof ranges); setCursor(null) }}>{key}</button>)}</div>
    </div>
    <div className="chart-reading"><strong>{active?.close.toLocaleString('en-US', { maximumFractionDigits: 2 })}</strong><span>{active?.date}</span></div>
    <canvas ref={canvas} role="img" aria-label={`${label}. ${data.length} observations from ${data[0]?.date} to ${data.at(-1)?.date}.`}
      onPointerMove={event => {
        const bounds = event.currentTarget.getBoundingClientRect()
        setCursor(Math.round(Math.max(0, Math.min(1, (event.clientX - bounds.left - 6) / Math.max(1, bounds.width - 58))) * (data.length - 1)))
      }} onPointerLeave={() => setCursor(null)} />
    <label className="chart-slider">Inspect session<input type="range" min="0" max={data.length - 1} value={cursor ?? data.length - 1}
      aria-valuetext={`${active?.date}, ${active?.close}`} onChange={event => setCursor(Number(event.target.value))} /></label>
    <div className="segments" aria-label="Chart overlays">{(['SMA20', 'SMA50', 'SMA200', 'SPY', 'Volume'] as const).map(key =>
      (showPriceTools || key === 'SPY') && (key !== 'SPY' || benchmark.length > 0) && (key !== 'Volume' || data.some(point => point.volume != null)) &&
      <button key={key} type="button" aria-pressed={overlays.includes(key)} onClick={() => toggle(key)}>{key}</button>)}</div>
    {overlays.includes('SPY') && comparisonRebased && <small>SPY is rebased to the company close on the first shared session.</small>}
    {first === 0 && <small>Showing all observations available for this timeframe.</small>}
  </section>
}
