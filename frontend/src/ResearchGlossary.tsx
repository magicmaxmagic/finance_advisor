import { useRef, useState } from 'react'
import { financialGlossary } from './financialGlossary'

export function ResearchGlossary() {
  const [firstVisit, setFirstVisit] = useState(() => localStorage.getItem('finlab-research-guide-seen') !== 'true')
  const guide = useRef<HTMLDetailsElement>(null)

  function markSeen() {
    localStorage.setItem('finlab-research-guide-seen', 'true')
    setFirstVisit(false)
  }

  return <section className="panel research-glossary" aria-label="Company research help">
    {firstVisit && <div className="section-heading">
      <p>New to company research?</p>
      <div className="segments">
        <button type="button" onClick={() => {
          if (guide.current) {
            guide.current.open = true
            guide.current.querySelector('summary')?.focus()
          }
          markSeen()
        }}>Open guide</button>
        <button type="button" onClick={() => {
          guide.current?.querySelector('summary')?.focus()
          markSeen()
        }}>Skip guide</button>
      </div>
    </div>}
    <details ref={guide}>
      <summary>Help · Read this research</summary>
      <ol>
        <li>Check the research cutoff and observed price date. They can differ on market holidays or when coverage is incomplete.</li>
        <li>Read strengths and concerns first. Use the price chart for market timing and the valuation and business quality sections for company evidence.</li>
        <li>Open a metric to see its observed period and comparison. Switch to Quant for the formula, sources, and score contribution.</li>
        <li>Use Model Validation to see how scores related to later returns. The Opportunity Score is a relative research ranking.</li>
      </ol>
      <dl>{Object.values(financialGlossary).map(({ term, meaning }) => <div key={term}><dt>{term}</dt><dd>{meaning}</dd></div>)}</dl>
    </details>
  </section>
}
