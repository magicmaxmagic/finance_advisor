/** Local portfolio protocol and legacy browser-ledger migration. */
export type Currency = 'USD' | 'CAD'
export type Trade = {
  id: string; ticker: string; date: string; side: 'buy' | 'sell'; quantity: number
  price: number; currency: Currency; fee: number; rate: number
}
export type Ledger = { base: Currency; trades: Trade[] }
export const STORAGE_KEY = 'finlab-portfolio-ledger-v4'
export const CSV_HEADER = 'date,ticker,side,quantity,price,currency,fees,exchange_rate'
const symbol = /^[A-Z0-9.:-]{1,64}$/
const iso = /^\d{4}-\d{2}-\d{2}$/

export function validateTrade(trade: Trade, base: Currency) {
  if (!symbol.test(trade.ticker)) throw Error('Select a valid ticker.')
  const date = new Date(trade.date + 'T00:00:00Z')
  if (!iso.test(trade.date) || !Number.isFinite(date.valueOf()) || date.toISOString().slice(0,10) !== trade.date)
    throw Error('Enter a valid transaction date.')
  if (trade.side !== 'buy' && trade.side !== 'sell') throw Error('Choose buy or sell.')
  if (trade.currency !== 'USD' && trade.currency !== 'CAD') throw Error('Choose USD or CAD.')
  if (![trade.quantity,trade.price,trade.rate].every(v => Number.isFinite(v) && v > 0)) throw Error('Quantity, price and any required FX rate must be positive.')
  if (!Number.isFinite(trade.fee) || trade.fee < 0) throw Error('Fees must be zero or greater.')
  if (trade.currency === base && trade.rate !== 1) throw Error('The exchange rate for the base currency must be 1.')
}

export function loadLedger(): Ledger {
  const text = localStorage.getItem(STORAGE_KEY)
  if (!text) return { base: 'USD', trades: [] }
  const result: Ledger = JSON.parse(text)
  if (!result || !['USD','CAD'].includes(result.base) || !Array.isArray(result.trades)) throw Error('Saved portfolio could not be read.')
  result.trades.forEach(trade => validateTrade(trade,result.base))
  if (result.trades.some(trade => typeof trade.id !== 'string') || new Set(result.trades.map(trade => trade.id)).size !== result.trades.length)
    throw Error('Saved portfolio has missing or duplicate transaction IDs.')
  return result
}

export type LedgerRevision = { revision: number; ledger: Ledger; updated_at: string | null }

export async function serverLedger(signal?: AbortSignal, update?: { expected_revision: number; ledger: Ledger }): Promise<LedgerRevision> {
  const response = await fetch('/api/portfolio/ledger', update ? {
    signal, method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(update),
  } : { signal })
  const body = await response.json()
  if (!response.ok) throw Error(typeof body.detail === 'string' ? body.detail : 'Check the local portfolio service and transaction fields.')
  return body as LedgerRevision
}

function parseLine(line: string) {
  const values: string[] = []
  let field = '', quote = false
  for (let index=0; index<line.length; index++) {
    const char = line[index]
    if (char === '"') {
      if (quote && line[index+1] === '"') { field += '"'; index++ }
      else quote = !quote
    } else if (char === ',' && !quote) { values.push(field.trim()); field = '' }
    else field += char
  }
  if (quote) throw Error('A CSV field has an unmatched quote.')
  return [...values,field.trim()]
}

export function parseTrades(csv: string, base: Currency): Trade[] {
  const [head,...lines] = csv.trim().replace(/^\uFEFF/,'').split(/\r?\n/)
  const columns = parseLine(head).map(value => value.toLowerCase())
  if (new Set(columns).size !== columns.length || !['date','side','quantity','price','currency'].every(key => columns.includes(key))
    || !(columns.includes('ticker') || columns.includes('security_id'))) throw Error('CSV needs date, ticker, side, quantity, price and currency columns.')
  const entries = lines.filter(line => line.trim()).map((line,index) => {
    try {
      const values = parseLine(line)
      if (values.length !== columns.length) throw Error('Column count does not match the header.')
      const fields = Object.fromEntries(columns.map((key,offset) => [key,values[offset]]))
      const trade: Trade = {
        id: crypto.randomUUID(), ticker: (fields.ticker || fields.security_id || '').toUpperCase(),
        date: fields.date, side: fields.side.toLowerCase() as Trade['side'], quantity: Number(fields.quantity),
        price: Number(fields.price), currency: fields.currency.toUpperCase() as Currency,
        fee: Number(fields.fees ?? fields.fee ?? 0), rate: Number(fields.exchange_rate || fields.fx_to_base || (fields.currency.toUpperCase() === base ? 1 : 0)),
      }
      validateTrade(trade,base)
      return trade
    } catch (reason) { throw Error(`CSV row ${index+2}. ${reason instanceof Error ? reason.message : String(reason)}`) }
  })
  if (!entries.length) throw Error('The CSV contains no transactions.')
  return entries
}

export function serializeTrades(trades: Trade[], internal = false) {
  const header = internal ? 'date,security_id,side,quantity,price,currency,fee,fx_to_base' : CSV_HEADER
  return [header,...trades.map(trade => [trade.date,trade.ticker,trade.side,trade.quantity,trade.price,trade.currency,trade.fee,trade.rate].join(','))].join('\n')+'\n'
}
