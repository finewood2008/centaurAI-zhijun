// Run after: PYTHONPATH=backend backend/.venv/bin/python -m tests.matters_fixture
// Uses only the disposable real-API fixture on 8774. Never send a chat or invoke a model.
import assert from 'node:assert/strict'
import { mkdtemp, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { chromium } from 'playwright'

const base = 'http://127.0.0.1:8774'
const initialHealth = await (await fetch(base + '/api/health')).json()
assert.equal(initialHealth.version, 'matters-fixture', 'STOP: only the disposable matters fixture may be tested')
const fixture = await (await fetch(base + '/__fixture')).json()
for (const key of ['conversationId', 'otherConversationId', 'replyMessageId', 'replyContent']) assert.ok(fixture[key], 'missing fixture ' + key)
assert.ok(fixture.replyContent.length > 180, 'full-reply test needs content beyond the drawer preview')
const cid = fixture.conversationId, otherId = fixture.otherConversationId
assert.notEqual(cid, otherId)
const dir = await mkdtemp(join(tmpdir(), 'zhijun-matters-e2e-'))
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, acceptDownloads: true })
const page = await context.newPage()
const errors = [], writes = [], forbidden = []
page.on('pageerror', error => errors.push(error.message))
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url())
  if (url.origin !== base) { forbidden.push(request.url()); return route.abort() }
  if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
    // This existing endpoint only reserves/selects a local reminder slot; it does not call a model.
    if (request.method() === 'POST' && /^\/api\/mindos\/conversations\/[^/]+\/memory\/attention$/.test(url.pathname)) return route.continue()
    writes.push({ path: url.pathname, method: request.method(), body: request.postDataJSON() })
    // UI management is local only. An accidental model request must fail before it is delivered.
    const allowed = /^\/api\/mindos\/(?:matters(?:\/[^/]+(?:\/artifacts)?)?|artifacts\/[^/]+|conversations\/[^/]+\/matter)$/
    if (!allowed.test(url.pathname)) { forbidden.push(request.method() + ' ' + url.pathname); return route.abort() }
  }
  return route.continue()
})
const get = async path => {
  const response = await context.request.get(base + path)
  assert.equal(response.ok(), true, await response.text())
  return response.json()
}
const bindingPath = id => '/api/mindos/conversations/' + id + '/matter'
const composer = page.locator('.zj-composer textarea').first()
const drawer = page.getByRole('dialog', { name: '事情与成果', exact: true })
const open = async () => { await page.locator('.matter-trigger').click(); await drawer.waitFor() }
const close = async () => { await drawer.getByRole('button', { name: '关闭事情与成果', exact: true }).click(); await drawer.waitFor({ state: 'hidden' }) }
const settle = async () => { await page.waitForFunction(() => !document.querySelector('.matter-workspace[aria-busy="true"]')) }
async function create(title) {
  const connect = drawer.locator('details.connect')
  if (!(await connect.evaluate(el => el.open))) await connect.locator('summary').click()
  // The same disposable server may be reused after a failed run. Explicitly detach its old fixture item.
  if (await drawer.locator('.matter-title').count()) {
    await connect.locator('select').selectOption('')
    await connect.getByRole('button', { name: '解除本段关联', exact: true }).click()
    await drawer.locator('.matter-title').waitFor({ state: 'hidden' })
    await settle()
  }
  await connect.getByLabel('新事情的名称', { exact: true }).fill(title)
  await connect.getByRole('button', { name: '新建并关联本段对话', exact: true }).click()
  await drawer.locator('.matter-title h3').filter({ hasText: title }).waitFor()
  await settle()
}
async function chooseConversation(id, title) {
  await page.locator('.zj-convs__item').filter({ hasText: title }).click()
  await page.waitForURL(base + '/mindos/c/' + id)
}
async function assertNoModels() {
  const info = await get('/__fixture')
  assert.equal(info.onlineRequests.length, fixture.onlineRequests.length, 'no external model calls')
  assert.equal(info.localRequests, fixture.localRequests, 'no local model calls')
  assert.deepEqual(forbidden, [], 'no unapproved request, background send, or external network')
}

try {
  const detail = await get('/api/mindos/conversations/' + cid)
  const otherDetail = await get('/api/mindos/conversations/' + otherId)
  const originalTitle = detail.conversation.title, otherTitle = otherDetail.conversation.title
  await page.goto(base + '/mindos/c/' + cid, { waitUntil: 'networkidle' })
  await composer.fill('这是我已经写下、尚未发送的具体安排。')
  const openingWrites = writes.length
  await open(); await settle()
  assert.equal(writes.length, openingWrites, 'opening the drawer must only read local records')
  assert.equal(await drawer.getByLabel('新事情的名称', { exact: true }).inputValue(), '')
  await assertNoModels()

  const matterTitle = '合成事项：与合伙人明确职责和授权 ' + Date.now().toString(36)
  await create(matterTitle)
  const binding = await get(bindingPath(cid)), matterId = binding.matter.id
  assert.equal(binding.matter.title, matterTitle)
  assert.ok(binding.bindingRevision > 0)
  assert.equal(writes.filter(r => r.path === '/api/mindos/matters').length, 1)
  await drawer.locator('summary').filter({ hasText: /^背景与进展/ }).click()
  await drawer.getByLabel('希望达成什么', { exact: true }).fill('先明确可独立决定与需共同商量的事项。')
  await drawer.getByLabel('下一步', { exact: true }).fill('准备一份双方可以核对的沟通提纲。')
  await drawer.getByRole('button', { name: '保存事情记录', exact: true }).click()
  await settle()
  assert.equal((await get(bindingPath(cid))).matter.nextStep, '准备一份双方可以核对的沟通提纲。')
  const beforePrepare = writes.length
  await drawer.getByRole('button', { name: '重要沟通提纲', exact: true }).click()
  await drawer.waitFor({ state: 'hidden' })
  const preparedText = await composer.inputValue()
  assert.ok(preparedText.startsWith('这是我已经写下、尚未发送的具体安排。'), 'preparation preserves existing text')
  assert.ok(preparedText.includes('帮我准备一份重要沟通提纲'))
  assert.equal(writes.length, beforePrepare, 'preparation fills only; it does not send or call a model')

  const assistant = page.locator('[data-message-id="' + fixture.replyMessageId + '"]')
  await assistant.getByRole('button', { name: '留下文稿', exact: true }).click()
  await drawer.waitFor()
  await drawer.locator('.pending select').selectOption('communication')
  await drawer.getByRole('button', { name: '保存为可编辑文稿', exact: true }).click()
  await drawer.getByLabel('完整正文', { exact: true }).waitFor()
  assert.equal(await drawer.getByLabel('完整正文', { exact: true }).inputValue(), fixture.replyContent, 'never save the 180-char preview as the complete document')
  const savedItems = (await get('/api/mindos/matters/' + matterId + '/artifacts')).items
  assert.equal(savedItems.length, 1)
  const artifactId = savedItems[0].id
  assert.equal(savedItems[0].kind, 'communication')
  assert.equal(savedItems[0].sourceMessageId, fixture.replyMessageId)
  assert.equal(savedItems[0].sourceConversationId, cid)
  const artifactRequest = writes.find(r => r.path.endsWith('/artifacts') && r.method === 'POST')
  assert.equal('markdown' in artifactRequest.body, false, 'server reads full stored message; client does not send preview')

  const editedTitle = '合成沟通提纲-用户核对稿'
  const edited = fixture.replyContent + '\n\n## 我补充的安排\n先核对各自理解，再决定尝试范围。暂未商定的时间仍留空。\n'
  await drawer.getByLabel('文稿名称', { exact: true }).fill(editedTitle)
  await drawer.getByLabel('完整正文', { exact: true }).fill(edited)
  await drawer.getByRole('button', { name: '保存文稿', exact: true }).click()
  await settle()
  const saved = (await get('/api/mindos/matters/' + matterId + '/artifacts')).items.find(x => x.id === artifactId)
  assert.equal(saved.markdown, edited); assert.equal(saved.userEdited, true)
  assert.equal(saved.sourceMessageId, fixture.replyMessageId)
  await close()
  assert.equal(await composer.inputValue(), preparedText, 'artifact operations preserve the unsent conversation draft')
  await page.reload({ waitUntil: 'networkidle' })
  assert.equal(await composer.inputValue(), preparedText, 'refresh restores the unsent conversation draft')
  await open(); await settle()
  await drawer.locator('.artifact-list button').filter({ hasText: editedTitle }).click()
  assert.equal(await drawer.getByLabel('完整正文', { exact: true }).inputValue(), edited, 'saved manual edits survive refresh')
  const downloadEvent = page.waitForEvent('download')
  await drawer.getByRole('button', { name: '下载 .md', exact: true }).click()
  const download = await downloadEvent
  assert.equal(download.suggestedFilename(), editedTitle + '.md')
  const downloaded = join(dir, 'download.md')
  await download.saveAs(downloaded)
  assert.equal(await readFile(downloaded, 'utf8'), edited)
  await page.screenshot({ path: join(dir, 'document-desktop.png'), fullPage: true })
  await close()

  // Select an existing conversation through the app, then create a distinct binding.
  await chooseConversation(otherId, otherTitle)
  await open(); await settle()
  assert.equal(await drawer.locator('.artifact-list button').count(), 0, 'another conversation does not inherit the previous artifact')
  const otherMatterTitle = '合成事项：独立的新安排'
  await create(otherMatterTitle)
  const otherBinding = await get(bindingPath(otherId))
  assert.notEqual(otherBinding.matter.id, matterId)
  await close()

  // Hold the old conversation's read while returning to the current one. No private store is mocked.
  let release, observed
  const held = new Promise(resolve => { release = resolve })
  const started = new Promise(resolve => { observed = resolve })
  const oldBindingUrl = base + bindingPath(cid)
  await page.route(oldBindingUrl, async route => {
    const response = await route.fetch()
    observed()
    await held
    await route.fulfill({ response })
  }, { times: 1 })
  await chooseConversation(cid, originalTitle)
  await started
  await chooseConversation(otherId, otherTitle)
  await page.locator('.matter-trigger').filter({ hasText: otherMatterTitle }).waitFor()
  release()
  await page.waitForLoadState('networkidle')
  assert.ok((await page.locator('.matter-trigger').innerText()).includes(otherMatterTitle), 'late data cannot bind a different matter to the new conversation')
  assert.equal(await drawer.isVisible(), false, 'switching conversations does not reopen an old drawer')
  await page.unroute(oldBindingUrl)
  await chooseConversation(cid, originalTitle)
  await open(); await settle()
  await drawer.locator('.artifact-list button').filter({ hasText: editedTitle }).click()
  assert.equal(await drawer.getByLabel('完整正文', { exact: true }).inputValue(), edited)

  await page.setViewportSize({ width: 390, height: 844 })
  await drawer.getByRole('button', { name: '阅读', exact: true }).click()
  const document = drawer.locator('.document')
  await document.locator('.zj-msg__body').waitFor()
  await document.scrollIntoViewIfNeeded()
  assert.ok((await document.innerText()).includes('先核对各自理解'), 'the complete edited document is readable on narrow screens')
  assert.equal(await drawer.evaluate(el => el.scrollWidth <= el.clientWidth + 1), true, 'drawer content does not overflow horizontally')
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, 'page fits the narrow viewport')
  const box = await drawer.boundingBox()
  assert.ok(box && box.x >= -1 && box.x + box.width <= 391 && box.y >= -1 && box.height <= 845)
  await page.screenshot({ path: join(dir, 'document-mobile.png'), fullPage: true })
  await close()
  await composer.scrollIntoViewIfNeeded()
  const inputBox = await composer.boundingBox()
  assert.ok(inputBox && inputBox.x >= 0 && inputBox.x + inputBox.width <= 391 && inputBox.y + inputBox.height <= 845, 'composer is accessible after closing the document')

  await page.setViewportSize({ width: 1440, height: 1050 })
  await page.goto(base + '/mindos/', { waitUntil: 'networkidle' })
  const homeMatter = page.locator('.matters-home article').filter({ hasText: matterTitle })
  await homeMatter.waitFor()
  assert.ok((await homeMatter.innerText()).includes('准备一份双方可以核对的沟通提纲'))
  await homeMatter.getByRole('button', { name: /接着推进/ }).click()
  await page.waitForURL(base + '/mindos/c/' + cid)
  await page.locator('.matter-trigger').filter({ hasText: matterTitle }).waitFor()
  await assertNoModels()
  assert.deepEqual(errors, [])
  console.log('matters E2E passed: read-only drawer; create/bind; preserve input + prepare without sending; complete reply/edit/refresh/download; stale conversation isolation; narrow document; homepage resume; zero model calls. Screenshots: ' + dir)
} catch (error) {
  await page.screenshot({ path: join(dir, 'failure.png'), fullPage: true })
  console.error('Synthetic fixture failure screenshot: ' + dir)
  console.error((await page.locator('body').innerText()).slice(-7000))
  throw error
} finally { await browser.close() }
