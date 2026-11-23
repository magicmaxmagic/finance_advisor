import { sankey, sankeyLinkHorizontal, type SankeyNode } from 'd3-sankey'
import { useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { cash, pct } from './format'
import { annualChange, compactMoney } from './financialAmounts'

export type RevenueSource = {
  id: string; label: string; amount: number; concept: string
  segment: string | null; product: string | null; source_url: string; source_sha256: string
}
export type IncomeLink = { source: string; target: string; amount: number; tone: 'cost' | 'adjustment' | 'retained' | 'revenue' }
export type IncomeStatement = {
  id: string; period_kind: 'annual' | 'quarter'; period_start: string; period_end: string
  accession: string; filed_date: string; source_url: string | null; accepted_at: string | null
  chart_status: 'available' | 'incomplete' | 'unreconciled' | 'signed_statement'
  values: Record<string, { value: number; source: string }>
  checks: { label: string; status: string; difference: number | null }[]
  margins: Record<string, number>; missing: string[]; links: IncomeLink[]
  revenue_sources?: RevenueSource[]
}

type Node = { id: string; label: string; stage: number; order: number; tone: IncomeLink['tone'] }
type Stream = { tone: IncomeLink['tone'] }
const short: Record<string, string> = {
  gross_profit: 'Gross profit',
  research: 'Research & development',
  sales_marketing: 'Sales & marketing',
  administration: 'General & admin',
  selling_general_admin: 'Selling, general & admin',
  operating_expenses: 'Operating expenses',
  nonoperating_gain: 'Nonoperating income',
  nonoperating_loss: 'Nonoperating expense',
  tax_benefit: 'Income tax benefit',
}
const explanation: Record<string, string> = {
  revenue: 'Total revenue reported for the period, including applicable revenue adjustments.',
  gross_profit: 'Revenue remaining after the company pays its direct costs of providing products and services.',
  operating_income: 'Profit from operations after operating expenses. Financing and other nonoperating items follow this subtotal.',
  net_income: 'Reported consolidated income remaining after nonoperating items and income tax.',
  cost_of_revenue: 'The direct cost of delivering the products and services sold during the period.',
  pretax: 'Operating income after the net nonoperating result and before income tax.',
  nonoperating_gain: 'Net income recorded outside operating activities that increased pretax income.',
  nonoperating_loss: 'Net expense recorded outside operating activities that reduced pretax income.',
  tax: 'The income tax provision for this reporting period.',
  tax_benefit: 'A reported tax benefit increased the amount of net income.',
}
const stageIds = [
  ['revenue'], ['gross_profit', 'cost_of_revenue'],
  ['operating_income', 'nonoperating_gain', 'research', 'sales_marketing', 'administration', 'selling_general_admin', 'operating_expenses'],
  ['pretax', 'tax_benefit', 'nonoperating_loss'], ['net_income', 'tax'],
]
const accent = { revenue: '#8da2bd', retained: '#65d6a2', cost: '#ed8b85', adjustment: '#99a4b5' }
const fill = { revenue: '#7089ab', retained: '#58c693', cost: '#d0726d', adjustment: '#657286' }
const SVG_WIDTH = 1260, SVG_HEIGHT = 535

function lines(text: string, max: number) {
  const wrapped: string[] = []
  for (const word of text.split(' ')) {
    const previous = wrapped.at(-1)
    if (previous && previous.length + word.length + 1 <= max) wrapped[wrapped.length - 1] = `${previous} ${word}`
    else wrapped.push(word)
  }
  return wrapped
}

function buildLayout(statement: IncomeStatement, labels: Record<string, string>) {
  const sources = statement.revenue_sources?.filter(row => row.amount > 0) ?? []
  const stages = sources.length ? [sources.map(row => row.id), ...stageIds] : stageIds
  const observed = new Set(statement.links.flatMap(link => [link.source, link.target]))
  const tiles = new Map(sources.map(row => [row.id, row.label]))
  const nodes = stages.flatMap((stage, position) => stage.filter(key => observed.has(key)).map((id, order): Node => ({
    id, label: tiles.get(id) ?? short[id] ?? labels[id] ?? id, stage: position, order,
    tone: tiles.has(id) || id === 'revenue' ? 'revenue' : ['cost_of_revenue', 'tax', 'research', 'sales_marketing',
      'administration', 'selling_general_admin', 'operating_expenses'].includes(id) ? 'cost'
      : ['tax_benefit', 'nonoperating_gain', 'nonoperating_loss'].includes(id) ? 'adjustment' : 'retained',
  })))
  const generator = sankey<Node, Stream>().nodeId(node => node.id).nodeAlign(node => node.stage)
    .nodeSort((a, b) => a.order - b.order).nodeWidth(7).nodePadding(45).iterations(0)
    .extent([[sources.length ? 184 : 28, 120], [1100, 493]])
  const geometry = generator({ nodes, links: statement.links.map(link => ({ ...link, value: link.amount })) })
  const main = new Set(stageIds.map(ids => ids[0]))
  const size = (node: SankeyNode<Node, Stream>) => (node.y1 ?? 0) - (node.y0 ?? 0)
  const largestPrincipal = Math.max(...geometry.nodes.filter(node => main.has(node.id)).map(size))
  const largestBranch = Math.max(...stageIds.map(ids =>
    geometry.nodes.filter(node => ids.slice(1).includes(node.id)).reduce((sum, node) => sum + size(node), 0)))
  // A single scale keeps every flow proportional while reserving the label and branch lanes.
  const shrink = Math.min(1, 126 / largestPrincipal, 130 / largestBranch)
  for (const node of geometry.nodes) node.y1 = (node.y0 ?? 0) + size(node) * shrink
  for (const link of geometry.links) link.width = (link.width ?? 0) * shrink
  for (const position of stages.keys()) {
    if (position === 0 && sources.length) continue
    const column = geometry.nodes.filter(node => node.stage === position)
    const key = column.find(node => main.has(node.id))
    if (!key) continue
    const branchNodes = column.filter(node => node !== key)
    const place = (node: SankeyNode<Node, Stream>, top: number) => {
      const height = size(node)
      node.y0 = top
      node.y1 = top + height
      return height
    }
    const principal = size(key)
    place(key, 180 - principal / 2)
    const branchesHeight = branchNodes.reduce((sum, node) => sum + size(node), 0)
    let y = Math.max(286, (key.y1 ?? 0) + 55)
    const gap = branchNodes.length > 1
      ? Math.min(65, (SVG_HEIGHT - 20 - y - branchesHeight) / (branchNodes.length - 1)) : 0
    for (const node of branchNodes) y += place(node, y) + gap
  }
  generator.update(geometry)
  return { ...geometry, stages: sources.length
    ? ['Reported revenue sources', 'Revenue', 'Gross profit', 'Operating income', 'Pretax income', 'Net income']
    : ['Revenue', 'Gross profit', 'Operating income', 'Pretax income', 'Net income'] }
}

function keyboardAction(event: KeyboardEvent, choose: () => void) {
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); choose() }
}

export function IncomeSankey({ statement, prior, labels, currency, title, quant }: {
  statement: IncomeStatement; prior?: IncomeStatement; labels: Record<string, string>; currency: string; title: string; quant: boolean
}) {
  const layout = useMemo(() => buildLayout(statement, labels), [statement, labels])
  const [active, setActive] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [hoveredLink, setHoveredLink] = useState<string | null>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  const svg = useRef<SVGSVGElement>(null)
  const detailed = layout.nodes.find(node => node.id === selected)
  const current = hoveredLink ? null : layout.nodes.find(node => node.id === active) ?? detailed
  const flow = sankeyLinkHorizontal<Node, Stream>()
  const sources = statement.revenue_sources ?? []
  const label = (key: string) => sources.find(row => row.id === key)?.label ?? short[key] ?? labels[key] ?? key
  const amountFor = (node: SankeyNode<Node, Stream>) => node.value ?? 0
  const tooltip = (node: SankeyNode<Node, Stream>) => `${node.label} · ${cash(amountFor(node), currency)}`
  const source = detailed && sources.find(row => row.id === detailed.id)
  const financialKey = detailed && ({ tax_benefit: 'tax', nonoperating_gain: 'nonoperating', nonoperating_loss: 'nonoperating' }[detailed.id] ?? detailed.id)
  const provenance = financialKey ? statement.values[financialKey]?.source : undefined
  const visual = (expanded = false) => <div className="income-flow-wrap" tabIndex={0} role="region" aria-label="Scrollable income statement flow chart">
    <svg ref={expanded ? undefined : svg} xmlns="http://www.w3.org/2000/svg" viewBox={`0 0 ${SVG_WIDTH} ${SVG_HEIGHT}`}
      role="group" aria-label="Revenue, spending, and profit Sankey">
      <rect width={SVG_WIDTH} height={SVG_HEIGHT} rx="13" fill="#151b23" />
      {layout.stages.map((heading, index) => {
        const x = layout.nodes.find(node => node.stage === index)?.x0 ?? 0
        return <text key={heading} x={index === 0 && sources.length ? 20 : x} y={25} className="sankey-stage"
          fill="#aebcce" fontFamily="system-ui, sans-serif" fontSize="10" letterSpacing="1.25">{heading.toUpperCase()}</text>
      })}
      {layout.links.map(link => {
        const start = link.source as SankeyNode<Node, Stream>, end = link.target as SankeyNode<Node, Stream>
        const key = `${start.id}-${end.id}`
        const highlighted = key === hoveredLink || start.id === current?.id || end.id === current?.id
        const fading = Boolean(hoveredLink || current) && !highlighted
        const amount = cash(link.value, currency)
        return <path key={key} d={flow(link) ?? undefined} fill="none" stroke={fill[link.tone]}
          opacity={fading ? .22 : highlighted ? .92 : .62} strokeWidth={link.width}
          tabIndex={0} role="button" aria-pressed={highlighted} data-source={start.id} data-target={end.id} data-amount={link.value}
          className="sankey-stream" aria-label={`${label(start.id)} to ${label(end.id)}, ${amount}`}
          onMouseEnter={() => setHoveredLink(key)} onMouseLeave={() => setHoveredLink(null)}
          onFocus={() => setHoveredLink(key)} onBlur={() => setHoveredLink(null)}
          onClick={() => setSelected(end.id)} onKeyDown={event => keyboardAction(event, () => setSelected(end.id))}>
          <title>{label(start.id)} → {label(end.id)} · {amount}</title>
        </path>
      })}
      {layout.nodes.map(node => {
        const baseline = node.y0 ?? 0, height = (node.y1 ?? 0) - baseline, x = node.x0 ?? 0
        const top = node.order === 0 && !sources.some(row => row.id === node.id)
        const left = sources.some(row => row.id === node.id)
        const below = node.id === 'cost_of_revenue'
        const alignLeftOfNode = left || below
        const width = left ? 155 : 143
        const groups = lines(node.label, left ? 24 : 19)
        const labelX = alignLeftOfNode ? x - 12 : x + 12
        const labelY = below ? baseline + height + 18
          : left ? baseline + Math.min(height / 2, 15) - (groups.length - 1) * 7 : top ? 61 : baseline - 21 - (groups.length - 1) * 13
        const amountY = alignLeftOfNode || top ? labelY + 17 + (groups.length - 1) * 13 : baseline - 7
        const margin = statement.margins[node.id]
        const yoy = annualChange(statement.values[node.id]?.value, prior?.values[node.id]?.value)
        return <g key={node.id} data-node={node.id} tabIndex={0} role="button" aria-label={tooltip(node)}
          aria-pressed={selected === node.id} className="sankey-node" onMouseEnter={() => setActive(node.id)}
          onMouseLeave={() => setActive(null)} onFocus={() => setActive(node.id)} onBlur={() => setActive(null)}
          onClick={() => setSelected(node.id)} onKeyDown={event => keyboardAction(event, () => setSelected(node.id))}>
          <title>{tooltip(node)}</title>
          <rect x={x} y={baseline} width="7" height={height} rx="2" fill={accent[node.tone]} />
          {top
            ? <text x={labelX} y={labelY} fill={accent[node.tone]} className="sankey-key-value" fontFamily="system-ui, sans-serif" fontSize="22" fontWeight="700">
              {compactMoney(amountFor(node), currency)}
            </text>
            : <>
              <text x={labelX} y={labelY} textAnchor={alignLeftOfNode ? 'end' : 'start'} className="sankey-label"
                fill="#edf2f7" fontFamily="system-ui, sans-serif" fontSize="11" fontWeight="600">
                {groups.map((group, index) => <tspan key={group} x={labelX} dy={index ? 13 : 0}>{group}</tspan>)}
              </text>
              <text x={labelX} y={amountY} textAnchor={alignLeftOfNode ? 'end' : 'start'} className="sankey-label" fill={accent[node.tone]}
                fontFamily="system-ui, sans-serif" fontSize="10">
                {compactMoney(amountFor(node), currency)}
              </text>
            </>}
          {top && <text x={labelX} y={86} fill="#b9c7d8" fontFamily="system-ui, sans-serif" fontSize="10">
            {margin !== undefined ? `${pct(margin)} margin` : yoy !== null ? `${yoy >= 0 ? '+' : ''}${pct(yoy)} YoY` : ''}
          </text>}
          {top && margin !== undefined && yoy !== null && <text x={labelX} y={102} fill="#aebcce" fontFamily="system-ui, sans-serif" fontSize="10">
            {yoy >= 0 ? '+' : ''}{pct(yoy)} YoY
          </text>}
          <rect x={alignLeftOfNode ? labelX - width : labelX} y={Math.min(labelY, amountY) - 12} width={width}
            height={top ? 61 : (groups.length + 1) * 16} fill="transparent" />
        </g>
      })}
    </svg>
  </div>

  function download() {
    if (!svg.current) return
    const copy = svg.current.cloneNode(true) as SVGSVGElement
    copy.querySelectorAll('[tabindex]').forEach(node => node.removeAttribute('tabindex'))
    const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(copy)], { type: 'image/svg+xml' }))
    const link = document.createElement('a'); link.href = url; link.download = `finlab-income-${statement.period_end}.svg`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return <figure className="financial-sankey">
    <div className="sankey-tools">
      <ul aria-label="Chart legend">
        {([['revenue', 'Revenue'], ['retained', 'Profit'], ['cost', 'Expense'], ['adjustment', 'Adjustments']] as const).map(([tone, name]) =>
          <li key={tone}><i style={{ background: accent[tone] }} />{name}</li>)}
      </ul>
      <div><button type="button" onClick={() => dialog.current?.showModal()}>Expand chart</button>
        <button type="button" onClick={download}>Download SVG</button></div>
    </div>
    {visual()}
    <figcaption className="sankey-caption">
      <span>{hoveredLink ? (() => {
        const link = layout.links.find(item => `${(item.source as Node).id}-${(item.target as Node).id}` === hoveredLink)
        return link ? `${label((link.source as Node).id)} → ${label((link.target as Node).id)} · ${cash(link.value, currency)}` : ''
      })() : current ? tooltip(current) : 'Wider flows represent larger amounts. Select a flow or label for the underlying evidence.'}</span>
      <span className="sankey-swipe">Scroll to see the full chart.</span>
    </figcaption>
    {detailed && <aside className="sankey-evidence" aria-label="Selected financial flow">
      <div><h4>{detailed.label}</h4><strong>{cash(amountFor(detailed), currency)}</strong></div>
      <p>{explanation[detailed.id] ?? (source ? 'A revenue category disclosed in the same filing and reconciled to total revenue.'
        : 'An operating expense disclosed in this consolidated income statement.')}</p>
      {quant && <p>{source?.concept ?? provenance ?? 'Derived from the reconciled statement'} · Period ended {statement.period_end}</p>}
      <button type="button" onClick={() => setSelected(null)}>Clear selected flow</button>
    </aside>}
    <dialog className="sankey-dialog" ref={dialog} aria-label="Expanded financial Sankey">
      <header><h3>{title}</h3><button type="button" onClick={() => dialog.current?.close()}>Close expanded chart</button></header>
      {visual(true)}
      {(hoveredLink || current) && <p className="sankey-caption">{current ? tooltip(current) : 'See the exact flow amount in its accessible label.'}</p>}
    </dialog>
  </figure>
}
