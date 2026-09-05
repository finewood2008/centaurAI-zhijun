// Use only the disposable backend/tests/task_routing_fixture.py (port 8772).
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

const base = 'http://127.0.0.1:8772'
const out = '../../data/diagnostics/memory-attention'
mkdirSync(out, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 1440, height: 950 } })
await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort())
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
const get = async path => {
  const response = await context.request.get(base + path)
  assert.equal(response.ok(), true, await response.text())
  return response.json()
}
const post = async (path, data = {}) => {
  const response = await context.request.post(base + path, { data })
  assert.equal(response.ok(), true, await response.text())
  return response.json()
}
try {
  assert.equal((await get('/api/health')).version, 'task-routing-fixture', 'never run against personal data')
  const initial = await get('/__fixture')
  assert.equal(initial.onlineRequests.length, 0)
  assert.equal(initial.localRequests, 0)
  const cid = initial.memoryConversationId
  const attentionPath = `/api/mindos/conversations/${cid}/memory/attention`
  let release
  const delayed = new Promise(resolve => { release = resolve })
  let hold = true
  await page.route('**/memory/attention', async route => {
    if (hold) { hold = false; await delayed }
    await route.continue()
  })
  await page.goto(base + `/mindos/c/${cid}`, { waitUntil: 'domcontentloaded' })
  const composer = page.locator('.zj-page__composer textarea')
  await composer.waitFor()
  await page.getByText('第 4 段。', { exact: false }).first().waitFor()
  await composer.fill('我正在输入，不要覆盖。')
  await page.locator('.zj-page__messages').evaluate(element => { element.scrollTop = 0 })
  release()
  const chip = page.locator('.zj-chip')
  await chip.waitFor()
  assert.equal(await chip.count(), 1)
  assert.equal(await page.locator('[data-testid=alignment-card]').count(), 0)
  assert.equal(await composer.inputValue(), '我正在输入，不要覆盖。')
  assert.equal(await composer.evaluate(element => element === document.activeElement), true)
  assert.equal(await page.locator('.zj-page__messages').evaluate(element => element.scrollTop < element.scrollHeight - element.clientHeight - 50), true, 'arrival must not jump to bottom')
  const attention = await post(attentionPath)
  assert.equal(attention.pendingCount, 2)
  await page.screenshot({ path: `${out}/desktop.png`, fullPage: true })

  await page.getByTestId('memory-draft-entry').click()
  const drawer = page.getByRole('dialog', { name: '对话工作台' })
  await drawer.waitFor()
  assert.equal(await drawer.getByText(attention.draft.savedContent, { exact: true }).count() > 0, true)
  const save = page.waitForRequest(request => request.url().endsWith('/memory/draft-review'))
  await drawer.getByRole('button', { name: '只保存为这件事的记录', exact: true }).click()
  assert.equal((await save).postDataJSON().expectedRevision, attention.draft.revision)
  await drawer.getByText('已保存的事件记录', { exact: true }).waitFor()
  const saved = await post(attentionPath)
  assert.equal(saved.draft.status, 'saved')
  await drawer.getByRole('button', { name: '关闭对话工作台' }).click()
  const dismiss = page.waitForRequest(request => request.url().endsWith('/memory/dismiss'))
  await chip.getByRole('button', { name: '不用记住', exact: true }).click()
  assert.equal((await dismiss).postDataJSON().discard, true)
  await chip.waitFor({ state: 'hidden' })
  assert.equal((await post(attentionPath)).candidate, null, 'dismissing must not reveal the next queued claim')
  await page.reload({ waitUntil: 'networkidle' })
  assert.equal(await chip.count(), 0, 'the consumed topic slot survives reload')

  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByTestId('memory-draft-entry').click()
  await drawer.waitFor()
  await page.screenshot({ path: `${out}/mobile-drawer.png`, fullPage: true })
  assert.equal(await drawer.evaluate(element => element.scrollWidth <= element.clientWidth + 1), true)
  const box = await drawer.boundingBox()
  assert.ok(box.x >= -1 && box.x + box.width <= 391)
  await drawer.getByRole('button', { name: '关闭对话工作台' }).click()
  assert.equal(await composer.isVisible(), true)

  await page.goto(base + '/mindos/settings', { waitUntil: 'networkidle' })
  const policy = page.getByTestId('memory-policy')
  const select = policy.getByRole('combobox')
  await select.selectOption('manual')
  await page.getByText('记忆整理偏好已保存，现有记忆不受影响', { exact: true }).waitFor()
  assert.equal((await get('/api/mindos/memory-policy')).mode, 'manual')
  await page.reload({ waitUntil: 'networkidle' })
  assert.equal(await select.inputValue(), 'manual')
  await policy.screenshot({ path: `${out}/mobile-settings.png` })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true)
  const final = await get('/__fixture')
  assert.equal(final.onlineRequests.length, 0, 'viewing/saving preferences and event outlines never invokes an online model')
  assert.equal(final.localRequests, 0)
  assert.deepEqual(errors, [])
  console.log('memory UI: own-turn prompt, quiet arrival, single durable slot, drawer save, desktop/mobile and policy persistence passed')
} finally { await browser.close() }
