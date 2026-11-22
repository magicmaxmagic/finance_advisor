import { lazy, Suspense, type ComponentProps } from 'react'
import type { FinancialChart as Chart } from './FinancialChart'

const ChartView = lazy(() => import('./FinancialChart').then(module => ({ default: module.FinancialChart })))

export function FinancialChart(props: ComponentProps<typeof Chart>) {
  return <Suspense fallback={<div className="panel pending" role="status">Loading chart…</div>}><ChartView {...props}/></Suspense>
}
