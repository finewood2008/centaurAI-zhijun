import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

const fixture = 'http://127.0.0.1:8771'
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', e => errors.push(e.message))
let fail = true
await page.route('**/*', async route => {
  const u = new URL(route.request().url())
  if (u.hostname !== '127.0.0.1') return route.abort()
  if (u.pathname.startsWith('/api/')) {
    if (u.pathname.endsWith('/learning/suggest') && fail) {
      fail = false
      return route.fulfill({ status: 503, json: { detail: '本地模型暂不可用；仍可手动记录，不会改用外部模型' } })
    }
    return route.fulfill({ response: await route.fetch({ url: fixture + u.pathname + u.search, timeout: 90000 }) })
  }
  return route.continue()
})
const info = await (await ctx.request.get(fixture + '/__fixture')).json()
const state = async () => (await (await ctx.request.get(`${fixture}/api/mindos/conversations/${info.conversationId}/learning`)).json()).episode
const dir = '../../data/diagnostics/context-learning'
mkdirSync(dir, { recursive: true })
try {
  await page.goto(`http://127.0.0.1:5173/mindos/c/${info.conversationId}`, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '观察与复盘', exact: true }).click()
  const card = page.getByTestId('learning-card')
  await card.getByRole('button', { name: '用这次经历校准理解' }).click()
  await card.getByRole('button', { name: '让 AI 草拟观察问题' }).click()
  await card.getByRole('alert').filter({ hasText: '不会改用外部模型' }).waitFor()
  await card.getByLabel('这次的具体情境').fill('本地模型不可用时仍可以手动记录')
  await card.getByRole('button', { name: '让 AI 草拟观察问题' }).click()
  await card.getByRole('status').filter({ hasText: '还没有保存' }).waitFor()
  assert.equal(await state(), null)
  await card.getByRole('button', { name: '确认预期，开始观察' }).click()
  await card.getByText('事前预期 · 已冻结', { exact: true }).waitFor()
  assert.equal((await state()).status, 'watching')
  await page.reload({ waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '观察与复盘', exact: true }).click()
  await card.getByText('事前预期 · 已冻结', { exact: true }).waitFor()
  await page.screenshot({ path: `${dir}/expectation.png`, animations: 'disabled' })
  // A held-out outcome arrives only after the frozen observation exists.
  const response = await ctx.request.post(`${fixture}/api/mindos/conversations/${info.conversationId}/outcome`, {
    data: { result: '准备后愿意分享，但临场问答仍会紧张', notes: '合成案例，不代表真实用户' },
  })
  assert.equal(response.status(), 200)
  await page.reload({ waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '观察与复盘', exact: true }).click()
  await card.getByRole('button', { name: '让 AI 帮我对照' }).click()
  await card.getByRole('status').filter({ hasText: '还没有保存' }).waitFor()
  await card.getByRole('button', { name: '保存对照，暂不改写本体' }).click()
  await card.getByRole('button', { name: '确认并修订这条理解' }).waitFor()
  assert.equal((await state()).status, 'proposed')
  await page.screenshot({ path: `${dir}/comparison.png`, animations: 'disabled' })
  await card.getByLabel('准备怎样修订理解').fill('有准备且主题熟悉时，我愿意分享；现场提问仍会紧张')
  await card.getByRole('button', { name: '确认并修订这条理解' }).click()
  await card.getByRole('status').filter({ hasText: '已按你的确认修订' }).waitFor()
  assert.equal((await state()).resolution.content, '有准备且主题熟悉时，我愿意分享；现场提问仍会紧张')
  await page.reload({ waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '观察与复盘', exact: true }).click()
  await card.getByRole('status').filter({ hasText: '已按你的确认修订' }).waitFor()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.keyboard.press('Escape')
  const closeNav = page.getByRole('button', { name: '关闭导航', exact: true })
  if (await closeNav.isVisible()) await closeNav.click()
  await page.getByRole('button', { name: '观察与复盘', exact: true }).click()
  await card.scrollIntoViewIfNeeded()
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  await page.screenshot({ path: `${dir}/mobile.png`, animations: 'disabled' })
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ passed: true, cases: ['local failure/manual fallback', 'AI draft read-only', 'freeze and reload', 'held-out outcome', 'proposal not auto applied', 'explicit revision', 'persist edited wording', 'mobile'] }))
} catch (e) {
  await page.screenshot({ path: `${dir}/failure.png`, animations: 'disabled' })
  console.error(JSON.stringify({ errors, body: (await page.locator('body').innerText()).slice(-6000) }))
  throw e
} finally { await browser.close() }
