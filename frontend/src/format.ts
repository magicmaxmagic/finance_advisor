export const pct = (value?: number | null) => value == null ? '—' : `${(value * 100).toFixed(1)}%`
export const scoreText = (value?: number | null) => value == null ? '—' : value.toFixed(1)
export const cash = (value?: number | null, currency?: string | null) => value == null ? '—' : currency
  ? new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(value)
  : value.toLocaleString('en-US', { maximumFractionDigits: 2 })
const metricNames: Record<string, string> = {
  earnings_yield: 'Earnings yield', roa: 'Return on assets', fcf_margin: 'Free cash flow margin',
  revenue_growth: 'Revenue growth', price_to_sma200: 'Price vs. 200-day average',
  relative_momentum_12m: 'Momentum relative to SPY', annualized_volatility: 'Price volatility',
  max_drawdown: 'Peak-to-trough decline', interest_coverage: 'Interest coverage', current_ratio: 'Current ratio',
  nonoperating_interest_coverage: 'Nonoperating interest coverage',
  business_quality: 'Business quality', cash_flow_quality: 'Cash flow quality', growth: 'Revenue growth',
}
export const metricName = (key: string) => metricNames[key] ?? key.replaceAll('_', ' ')
export function classification(n: number | null) {
  return n == null ? 'Insufficient evidence' : n >= 65 ? 'Higher research rank' : n <= 35 ? 'Lower research rank' : 'Mid-range research rank'
}

