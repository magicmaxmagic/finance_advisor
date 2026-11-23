import { expect, test } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'

const reportDir = resolve(process.cwd(), '../data/reports/v8')

test.beforeEach(async ({ context, baseURL }) => {
  const origin = new URL(baseURL!).origin
  await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort())
  await mkdir(reportDir, { recursive:true })
})

test('calendar filters an official event and opens its filed quarterly statement', async ({page}) => {
  await page.addInitScript(() => localStorage.setItem('finlab-watchlist', JSON.stringify(['GOOGL'])))
  await page.goto('/?as_of=2026-11-20#calendar')
  const calendar=page.getByRole('main').filter({ has:page.getByRole('heading',{name:'Financial events',exact:true}) })
  await calendar.getByLabel('Company',{exact:true}).selectOption('GOOGL')
  await calendar.getByRole('button',{name:'Upcoming',exact:true}).click()
  await expect(calendar.getByText(/Alphabet common dividend payment date/)).toBeVisible()
  await expect(calendar.getByText('2026-12-14 · Confirmed')).toBeVisible()
  await calendar.getByLabel('Company',{exact:true}).selectOption('watchlist')
  await expect(calendar.getByText(/Alphabet common dividend payment date/)).toBeVisible()
  await page.screenshot({path:resolve(reportDir,'calendar-upcoming.png')})
  await calendar.getByRole('button',{name:'Week',exact:true}).click()
  await expect(calendar.getByText('2026-11-16 to 2026-11-22')).toBeVisible()
  await calendar.getByRole('button',{name:'Month',exact:true}).click()
  await calendar.getByRole('button',{name:'Previous period'}).click()
  await calendar.getByLabel('Event type').selectOption('earnings_call')
  await expect(calendar.getByText(/Alphabet Q3 2026 earnings call/)).toBeVisible()
  await expect(calendar.getByText(/1:30 PM · America\/Los_Angeles · Historical/)).toBeVisible()
  await calendar.getByRole('link',{name:'Financial performance',exact:true}).click()
  const statement=page.getByRole('region',{name:'Financial performance'})
  await expect(statement.getByLabel('Period ending')).toHaveValue('quarter:2026-09-30')
  await expect(statement.getByRole('group',{name:'Revenue, spending, and profit Sankey'})).toBeVisible()
})

test('portfolio migrates an existing browser ledger once and restores it after a reset', async ({page}) => {
  await page.addInitScript(() => {
    if (localStorage.getItem('v8-test-seeded')) return
    localStorage.setItem('v8-test-seeded','true')
    localStorage.setItem('finlab-portfolio-ledger-v4',JSON.stringify({base:'USD',trades:[{
      id:'legacy-buy', ticker:'US:0000320193:AAPL', date:'2020-01-02', side:'buy', quantity:1, price:100, currency:'USD', fee:0, rate:1,
    }]}))
  })
  let saved = {revision:0,updated_at:null,ledger:{base:'USD',trades:[]}}
  let writes = 0
  await page.route('**/api/portfolio/ledger', async route => {
    if (route.request().method() === 'PUT') {
      const update = route.request().postDataJSON()
      expect(update.expected_revision).toBe(0)
      writes++
      saved = {...saved,revision:1,ledger:update.ledger}
    }
    await route.fulfill({json:saved})
  })
  await page.goto('/?as_of=2020-12-31#portfolio/AAPL')
  await expect(page.getByText('Browser transactions copied to the local portfolio store.')).toBeVisible()
  await expect(page.getByRole('button',{name:'Edit AAPL transaction'})).toBeVisible()
  await page.evaluate(() => localStorage.removeItem('finlab-portfolio-ledger-v4'))
  await page.reload()
  await expect(page.getByRole('button',{name:'Edit AAPL transaction'})).toBeVisible()
  expect(writes).toBe(1)
})

test('company research separates decisions, sentiment, and held-out model evidence', async ({page}) => {
  await page.goto('/?as_of=2026-11-20#company-research/GOOGL')
  await page.getByRole('button',{name:'Decision research',exact:true}).click()
  await page.getByLabel('Research horizon').selectOption('tactical')
  await expect(page.getByText('Valuation attractiveness',{exact:true})).toBeVisible()
  await page.getByText(/Exit and thesis review ·/).click()
  await expect(page.getByRole('group',{name:'Exit and thesis review'}).getByText(/Predictive evidence inconclusive · 46 windows/)).toBeVisible()
  await page.screenshot({path:resolve(reportDir,'company-decision-research.png')})
  await page.getByRole('button',{name:'Market & events',exact:true}).click()
  await expect(page.getByText('1 published release in 30 days · 0 in the previous 30 days.')).toBeVisible()
  await expect(page.getByText(/positive tone/)).toBeVisible()
  await page.getByRole('button',{name:'Open guide'}).click()
  await expect(page.getByRole('heading',{name:'Guided company analysis'})).toBeVisible()
  await page.getByRole('button',{name:'Next lesson'}).click()
  await page.getByRole('button',{name:'Show the assessment'}).click()
  await expect(page.locator('#company-assessment-summary')).toBeFocused()
  await page.getByRole('button',{name:'Model Validation',exact:true}).click()
  await page.getByLabel('Validation study').selectOption('v8')
  await expect(page.getByRole('heading',{name:'Same companies, same dates'})).toBeVisible()
  await expect(page.getByText('Predictive evidence inconclusive',{exact:true})).toBeVisible()
  await expect(page.getByRole('heading',{name:'Rank IC by decision date'})).toBeVisible()
  await page.screenshot({path:resolve(reportDir,'held-out-validation.png')})
})
