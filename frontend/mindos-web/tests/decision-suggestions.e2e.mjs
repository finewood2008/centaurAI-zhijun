// Real Vue + isolated API fixture: no request can reach the user's database.
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

const fixture = 'http://127.0.0.1:8770'
const origin = 'http://127.0.0.1:5173'
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', e => errors.push(e.message))
let failNext = false
let delayNext = false
await page.route('**/*', async route => {
  const url = new URL(route.request().url())
  if (url.hostname !== '127.0.0.1') return route.abort()
  if (url.pathname.startsWith('/api/')) {
    if (url.pathname.endsWith('/suggestions')) {
      if (delayNext) { delayNext = false; await new Promise(r => setTimeout(r, 500)) }
      if (failNext) {
        failNext = false
        return route.fulfill({ status: 503, json: { detail: { code: 'LOCAL_UNAVAILABLE', detail: '本地模型暂时无法生成候选；不会切换到外部模型' } } })
      }
    }
    const response = await route.fetch({ url: fixture + url.pathname + url.search, timeout: 90000 })
    return route.fulfill({ response })
  }
  return route.continue()
})
const info = await (await context.request.get(fixture + '/__fixture')).json()
const getDraft = async () => (await context.request.get(`${fixture}/api/mindos/conversations/${info.conversationId}/decision-draft`)).json()
try {
  await page.goto(`${origin}/mindos/c/${info.conversationId}`, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: /^判断草稿/ }).click()
  const panel = page.getByRole('complementary', { name: '判断草稿', exact: true })
  await panel.getByText('由你来决定', { exact: true }).waitFor()
  const choice = panel.getByPlaceholder('最后你打算怎么选')
  const reason = panel.getByPlaceholder('关键的事实、假设和取舍')
  const outcome = panel.getByPlaceholder('到时候怎么判断这个选择对不对')
  const save = panel.getByRole('button', { name: '记进判断簿', exact: true })
  const generate = async () => {
    await panel.getByRole('button', { name: /帮我想几个方向|换一组方向/ }).click()
    await panel.getByTestId('decision-direction').first().waitFor({ timeout: 90000 })
  }
  await generate()
  assert.equal(await panel.getByTestId('decision-direction').count(), 3)
  assert.equal(await choice.inputValue(), '')
  assert.equal((await getDraft()).fields.choice, null)
  assert.equal((await getDraft()).status, 'draft')
  assert.equal(await save.isDisabled(), true)
  mkdirSync('../../data/diagnostics/decision-suggestions', { recursive: true })
  await page.screenshot({ path: '../../data/diagnostics/decision-suggestions/directions.png' })
  await panel.getByRole('button', { name: '使用这个方向：小范围验证' }).click()
  assert.equal(await choice.inputValue(), '先做一个可撤回的小试点')
  assert.equal((await getDraft()).fields.choice, null)
  assert.equal(await save.isDisabled(), true) // AI never filled confidence.
  await reason.fill('这是我自己修改的理由')
  await panel.getByRole('button', { name: '五成 · 还不确定', exact: true }).click()
  assert.equal(await save.isEnabled(), true)

  // Regeneration and rejected candidates cannot overwrite edits.
  await generate()
  await panel.getByRole('button', { name: '使用这个方向：先保留现状' }).click()
  const replace = panel.getByRole('group', { name: '确认如何填入候选' })
  assert.equal(await reason.inputValue(), '这是我自己修改的理由')
  await replace.getByRole('button', { name: '取消', exact: true }).click()
  await panel.getByRole('button', { name: '都不合适，我自己写' }).click()
  assert.equal(await reason.inputValue(), '这是我自己修改的理由')
  await outcome.fill('')
  await generate()
  await panel.getByRole('button', { name: '使用这个方向：先保留现状' }).click()
  await replace.getByRole('button', { name: '只补空白', exact: true }).click()
  assert.equal(await reason.inputValue(), '这是我自己修改的理由')
  assert.equal(await choice.inputValue(), '先做一个可撤回的小试点')
  assert.equal(await outcome.inputValue(), '观察现有用户的问题是否减少。')
  assert.equal(await panel.getByRole('slider').inputValue(), '50')

  // Failure and typing during a request preserve all form values.
  failNext = true
  await panel.getByRole('button', { name: '帮我想几个方向' }).click()
  await panel.getByRole('alert').filter({ hasText: '不会切换' }).waitFor()
  assert.equal(await reason.inputValue(), '这是我自己修改的理由')
  delayNext = true
  await panel.getByRole('button', { name: '帮我想几个方向' }).click()
  await reason.fill('请求期间的新修改')
  await panel.getByRole('alert').filter({ hasText: '生成期间修改' }).waitFor()
  assert.equal(await reason.inputValue(), '请求期间的新修改')
  assert.equal((await getDraft()).fields.choice, null)

  await page.setViewportSize({ width: 390, height: 844 })
  await page.keyboard.press('Escape')
  const closeNav = page.getByRole('button', { name: '关闭导航', exact: true })
  if (await closeNav.isVisible()) await closeNav.click()
  await page.getByRole('button', { name: /^判断草稿/ }).click()
  await choice.scrollIntoViewIfNeeded()
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  await page.screenshot({ path: '../../data/diagnostics/decision-suggestions/mobile.png', animations: 'disabled' })
  await save.click()
  await panel.getByText('已记进判断簿', { exact: true }).waitFor()
  const saved = await getDraft()
  assert.equal(saved.status, 'confirmed')
  assert.equal(saved.fields.rationale, '请求期间的新修改')
  assert.equal(saved.fields.confidence, 50)
  assert.deepEqual(saved.fields.assistedFields, ['choice', 'rationale', 'expectedOutcome'])
  await page.reload({ waitUntil: 'networkidle' })
  assert.equal((await getDraft()).status, 'confirmed')
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ passed: true, cases: ['read-only generation', 'explicit choice', 'confidence untouched', 'edit protection', 'fill empty', 'failure', 'typing race', 'mobile', 'confirm and reload'] }))
} catch (err) {
  mkdirSync('../../data/diagnostics/decision-suggestions', { recursive: true })
  await page.screenshot({ path: '../../data/diagnostics/decision-suggestions/failure.png' })
  console.error(JSON.stringify({ errors, url: page.url(), body: (await page.locator('body').innerText()).slice(-7000) }))
  throw err
} finally { await browser.close() }
