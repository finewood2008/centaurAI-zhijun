// Real Vue + isolated FastAPI/SQLite fixture. All network stays on loopback.
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

const origin = 'http://127.0.0.1:5173'
const fixture = 'http://127.0.0.1:8769'
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', e => errors.push(e.message))
await page.route('**/*', async route => {
  const url = new URL(route.request().url())
  if (url.hostname !== '127.0.0.1') return route.abort()
  if (url.pathname.startsWith('/api/')) {
    const response = await route.fetch({ url: fixture + url.pathname + url.search })
    return route.fulfill({ response })
  }
  return route.continue()
})
const info = await (await context.request.get(fixture + '/__fixture')).json()
const getClaim = async () => (await context.request.get(fixture + '/api/mindos/ontology/claims/' + info.claimId)).json()
try {
  await page.goto(`${origin}/mindos/c/${info.conversationId}`, { waitUntil: 'networkidle' })
  const card = page.getByTestId('alignment-card')
  await card.waitFor()
  assert.match(await card.textContent(), /尚未校准/)
  await card.getByLabel('不代表我', { exact: true }).check()
  await card.getByLabel('你的说明（可选）').fill('合成说明：这是工作安排，不是我的个人追求。')
  // Selecting a radio is only a preview; no write until the confirm button.
  assert.equal((await getClaim()).selfAlignment.level, null)
  const saved = page.waitForResponse(r => r.url().endsWith(`/claims/${info.claimId}/alignment`) && r.request().method() === 'POST')
  await card.getByRole('button', { name: '确认保存校准', exact: true }).click()
  assert.equal((await saved).status(), 200)
  const c = await getClaim()
  assert.equal(c.trustState, 'confirmed')
  assert.equal(c.selfAlignment.level, 0)
  await page.reload({ waitUntil: 'networkidle' })
  const privacy = page.getByTestId('alignment-privacy')
  await privacy.locator('summary').first().click()
  await privacy.getByText('fixture.invalid', { exact: false }).waitFor()
  await privacy.locator('input[type=checkbox]').first().check()
  await privacy.getByRole('button', { name: '允许所选画像用于该服务' }).click()
  await privacy.getByText('当前服务已授权', { exact: false }).waitFor()

  await page.goto(`${origin}/mindos/me?section=matters&claim=${info.claimId}`, { waitUntil: 'networkidle' })
  const node = page.getByTestId('selfmap-node')
  assert.ok(await node.count() >= 5)
  const point = page.locator('[data-testid="selfmap-node"]').and(page.getByRole('button', { name: /我负责星桥项目.*不代表我/ }))
  await point.waitFor()
  const radius = async () => point.locator('.zj-map__dot').evaluate(el => Math.hypot(Number(el.getAttribute('cx')) - 360, Number(el.getAttribute('cy')) - 360))
  assert.ok(Math.abs(await radius() - 140) < 0.1)
  const mapCard = page.getByTestId('alignment-card').filter({ hasText: '不代表我' }).first()
  await mapCard.getByRole('button', { name: '修改我的校准' }).click()
  await mapCard.getByLabel('很能代表', { exact: true }).check()
  await mapCard.getByRole('button', { name: '确认保存校准' }).click()
  const highPoint = page.getByRole('button', { name: /我负责星桥项目.*很能代表/ })
  await highPoint.waitFor()
  const highRadius = await highPoint.locator('.zj-map__dot').evaluate(el => Math.hypot(Number(el.getAttribute('cx')) - 360, Number(el.getAttribute('cy')) - 360))
  assert.ok(Math.abs(highRadius - 80) < .1)
  await page.reload({ waitUntil: 'networkidle' })
  assert.equal((await getClaim()).selfAlignment.level, 4)
  mkdirSync('../../data/diagnostics/self-alignment', { recursive: true })
  await page.screenshot({ path: '../../data/diagnostics/self-alignment/desktop.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.keyboard.press('Escape')
  await page.screenshot({ path: '../../data/diagnostics/self-alignment/mobile.png', animations: 'disabled' })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ passed: true, ...info, cases: ['preview is not approval', 'zero retains fact', 'service consent', 'radial placement', 'refresh', 'mobile'] }))
} finally { await browser.close() }
