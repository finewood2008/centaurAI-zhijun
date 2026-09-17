// Empty and failed reads use the same disposable fixture, with no writes.
import assert from 'node:assert/strict'
import { chromium } from 'playwright'
import { expect } from 'playwright/test'
const origin = process.env.REVISIT_ORIGIN || 'http://127.0.0.1:8777'
assert.equal((await (await fetch(origin + '/api/health')).json()).version, 'zhijun-revisit-fixture')
const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' })
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } })
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  let fail = false
  await page.route('**/api/mindos/growth/decisions', route => route.fulfill(fail
    ? { status: 503, json: { detail: '合成连接暂时中断' } } : { json: { items: [] } }))
  await page.route('**/api/mindos/conversations?*', route => route.fulfill({ json: { items: [], total: 0, hasMore: false } }))
  await page.goto(origin + '/mindos/review')
  await expect(page.getByText('现在没有需要回看的经历', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '选一段经历', exact: true }).click()
  await expect(page.getByText('当前没有其他可选经历。', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '记下一个选择', exact: true }).click()
  await page.getByLabel('这件事', { exact: false }).fill('合成草稿：取消后不保存')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  fail = true
  await page.reload()
  await expect(page.getByRole('button', { name: /重试/ })).toBeVisible()
  await expect(page.getByText('现在没有需要回看的经历', { exact: true })).toHaveCount(0)
  fail = false
  await page.getByRole('button', { name: /重试/ }).click()
  await expect(page.getByText('现在没有需要回看的经历', { exact: true })).toBeVisible()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  assert.deepEqual(errors, [])
  console.log('PASS revisit empty state, picker cancel, choice draft cancel, failed reads and retry on narrow screen')
} finally { await browser.close() }
