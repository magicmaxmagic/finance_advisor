// Run with Node 20+ (--experimental-websocket on Node 20) and local Chrome.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'

const [chrome, origin = 'http://127.0.0.1:5174', output = 'data/reports/ui'] = process.argv.slice(2)
assert(chrome && typeof WebSocket === 'function', 'Provide the Chrome binary and enable the Node WebSocket API.')
const profile = await mkdtemp(join(tmpdir(), 'finlab-ui-'))
const browser = spawn(chrome, ['--headless', '--disable-gpu', '--no-first-run',
  '--disable-background-networking', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'],
{ stdio: 'ignore' })
let connection

async function until(check, description, timeout = 90000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    const result = await check()
    if (result) return result
    await sleep(150)
  }
  throw Error(`Timed out: ${description}`)
}

try {
  await mkdir(output, { recursive: true })
  const port = await until(async () => (await readFile(join(profile, 'DevToolsActivePort'), 'utf8').catch(() => '')).split('\n')[0], 'Chrome debugger')
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  const page = pages.find(target => target.type === 'page')
  connection = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    connection.addEventListener('open', resolve, { once: true })
    connection.addEventListener('error', reject, { once: true })
  })
  let id = 0
  const pending = new Map()
  const errors = []
  connection.addEventListener('message', event => {
    const data = JSON.parse(event.data)
    if (data.method === 'Runtime.exceptionThrown') errors.push(data.params.exceptionDetails.text + ' ' + data.params.exceptionDetails.exception?.description)
    const callback = pending.get(data.id)
    if (callback) {
      pending.delete(data.id)
      if (data.error) callback.reject(data.error)
      else callback.resolve(data.result)
    }
  })
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const callId = ++id
    pending.set(callId, { resolve, reject })
    connection.send(JSON.stringify({ id: callId, method, params }))
  })
  const evaluate = async expression => {
    const data = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true, userGesture: true })
    if (data.exceptionDetails) throw Error(JSON.stringify(data.exceptionDetails))
    return data.result.value
  }
  const evaluateFunction = (fn, argument) => evaluate(`(${fn})(${JSON.stringify(argument)})`)
  await send('Runtime.enable')
  await send('Page.enable')
  async function size(width) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: width === 390 ? 844 : 980, deviceScaleFactor: 1, mobile: false })
  }
  async function go(path, heading, content, search = '') {
    const navigation = await send('Page.navigate', { url: `${origin}/${search}#${path}` })
    assert(!navigation.errorText, navigation.errorText)
    try {
      await until(() => evaluateFunction(label => document.querySelector('h1')?.textContent === label, heading), heading)
      if (content) await until(() => evaluateFunction(label => document.body.innerText.includes(label), content), content)
    } catch (error) {
      const state = await evaluate('({ url: location.href, title: document.title, text: document.body?.innerText.slice(0, 1200) })')
      throw Error(`${error.message}. Page: ${JSON.stringify(state)}. Browser errors: ${JSON.stringify(errors)}`)
    }
  }
  async function click(label) {
    assert(await evaluateFunction(text => {
      const button = [...document.querySelectorAll('button')].find(item => item.getClientRects().length
        && (item.textContent.trim() === text || item.getAttribute('aria-label') === text))
      if (!button) return false
      button.click()
      return true
    }, label), `Visible control: ${label}`)
  }
  async function fill(label, value) {
    assert(await evaluateFunction(({ label, value }) => {
      const field = [...document.querySelectorAll('label')].find(item => item.firstChild?.textContent.trim() === label)
        ?.querySelector('input,textarea')
      if (!field) return false
      Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), 'value').set.call(field, value)
      field.dispatchEvent(new Event('input', { bubbles: true }))
      field.dispatchEvent(new Event('change', { bubbles: true }))
      return true
    }, { label, value }), `Input: ${label}`)
  }
  async function snap(name, scroll = true) {
    await sleep(350)
    if (scroll) await evaluate('window.scrollTo(0, 0)')
    assert(await evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), `No viewport overflow on ${name}`)
    const { data } = await send('Page.captureScreenshot', { format: 'png' })
    await writeFile(join(output, `${name}.png`), Buffer.from(data, 'base64'))
  }
  await size(1440)
  await go('overview', 'Market overview', 'Top research opportunities')
  await snap('after-overview-1440')
  await click('Explore companies')
  await until(() => evaluate("document.querySelector('h1')?.textContent === 'Discover companies'"), 'screener navigation')
  await until(() => evaluate('document.querySelectorAll(".screener tbody tr").length > 5'), 'screener rows')
  await click('Compact density')
  assert(await evaluate("!!document.querySelector('.table-wrap.compact')"))
  await click('Next')
  assert(await evaluate("document.querySelector('.pagination').textContent.includes('2 /')"))
  await size(1280)
  await snap('after-discover-1280')
  await go('company-research/AAPL', 'Company research', 'Explore the evidence')
  await until(() => evaluate("document.querySelector('canvas')?.width > 0"), 'chart')
  await click('SMA20')
  await click('3M')
  assert(await evaluate("Array.from(document.querySelectorAll('button[aria-pressed=true]')).some(el => el.textContent === '3M')"))
  await click('☆ Watchlist')
  assert(await evaluate("JSON.parse(localStorage.getItem('finlab-watchlist')).includes('AAPL')"))
  await snap('after-company-1280')
  await size(768)
  await snap('after-company-768')
  await size(390)
  await snap('after-company-390')
  await click('Toggle navigation')
  assert(await evaluate("document.querySelector('.sidebar').classList.contains('mobile-open')"))
  await click('Data Center')
  await until(() => evaluate("document.body.innerText.includes('Current publication')"), 'data center publication')
  await click('Request synchronization')
  await click('Cancel')
  await snap('after-data-center-390')
  await size(1440)
  await go('model-validation', 'Model validation', 'Predictive evidence inconclusive.')
  await until(() => evaluate("document.body.innerText.includes('Model comparison')"), 'archived evidence')
  await snap('after-validation-1440')
  await go('strategies', 'Strategy lab', 'Run a historical simulation')
  await fill('Start date', '2020-01-02')
  await fill('End date', '2020-03-31')
  await click('Run simulation')
  await until(() => evaluate("document.body.innerText.includes('Annualized return')"), 'strategy results')
  await click('Drawdown')
  await until(() => evaluate("document.body.innerText.includes('Drawdown (%)')"), 'drawdown chart')
  await snap('after-strategy-1440')
  await go('portfolio', 'Portfolio', 'Value a portfolio')
  await fill('Transactions', 'date,security_id,side,quantity,price,currency,fx_to_base,fee\n2020-01-02,AAPL,buy,10,100,USD,1,0\n')
  await fill('Valuation date', '2020-12-31')
  await click('Analyze holdings')
  await until(() => evaluate("document.body.innerText.includes('Portfolio holdings')"), 'portfolio results')
  await snap('after-portfolio-1440')
  await go('watchlist', 'Watchlist', 'AAPL')
  await go('company-research/AAPL', 'Company research', '70.6', '?as_of=2020-12-31')
  await click('Quant')
  await until(() => evaluate("document.body.innerText.includes('hierarchical-equity-assessment-v2')"), 'quant methodology')
  await snap('after-company-historical-1440')
  await click('SMA50')
  await click('SPY')
  await evaluate("document.querySelector('.price-chart')?.scrollIntoView({block:'center'})")
  await snap('after-chart-1440', false)
  await size(390)
  await go('overview', 'Market overview', 'Top research opportunities')
  await snap('after-overview-390')
  assert.deepEqual(errors, [], 'No runtime exceptions')
  console.log('FinLab browser checks passed at 1440, 1280, 768, and 390 pixels.')
} finally {
  connection?.close()
  browser.kill()
  await sleep(800)
  await rm(profile, { recursive: true, force: true }).catch(() => {})
}
