// Start backend: .venv/bin/python -m tests.task_routing_fixture
// This test refuses live databases and blocks all non-fixture browser traffic.
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

const base = 'http://127.0.0.1:8772'
const output = '../../data/diagnostics/conversation-management'
mkdirSync(output, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort())
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
const request = async (method, path, data) => {
  const response = await context.request.fetch(base + path, { method, ...(data ? { data } : {}) })
  assert.equal(response.ok(), true, await response.text())
  return response.json()
}
const get = path => request('GET', path)
const patch = (cid, data) => request('PATCH', `/api/mindos/conversations/${cid}`, data)
const done = async promise => { const response = await promise; assert.equal(response.ok(), true, await response.text()); await response.finished() }
try {
  assert.equal((await get('/api/health')).version, 'task-routing-fixture')
  const fixture = await get('/__fixture')
  const cid = fixture.memoryConversationId
  const getCurrent = async () => (await get(`/api/mindos/conversations/${cid}`)).conversation
  for (let i = 0; i < 35; i++) await request('POST', '/api/mindos/conversations', { mode: 'chat', title: `合成分页 ${String(i).padStart(2, '0')}` })
  await page.goto(base + `/mindos/c/${cid}`, { waitUntil: 'networkidle' })
  const composer = page.locator('.zj-page__composer textarea')
  const originalDraft = '这一句还没有发送，整理对话时不要丢失。'
  await composer.fill(originalDraft)
  const manage = async label => {
    await page.getByRole('button', { name: '管理当前对话', exact: true }).click()
    await page.getByRole('menuitem', { name: label, exact: true }).click()
  }
  await manage('重命名')
  const rename = page.getByRole('dialog', { name: '重命名对话' })
  await rename.getByLabel('对话名称', { exact: true }).fill('合成修改后的名称')
  let response = page.waitForResponse(r => r.url() === `${base}/api/mindos/conversations/${cid}` && r.request().method() === 'PATCH')
  await rename.getByLabel('对话名称', { exact: true }).press('Enter')
  await done(response)
  await rename.waitFor({ state: 'hidden' })
  assert.equal(await composer.inputValue(), originalDraft)
  assert.equal(await page.locator('.zj-page__title').textContent(), '合成修改后的名称')
  await manage('重命名')
  await rename.getByLabel('对话名称', { exact: true }).fill('不应该保存的名称')
  await rename.getByLabel('对话名称', { exact: true }).press('Escape')
  await rename.waitFor({ state: 'hidden' })
  assert.equal((await getCurrent()).title, '合成修改后的名称')

  response = page.waitForResponse(r => r.url() === `${base}/api/mindos/conversations/${cid}` && r.request().method() === 'PATCH')
  await manage('置顶')
  await done(response)
  await page.waitForFunction(() => document.querySelector('.zj-convs__item')?.textContent.includes('合成修改后的名称'))
  assert.ok((await getCurrent()).pinnedAt)
  const manageStatus = async action => {
    const pending = page.waitForResponse(r => r.url() === `${base}/api/mindos/conversations/${cid}` && r.request().method() === 'PATCH')
    await page.getByRole('button', { name: '管理当前对话', exact: true }).click()
    await page.getByRole('menuitem').filter({ hasText: action }).click()
    await done(pending)
  }
  const messagesBefore = await page.locator('[data-message-id]').count()
  await manageStatus('归档')
  await page.getByTestId('current-archived').waitFor()
  assert.equal(await composer.inputValue(), originalDraft)
  assert.equal(await page.locator('[data-message-id]').count(), messagesBefore)
  assert.equal(await page.getByRole('dialog').count(), 0, 'archive must not add a confirmation dialog')
  response = page.waitForResponse(r => r.url() === `${base}/api/mindos/conversations/${cid}` && r.request().method() === 'PATCH')
  await page.locator('.zj-archive-feedback').getByRole('button', { name: '撤销', exact: true }).click()
  await done(response)
  await page.getByTestId('current-archived').waitFor({ state: 'hidden' })
  assert.equal((await getCurrent()).status, 'active')
  assert.ok((await getCurrent()).pinnedAt, 'archive and undo retain pin')
  await manageStatus('归档')
  const beforeRead = await getCurrent()
  await page.getByRole('group', { name: '对话分组' }).getByRole('button', { name: '已归档', exact: true }).click()
  const search = page.getByRole('searchbox', { name: '搜索对话标题和正文' })
  response = page.waitForResponse(r => r.url().includes('/api/mindos/conversations?') && new URL(r.url()).searchParams.get('q') === '海湾')
  await search.fill('海湾')
  await done(response)
  assert.equal(new URL((await response).url()).searchParams.get('status'), 'all')
  const result = page.locator('.zj-convs__item').filter({ hasText: '海湾' })
  await result.waitFor()
  assert.equal(await result.locator('.zj-convs__match').textContent(), '正文：明天我去海湾活动看看；先了解大家的作品，不急着确定合作。')
  await result.click()
  await page.locator('.zj-turn--search-hit').waitFor()
  assert.match(await page.locator('.zj-turn--search-hit').textContent(), /海湾/)
  assert.equal(await composer.inputValue(), originalDraft)
  assert.equal((await getCurrent()).status, 'archived')
  assert.equal((await getCurrent()).updatedAt, beforeRead.updatedAt, 'reading a search result must not restore/bump the archive')
  await page.screenshot({ path: `${output}/desktop-search.png`, fullPage: true })
  await page.getByRole('button', { name: '清除搜索', exact: true }).click()
  assert.equal(await page.getByRole('group', { name: '对话分组' }).getByRole('button', { name: '已归档', exact: true }).getAttribute('aria-pressed'), 'true')
  await page.getByRole('group', { name: '对话分组' }).getByRole('button', { name: '最近', exact: true }).click()
  await page.getByRole('button', { name: '加载更多', exact: true }).waitFor()
  assert.equal(await page.locator('.zj-convs__item').count(), 30)
  await page.getByRole('button', { name: '加载更多', exact: true }).click()
  await page.waitForFunction(() => document.querySelectorAll('.zj-convs__item').length > 30)
  const total = (await get('/api/mindos/conversations?status=active&limit=200')).total
  assert.equal(await page.locator('.zj-convs__item').count(), total)

  // Rename conflict keeps the entered name; re-submit uses the refreshed revision.
  await manage('重命名')
  await rename.getByLabel('对话名称', { exact: true }).fill('我输入的新名称')
  let current = await getCurrent()
  await patch(cid, { title: '别处更新的名称', expectedRevision: current.metadataRevision })
  const conflict = page.waitForResponse(r => r.url() === `${base}/api/mindos/conversations/${cid}` && r.request().method() === 'PATCH')
  await rename.getByRole('button', { name: '保存名称', exact: true }).click()
  assert.equal((await conflict).status(), 409)
  await rename.getByRole('alert').waitFor()
  assert.equal(await rename.getByLabel('对话名称', { exact: true }).inputValue(), '我输入的新名称')
  response = page.waitForResponse(r => r.url() === `${base}/api/mindos/conversations/${cid}` && r.request().method() === 'PATCH')
  await rename.getByRole('button', { name: '保存名称', exact: true }).click()
  await done(response)
  await rename.waitFor({ state: 'hidden' })
  assert.equal(await composer.inputValue(), originalDraft)

  await page.setViewportSize({ width: 390, height: 844 })
  await page.waitForTimeout(350) // let the app's responsive sidebar transition settle
  await page.locator('.zj-page__side-toggle').click()
  await page.getByRole('group', { name: '对话分组' }).getByRole('button', { name: '已归档', exact: true }).click()
  const rowMore = page.getByRole('button', { name: '管理对话 我输入的新名称', exact: true })
  await rowMore.waitFor()
  await rowMore.click()
  const menu = page.getByRole('menu', { name: '管理对话 我输入的新名称', exact: true })
  await menu.waitFor()
  assert.equal(await menu.getByRole('menuitem').count(), 4)
  const box = await menu.boundingBox()
  assert.ok(box.x >= 0 && box.x + box.width <= 390 && box.y >= 0 && box.y + box.height <= 844)
  assert.equal(await menu.evaluate(el => el.scrollHeight <= el.clientHeight + 1), true, 'four management actions must not be squeezed to one visible row')
  await menu.press('Escape')
  assert.equal(await rowMore.evaluate(el => document.activeElement === el), true)
  await rowMore.click()
  await menu.waitFor()
  await page.screenshot({ path: `${output}/mobile-menu.png` })
  await page.keyboard.press('Escape')
  assert.equal(await composer.inputValue(), originalDraft)
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)

  // A new persisted user message restores an archived conversation. Set only the
  // disposable fixture's model mode; the recorded fake model has no network.
  const routing = await get(`/api/mindos/conversations/${cid}/routing`)
  await request('PUT', `/api/mindos/conversations/${cid}/routing`, { mode: 'local', expectedRevision: routing.mode.revision })
  await page.locator('.zj-page__side-toggle').click()
  await page.reload({ waitUntil: 'networkidle' })
  await page.locator('.zj-turn--search-hit').waitFor()
  await composer.fill('合成测试：继续谈这次活动。')
  response = page.waitForResponse(r => r.url() === `${base}/api/mindos/conversations/${cid}/messages` && r.request().method() === 'POST')
  await composer.press('Enter')
  await done(response)
  await page.getByRole('button', { name: '停止', exact: true }).waitFor({ state: 'hidden' })
  await page.getByTestId('current-archived').waitFor({ state: 'hidden' })
  assert.equal((await getCurrent()).status, 'active')
  assert.equal(new URL(page.url()).searchParams.has('message'), false)

  // The new-conversation composer has its own session draft during search navigation.
  await page.goto(base + '/mindos/chat', { waitUntil: 'networkidle' })
  const landingDraft = '新对话还没发送，搜索以后还要接着写。'
  await composer.fill(landingDraft)
  await page.locator('.zj-page__side-toggle').click()
  let releaseOld
  let startedOld
  const oldRequest = new Promise(resolve => { startedOld = resolve })
  const oldGate = new Promise(resolve => { releaseOld = resolve })
  await page.route('**/api/mindos/conversations?**', async route => {
    if (new URL(route.request().url()).searchParams.get('q') === '合成分页 01') {
      const result = await route.fetch()
      startedOld()
      await oldGate
      try { await route.fulfill({ response: result }) } catch { /* the client correctly aborted this obsolete request */ }
    } else await route.continue()
  })
  await search.fill('合成分页 01')
  await oldRequest
  response = page.waitForResponse(r => r.url().includes('/api/mindos/conversations?') && new URL(r.url()).searchParams.get('q') === '我输入的新名称')
  await search.fill('我输入的新名称')
  await done(response)
  releaseOld()
  await page.waitForTimeout(200)
  assert.equal(await page.locator('.zj-convs__item').count(), 1)
  assert.match(await page.locator('.zj-convs__item').textContent(), /我输入的新名称/)
  await page.locator('.zj-convs__item').click()
  await page.locator('.zj-page__title').filter({ hasText: '我输入的新名称' }).waitFor()
  await page.locator('.zj-page__side-toggle').click()
  await page.getByRole('button', { name: '新对话', exact: true }).click()
  await page.waitForURL('**/mindos/chat')
  assert.equal(await composer.inputValue(), landingDraft)

  // A collapsed narrow sidebar still exposes archive Undo next to current status.
  await page.goto(base + `/mindos/c/${cid}`, { waitUntil: 'networkidle' })
  await composer.fill('手机上尚未发送的内容。')
  await manageStatus('归档')
  const mobileUndo = page.locator('.zj-page__mobile-undo')
  await mobileUndo.waitFor()
  assert.equal(await mobileUndo.isVisible(), true)
  assert.equal(await composer.inputValue(), '手机上尚未发送的内容。')
  await page.screenshot({ path: `${output}/mobile-archive-undo.png`, fullPage: true })
  response = page.waitForResponse(r => r.url() === `${base}/api/mindos/conversations/${cid}` && r.request().method() === 'PATCH')
  await mobileUndo.getByRole('button', { name: '撤销', exact: true }).click()
  await done(response)
  await page.getByTestId('current-archived').waitFor({ state: 'hidden' })
  assert.equal(await composer.inputValue(), '手机上尚未发送的内容。')
  assert.deepEqual(errors, [])
  console.log('conversation management UI: rename/Esc/conflict, archive/undo/read, pins, preserved existing+landing drafts, stale search rejection, all-status search/target, pagination, narrow menu/Undo and new-user restore passed')
} finally { await browser.close() }
