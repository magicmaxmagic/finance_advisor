import { useRef, useState } from 'react'
import { financialGlossary } from './financialGlossary'

const lessons = [
  ['Choose a company', 'Search by ticker or company name to open the company you want to study.', 'Search companies', 'search'],
  ['Read the assessment', 'Compare the company’s strengths, concerns, and unavailable evidence for the chosen date.', 'Show the assessment', 'summary'],
  ['Separate quality from valuation', 'Open Valuation and Business quality in Research details. A profitable company can still trade at an expensive multiple.', 'Show the research topics', 'details'],
  ['Check trend and risk', 'Use the price chart for trend and the Risks topic for volatility, declines, and the balance sheet.', 'Show price history', 'price'],
  ['Check the evidence', 'Model Validation shows uncertainty and how scores related to subsequent returns in the evaluated sample.', 'Open Model Validation', 'validation'],
  ['Save the company', 'A watchlist keeps companies you want to revisit and filters the events calendar.', 'Save to watchlist', 'watchlist'],
] as const

export type GuideDestination = typeof lessons[number][3]

export function ResearchGlossary({ visit }: { visit: (destination: GuideDestination) => void }) {
  const [firstVisit, setFirstVisit] = useState(() => localStorage.getItem('finlab-research-guide-seen') !== 'true')
  const [step, setStep] = useState(0)
  const guide = useRef<HTMLDetailsElement>(null)
  const lesson = lessons[step]

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
      <div className="v4-context"><h3>Guided company analysis</h3><p>{step + 1} of {lessons.length} · {lesson[0]}</p>
        <p>{lesson[1]}</p><div className="toolbar">
          <button type="button" onClick={() => visit(lesson[3])}>{lesson[2]}</button>
          <button type="button" disabled={step === 0} onClick={() => setStep(step - 1)}>Previous lesson</button>
          {step + 1 < lessons.length && <button type="button" onClick={() => setStep(step + 1)}>Next lesson</button>}
        </div></div>
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
