export const financialGlossary: Record<string, {term: string; meaning: string}> = {
  trailing_pe: { term: 'Price-to-earnings', meaning: 'How much investors pay for a dollar of the company’s latest published fiscal-year net income. Compare it with the company’s history and comparable firms.' },
  ev_to_operating_ebitda: { term: 'EV / EBITDA', meaning: 'Enterprise value per dollar of operating income before depreciation and amortization. Consider the cost of maintaining the business and whether the debt and equity claims are complete.' },
  roic: { term: 'Return on invested capital', meaning: 'After-tax operating profit relative to the capital invested in the business. Compare similar businesses over several periods.' },
  fcf_yield: { term: 'Free cash flow yield', meaning: 'Cash from operations after capital spending, relative to the market value of equity. A high yield can reflect a temporary cash-flow gain.' },
  beta: { term: 'Beta', meaning: 'How sensitive the stock’s daily returns have been to SPY. A beta above one indicates greater historical sensitivity over the measured window.' },
  annualized_volatility: { term: 'Volatility', meaning: 'The spread of daily returns, expressed at an annual scale. It includes both advances and declines.' },
  rsi: { term: 'RSI', meaning: 'A short-term comparison of recent gains and losses. Extreme readings can persist, so check the trend before reading them as a reversal.' },
  macd: { term: 'MACD', meaning: 'The gap between fast and slow exponential moving averages. Its sign describes which average is higher. Compare changes over time for the same stock.' },
  ma200: { term: 'SMA200', meaning: 'The average of the last 200 observed closing prices. A price below that average shows weakness relative to its own history.' },
  sharpe: { term: 'Sharpe ratio', meaning: 'Average excess return relative to return volatility. Compare results that use the same risk-free assumption, period and cost convention.' },
  max_drawdown: { term: 'Maximum drawdown', meaning: 'The deepest observed fall from a previous peak during the measurement period.' },
  rank_ic: { term: 'Rank information coefficient', meaning: 'The correlation between the model’s ranking and the subsequent return ranking. Sample size and uncertainty matter when interpreting its sign.' },
}
