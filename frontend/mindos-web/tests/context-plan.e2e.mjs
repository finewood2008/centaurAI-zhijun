// Browser contract checks: disposable server only; deterministic SSE/preview doubles, no model calls.
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

const base = 'http://127.0.0.1:8772'
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort())
const dir = '../../data/diagnostics/context-plan'
mkdirSync(dir, { recursive: true })
const health = await (await context.request.get(base + '/api/health')).json()
assert.equal(health.version, 'task-routing-fixture')
const conversation = await (await context.request.post(base + '/api/mindos/conversations', { data: { title: '来源与补查（隔离合成案例）' } })).json()
const cid = conversation.id
const path = '/api/mindos/conversations/' + cid
const modeState = await (await context.request.get(base + path + '/routing')).json()
const modeUpdate = await context.request.put(base + path + '/routing', { data: { mode: 'online', expectedRevision: modeState.mode.revision, acknowledge: true, serviceId: modeState.service.id, freshContext: true } })
assert.equal(modeUpdate.ok(), true, await modeUpdate.text())
const now = new Date().toISOString()
const service = { id: 'synthetic-online', name: '合成在线服务', model: 'synthetic-main', external: true }
const clauseBasis = { scope: 'synthetic', charterId: 'charter-1', version: 3, clauseIds: ['principle-1'] }
const item = (citationId, title, text, category) => ({ citationId, kind: 'claim', id: 'claim-' + citationId, version: 'v1', title, text, category, ref: { kind: 'claim', id: 'claim-' + citationId, version: 'v1' } })
const plan = { revision: 'context-1', stage: 'initial', background: [item('p1', '当前处境', '当前项目的预算和人手都有限。', 'background')], evidence: [item('p2', '一次可撤回的探索', '我更愿意先做小范围试验，看看实际反馈。', 'evidence')], providedRefs: ['p1', 'p2'], citedRefs: [], excluded: [{ title: '未授权旧资料', reason: '未纳入当前问题' }], citationAudit: { invalidRefs: [] } }
const provenance = contextPlan => ({ contextPlan, charterBasis: clauseBasis, confirmedClaims: [], workingClaims: [], materials: [], promptChars: 150, retractedNotices: 0, charterVersion: 3, routing: { service, revision: 'route-1', purposeLabel: '日常对话', excluded: [], reason: '明确选择在线' } })
const message = (id, role, content, seq, extras = {}) => ({ id, role, content, seq, conversationId: cid, createdAt: now, status: 'complete', ...extras })
const messages = [
  message('old-user', 'user', '以前聊到的合作方式还在吗？', 1),
  message('old-assistant', 'assistant', '这是未区分提供和引用的旧回执。', 2, { provenance: { promptChars: 20, memoryContext: { intent: 'conversation', status: 'inherited', inheritedCount: 4, directCount: 0, excludedCount: 0, searched: false } } }),
]
let nonce, pending = false, granted = false, ready = false, sendCount = 0, newTurn = true
let persistedUser, persistedAssistant
const previews = [], sends = []
const preview = body => ({ revision: granted ? 'route-granted' : pending ? 'route-supplemented' : 'route-initial', conversationId: cid, purpose: 'chat', purposeLabel: '日常对话', service: body.localOnly ? { ...service, name: '合成本地模型', external: false } : service, reason: '合成上下文核对', missing: pending && !granted && !body.localOnly ? ['new-evidence'] : [], blocked: [], sources: [{ key: 'new-evidence', title: '补查找到的计划片段', text: '先小范围试验，再决定是否继续。', version: 'v2', blocked: '', kind: 'material' }], excluded: [], request: { system: '合成提示；无真实个人信息', messages: [{ role: 'user', content: body.content }] }, contextPlan: { ...plan, stage: pending ? 'supplemented' : 'initial' } })
const sse = events => events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')

await page.route(base + path, async route => {
  if (route.request().method() !== 'GET') return route.continue()
  const response = await route.fetch()
  const detail = await response.json()
  await route.fulfill({ json: { ...detail, conversation: { ...detail.conversation, messageCount: messages.length }, messages } })
})
await page.route(base + path + '/routing/preview', async route => {
  const body = route.request().postDataJSON()
  previews.push(body)
  await route.fulfill({ json: preview(body) })
})
await page.route(base + path + '/routing/grant', async route => {
  const body = route.request().postDataJSON()
  assert.deepEqual(body.keys, ['new-evidence'])
  granted = true
  await route.fulfill({ json: { ok: true } })
})
await page.route(base + path + '/messages', async route => {
  const body = route.request().postDataJSON()
  sends.push(body); sendCount += 1
  if (newTurn) {
    newTurn = false
    nonce = body.requestId
    const suffix = sendCount === 1 ? '' : '-' + sendCount
    persistedUser = message('persisted-user' + suffix, 'user', body.content, messages.length + 1, { meta: { materialRefs: [] } })
    persistedAssistant = message('persisted-assistant' + suffix, 'assistant', '补充信息需要核对，原消息已保留。', messages.length + 2, { status: 'error', provider: 'synthetic', model: service.model, external: true, meta: { requestId: nonce, replyTo: persistedUser.id, depth: body.depth, turnMode: body.mode, contextStage: 'supplemented', contextPending: { code: 'ROUTE_CONSENT_REQUIRED', stage: 'supplemented' } }, provenance: provenance(plan) })
    messages.push(persistedUser, persistedAssistant)
    pending = true
    await route.fulfill({ contentType: 'text/event-stream', body: sse([
      ['meta', { messageId: persistedAssistant.id, userMessageId: persistedUser.id, conversationId: cid, provider: 'synthetic', model: service.model, external: true, mode: 'chat', turnMode: body.mode, depth: body.depth }],
      ['provenance', provenance(plan)],
      ['error', { code: 'ROUTE_CONSENT_REQUIRED', message: persistedAssistant.content, preview: preview(body), requestId: nonce, userMessageId: persistedUser.id, messageId: persistedAssistant.id, stage: 'supplemented', retryable: true }],
    ]) })
    return
  }
  assert.equal(body.requestId, nonce)
  assert.equal(body.retryUserMessageId, persistedUser.id)
  assert.equal(body.content, persistedUser.content)
  assert.ok(granted || body.localOnly, 'supplement cannot send before explicit authorization or local choice')
  const finalPlan = { ...plan, revision: 'context-2', stage: 'supplemented', citedRefs: ['p2'], citationAudit: { invalidRefs: ['p99'] } }
  persistedAssistant.content = '先用小范围试验验证，再决定是否继续。[p2]'
  persistedAssistant.status = 'complete'
  persistedAssistant.meta.contextPending = undefined
  persistedAssistant.provenance = provenance(finalPlan)
  ready = true
  await route.fulfill({ contentType: 'text/event-stream', body: sse([
    ['meta', { messageId: persistedAssistant.id, userMessageId: persistedUser.id, conversationId: cid, provider: 'synthetic', model: service.model, external: !body.localOnly, mode: 'chat', turnMode: body.mode, depth: body.depth }],
    ['provenance', provenance({ ...finalPlan, citedRefs: [] })],
    ['token', { t: persistedAssistant.content }],
    ['provenance', provenance(finalPlan)],
    ['message_done', { messageId: persistedAssistant.id, status: 'complete' }],
  ]) })
})

try {
  await page.goto(base + '/mindos/c/' + cid, { waitUntil: 'networkidle' })
  assert.equal(await page.getByTestId('provenance-graph').count(), 0, 'sources stay collapsed by default')
  await page.getByTestId('provenance-toggle').first().click()
  await page.getByText('历史权限链关联了 4 条本体理解，不代表本轮重新读取、提供或引用了原记录。').waitFor()
  assert.equal(await page.getByTestId('context-provided').count(), 0, 'legacy lineage does not become supplied evidence')
  await page.getByTestId('provenance-toggle').first().click()
  const composer = page.locator('.zj-composer textarea')
  await composer.fill('我想继续比较这次探索的投入与风险。')
  await composer.press('Enter')
  const retry = page.getByRole('button', { name: '核对补充资料并继续', exact: true })
  await retry.waitFor()
  assert.equal(await page.locator('dialog[open]').count(), 0, 'supplement pause does not interrupt with a popup')
  assert.equal(sends.length, 1)
  await composer.fill('这是尚未发送的新想法。')
  await page.screenshot({ path: dir + '/pending-desktop.png', fullPage: true })
  await page.reload({ waitUntil: 'networkidle' })
  await retry.waitFor()
  assert.equal(await composer.inputValue(), '这是尚未发送的新想法。')
  await retry.click()
  const consent = page.getByRole('dialog', { name: '这次要让在线模型使用哪些内容？' })
  await consent.waitFor()
  assert.equal(sends.length, 1)
  await consent.getByRole('button', { name: '取消', exact: true }).click()
  assert.equal(await composer.inputValue(), '这是尚未发送的新想法。')
  assert.equal(sends.length, 1)
  await retry.click()
  await consent.waitFor()
  await consent.getByRole('button', { name: '允许所选内容用于该服务和用途', exact: true }).click()
  await page.getByText('先用小范围试验验证，再决定是否继续。[p2]', { exact: true }).waitFor()
  await retry.waitFor({ state: 'hidden' })
  assert.equal(ready, true)
  assert.equal(sends.length, 2)
  assert.equal(await page.locator('[data-message-id="persisted-user"]').count(), 1)
  assert.equal(await page.locator('[data-message-id="persisted-assistant"]').count(), 1)
  assert.ok(previews.slice(1).every(body => body.requestId === nonce && body.retryUserMessageId === persistedUser.id))
  assert.equal(await composer.inputValue(), '这是尚未发送的新想法。')
  const finalToggle = page.getByTestId('provenance-toggle').last()
  await finalToggle.filter({ hasText: '提供给模型 2 项信息 · 回答明确引用 1 项 · 已补查一次' }).waitFor()
  await finalToggle.click()
  await page.getByTestId('context-constraints').getByText('遵循的约定', { exact: true }).waitFor()
  assert.equal(await page.getByTestId('context-provided').locator('details').count(), 2)
  assert.equal(await page.getByTestId('context-cited').locator('li').count(), 1)
  await page.getByTestId('context-provided').locator('summary').last().click()
  await page.getByText('另有 1 个无法核验的引用标识，未列入明确引用。').waitFor()
  await page.getByTestId('context-cited').scrollIntoViewIfNeeded()
  await page.screenshot({ path: dir + '/sources-desktop.png', fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.waitForFunction(() => { const nav = document.querySelector('.ws-sidebar'); return nav.getAttribute('aria-hidden') === 'true' || nav.getAttribute('role') === 'dialog' })
  if (await page.getByRole('dialog', { name: '导航菜单', exact: true }).isVisible()) await page.getByRole('button', { name: '关闭导航', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.ws-sidebar').getBoundingClientRect().right <= 1)
  await page.getByTestId('context-cited').scrollIntoViewIfNeeded()
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true)
  const box = await composer.boundingBox()
  assert.ok(box && box.y >= 0 && box.y + box.height <= 844, 'composer remains visible with expanded source details')
  await page.screenshot({ path: dir + '/sources-mobile.png', fullPage: true })
  await page.reload({ waitUntil: 'networkidle' })
  assert.equal(await page.getByTestId('provenance-graph').count(), 0, 'refresh restores receipt but not forced-open panels')
  await page.getByTestId('provenance-toggle').last().filter({ hasText: '回答明确引用 1 项' }).waitFor()
  // A new paused turn may only change to local after the user explicitly chooses it.
  newTurn = true; pending = false; granted = false; ready = false
  await composer.fill('这一次我想只在本机继续分析。')
  await composer.press('Enter')
  await retry.waitFor()
  assert.equal(sends.length, 3)
  assert.equal(await page.locator('dialog[open]').count(), 0)
  await page.getByRole('button', { name: '改用本地', exact: true }).click()
  await retry.waitFor({ state: 'hidden' })
  assert.equal(sends.length, 4)
  assert.equal(sends[3].localOnly, true)
  assert.equal(sends[3].requestId, sends[2].requestId)
  assert.equal(sends[3].retryUserMessageId, persistedUser.id)
  assert.equal(granted, false, 'local retry does not grant online permission')
  assert.deepEqual(errors, [])
  const calls = await (await context.request.get(base + '/__fixture')).json()
  assert.equal(calls.onlineRequests.length, 0)
  assert.equal(calls.localRequests, 0)
  console.log('context-plan E2E: conservative legacy, pause/refresh/cancel/grant same nonce and message, final provenance, desktop/mobile passed; no model calls')
} catch (error) {
  await page.screenshot({ path: dir + '/failure.png', fullPage: true })
  console.log((await page.locator('body').innerText()).slice(-8000))
  throw error
} finally { await browser.close() }
