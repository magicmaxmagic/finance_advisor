import { expect, test } from '@playwright/test'
import { mkdir, readFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const reportDir = resolve(process.cwd(), '../data/reports/v6')
const alphabet = '/?as_of=2026-11-20#company-research/GOOGL'

test.beforeEach(async ({ context, baseURL }) => {
  const origin = new URL(baseURL!).origin
  await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort())
  await mkdir(reportDir, { recursive: true })
})

test('Alphabet research explains missing valuation and reports risk independently', async ({ page }) => {
  await page.goto(alphabet)
  await expect(page.getByRole('heading', { name: /Alphabet Inc\./ })).toBeVisible()
  await expect(page.locator('.evidence-grid').getByText(/verified price for each outstanding economic class/)).toBeVisible()
  await expect(page.locator('.assessment-quick')).toContainText('38.6')
  await expect(page.locator('.assessment-quick')).toContainText('88.7')
  await expect(page.getByText(/closed 21\.3% below/)).toBeVisible()
  await expect(page.locator('.evidence-grid').getByText(/Business quality · 95\.5/)).toBeVisible()
  const chart = page.getByRole('heading', { name: 'Price history', exact: true })
  await expect(chart).toBeVisible()
  expect(await chart.evaluate(node => Boolean(
    node.compareDocumentPosition(document.querySelector('.v4-evidence')!) & Node.DOCUMENT_POSITION_FOLLOWING
  ))).toBe(true)
  await page.screenshot({ path: resolve(reportDir, 'googl-desktop.png') })
  await page.getByRole('button', { name: 'View price history' }).click()
  await expect(page.locator('.company-price-history')).toBeFocused()
  await page.screenshot({ path: resolve(reportDir, 'googl-chart.png') })

  await page.getByRole('button', { name: 'Explain Fiscal-year P/E' }).click()
  const valuation = page.getByRole('dialog', { name: 'Fiscal-year P/E' })
  await expect(valuation.getByText(/verified price for each outstanding economic class/)).toBeVisible()
  await valuation.getByRole('button', { name: 'Quant', exact: true }).click()
  await expect(valuation.getByText(/UNSUPPORTED/)).toBeVisible()
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: 'Score breakdown', exact: true }).click()
  await expect(page.getByText(/6 comparable observations; 8 are required/)).toBeVisible()
  await page.screenshot({ path: resolve(reportDir, 'googl-risk-evidence.png') })

  await page.getByRole('button', { name: 'Valuation', exact: true }).click()
  await page.emulateMedia({ media: 'print' })
  await expect(page.getByRole('searchbox')).toBeHidden()
  await expect(page.locator('.sidebar')).toBeHidden()
  await expect(page.locator('.assessment-hero')).toBeVisible()
  await expect(chart).toBeVisible()
  await expect.poll(() => page.locator('.chart-print-image').first().evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0)
  await page.pdf({ path: resolve(reportDir, 'googl-research.pdf'), format: 'A4', printBackground: true })
})

test('Alphabet reconciles its income flow and links official earnings events', async ({ page }) => {
  const performanceDir = resolve(process.cwd(), '../data/reports/v7-performance')
  await mkdir(performanceDir, { recursive: true })
  await page.goto(alphabet)
  await page.getByRole('button', { name: 'Open financial performance' }).click()
  const region = page.getByRole('region', { name: 'Financial performance' })
  const chart = region.getByRole('group', { name: /Revenue, spending, and profit Sankey/ })
  await expect(chart).toBeVisible()
  await expect(region.getByRole('heading', { name: 'How Alphabet makes money' })).toBeVisible()
  await expect(chart.getByRole('button', { name: /Google Search & other to Revenue/ })).toBeVisible()
  const accounting = await chart.locator('path[data-source]').evaluateAll(paths => paths.map(path => ({
    source: path.getAttribute('data-source'), target: path.getAttribute('data-target'), amount: Number(path.getAttribute('data-amount')),
    width: Number(path.getAttribute('stroke-width')),
  })))
  expect(accounting.filter(row => row.source?.startsWith('revenue_source_')).reduce((sum, row) => sum + row.amount, 0)).toBe(127_577_000_000)
  expect(accounting.filter(row => ['research', 'sales_marketing', 'administration'].includes(row.target ?? '')).every(row => row.source === 'gross_profit')).toBe(true)
  const widthPerDollar = accounting[0].width / accounting[0].amount
  expect(accounting.every(row => Math.abs(row.width / row.amount / widthPerDollar - 1) < 1e-8)).toBe(true)
  expect(await chart.locator('text').evaluateAll(labels => {
    const boxes = labels.filter(node => node.textContent?.trim()).map(node => (node as SVGTextElement).getBBox())
    return boxes.some((box, index) => boxes.slice(index + 1).some(other =>
      box.x < other.x + other.width && box.x + box.width > other.x &&
      box.y < other.y + other.height && box.y + box.height > other.y))
  })).toBe(false)
  const ribbon = chart.getByRole('button', { name: /Operating income to Nonoperating expense/ })
  await ribbon.focus()
  await expect(ribbon).toHaveAttribute('aria-pressed', 'true')
  await ribbon.press('Enter')
  await expect(region.getByRole('complementary', { name: 'Selected financial flow' })).toContainText('$8,221,000,000')
  await region.getByRole('button', { name: 'Clear selected flow' }).click()
  const exportEvent = page.waitForEvent('download')
  await region.getByRole('button', { name: 'Download SVG' }).click()
  const download = await exportEvent
  expect(download.suggestedFilename()).toBe('finlab-income-2026-09-30.svg')
  expect(await readFile((await download.path())!, 'utf-8')).toContain('Google Search &amp; other')
  await region.getByRole('button', { name: 'Expand chart' }).click()
  await expect(page.getByRole('dialog', { name: 'Expanded financial Sankey' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: 'Expanded financial Sankey' })).toBeHidden()
  await region.getByText('Income statement and filing evidence', { exact: true }).click()
  const table = region.getByRole('table', { name: 'Consolidated income statement and comparison' })
  await expect(table).toContainText('$127,577,000,000')
  await expect(table).toContainText('$26,468,000,000')
  await expect(table).toContainText('2025-09-30')
  await region.getByRole('button', { name: 'Quant', exact: true }).click()
  await expect(region.getByText(/Pretax less tax equals reported net income · matched/)).toBeVisible()
  await expect(region.getByText(/0001652044-26-000084/)).toBeVisible()
  await region.getByRole('button', { name: 'Annual', exact: true }).click()
  await expect(region.getByRole('table', { name: 'Reported values and reconciliation inputs' })).toContainText('$402,836,000,000')
  const call = region.getByRole('listitem').filter({ hasText: 'Alphabet Q3 2026 earnings call' })
  await expect(call).toContainText('1:30 PM')
  await expect(call).toContainText('historical')
  await call.getByRole('button', { name: 'View statement' }).click()
  await expect(region.getByLabel('Period ending')).toHaveValue('quarter:2026-09-30')
  await expect(region.getByRole('listitem').filter({ hasText: 'common dividend payment' })).toContainText('2026-12-14 · confirmed')
  await region.getByRole('button', { name: 'Simple', exact: true }).click()
  await page.mouse.move(3, 3)
  for (const width of [1440, 1280, 768, 390]) {
    await page.setViewportSize({ width, height: width > 800 ? 1050 : 844 })
    await expect(chart).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
    await region.getByRole('heading', { name: 'How Alphabet makes money' }).evaluate(node => {
      window.scrollBy(0, node.getBoundingClientRect().top - 130)
    })
    await page.screenshot({ path: resolve(performanceDir, `alphabet-sankey-v2-${width}.png`) })
  }
  const scrollable = region.getByRole('region', { name: 'Scrollable income statement flow chart' })
  await scrollable.evaluate(node => { node.scrollLeft = node.scrollWidth })
  expect(await scrollable.evaluate(node => node.scrollLeft)).toBeGreaterThan(400)
  await page.screenshot({ path: resolve(performanceDir, 'alphabet-sankey-v2-390-scrolled.png') })
  await page.setViewportSize({ width: 1440, height: 1024 })
  await page.emulateMedia({ media: 'print' })
  await expect(region.getByRole('button', { name: 'Download SVG' })).toBeHidden()
  await expect(chart).toBeVisible()
  await page.pdf({ path: resolve(performanceDir, 'alphabet-sankey-v2-print.pdf'), format: 'A4', printBackground: true })
})

test('Alphabet keeps the main price chart and explanations usable on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(alphabet)
  await expect(page.locator('.hero-score')).toHaveText('Unavailable')
  await expect(page.getByRole('heading', { name: 'Price history', exact: true })).toBeVisible()
  const price = page.locator('.price-chart').filter({ has: page.getByRole('heading', { name: 'Price history', exact: true }) })
  await price.getByRole('button', { name: 'SMA200', exact: true }).click()
  await expect(price.getByRole('button', { name: 'SMA200', exact: true })).toHaveAttribute('aria-pressed', 'true')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  await page.evaluate(() => window.scrollTo(0, 0))
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(0)
  await page.screenshot({ path: resolve(reportDir, 'googl-mobile.png'), fullPage: true })
  await page.getByRole('button', { name: 'Skip guide' }).click()
  await page.reload()
  await expect(page.getByRole('region', { name: 'Company research help' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open guide' })).toHaveCount(0)
  await page.getByText('Help · Read this research', { exact: true }).click()
  await expect(page.getByText('Rank information coefficient', { exact: true })).toBeVisible()
})

