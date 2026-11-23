export const compactMoney = (amount: number, currency: string) => new Intl.NumberFormat('en-US', {
  style: 'currency', currency, notation: 'compact', maximumFractionDigits: 2,
}).format(amount)

export function annualChange(current?: number, prior?: number) {
  if (current == null || prior == null || prior <= 0) return null
  return current / prior - 1
}

