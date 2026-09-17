// Actual built pages and real isolated FastAPI stores. No user data or model network.
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'
const origin = process.env.REFLECTION_FIXTURE_URL || 'http://127.0.0.1:8776'
const health = await (await fetch(origin + '/api/health')).json()
assert.equal(health.version, 'zhijun-v3-reflection-fixture')
const fixture = await (await fetch(origin + '/__fixture')).json()
assert.equal(fixture.synthetic, true)
const output = process.env.REFLECTION_SCREENSHOTS || '/private/tmp/zhijun-v3-screenshots'
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const errors = []
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.on('pageerror', err => errors.push(err.message))
  await page.goto(origin + '/mindos/reflections')
  const card = page.getByTestId('reflection-card')
  await card.waitFor()
  await card.locator('summary').filter({ hasText: '为什么这样想' }).click()
  assert.equal(await card.locator('blockquote').count(), 2)
  await page.screenshot({ path: output + '/01-first-reflection.png', fullPage: true })
  await card.getByRole('button', { name: '看情况', exact: true }).click()
  const note = '主要针对新合作伙伴，熟悉的人不用反复确认。'
  await card.getByRole('textbox').fill(note)
  await card.getByRole('button', { name: '保存我的补充', exact: true }).click()
  await card.getByRole('status').filter({ hasText: '你的补充已保存' }).waitFor()
  await page.getByRole('button', { name: '我认可的', exact: true }).click()
  await card.locator('.reflection-correction').filter({ hasText: note }).waitFor()
  await page.reload()
  await page.getByRole('button', { name: '我认可的', exact: true }).click()
  await card.locator('.reflection-correction').filter({ hasText: note }).waitFor()
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 })
    if (width < 768) await page.waitForFunction(() => document.querySelector('.ws-sidebar').getBoundingClientRect().right <= 1)
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'no horizontal overflow')
    await page.screenshot({ path: output + `/02-corrected-${width}.png`, fullPage: true })
  }
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto(origin + '/mindos/c/' + fixture.conversationId)
  const composer = page.locator('textarea').last()
  await composer.fill('我准备和熟悉的老伙伴继续合作，要注意哪些问题？')
  await composer.press('Enter')
  await page.getByText('演示回复：我会保留你补充的适用条件：', { exact: false }).waitFor({ timeout: 20000 })
  const recorded = await (await fetch(origin + '/__fixture')).json()
  assert.ok(recorded.requests.at(-1).system.includes(note), 'next actual chat prompt carries the corrected condition')
  await page.screenshot({ path: output + '/03-next-conversation.png', fullPage: true })
  await page.goto(origin + '/mindos/reflections')
  await page.getByRole('button', { name: '我认可的', exact: true }).click()
  await card.getByRole('button', { name: '修改我的反馈', exact: true }).click()
  await card.getByRole('button', { name: '不像我', exact: true }).click()
  await page.getByRole('button', { name: '我不同意的', exact: true }).click()
  await card.getByText('你不同意的观察', { exact: true }).first().waitFor()
  const response = await fetch(origin + '/api/mindos/conversations/' + fixture.conversationId + '/routing/preview', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ content: '新合作伙伴的沟通问题怎么处理？' }) })
  assert.equal(response.status, 200)
  const preview = await response.json()
  assert.ok(!JSON.stringify(preview.request).includes('用户校准过的照见'), 'rejected reflection stops being a current understanding')
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ ok: true, screenshots: output, checks: ['evidence', 'correction', 'reload', 'next-chat-prompt', 'rejection', 'desktop-and-mobile'] }))
} finally { await browser.close() }
