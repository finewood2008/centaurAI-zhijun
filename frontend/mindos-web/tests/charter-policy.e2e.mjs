import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'
const base = 'http://127.0.0.1:8772'
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort())
const page = await context.newPage()
const dir = '../../data/diagnostics/conversational-charter'
mkdirSync(dir, { recursive: true })
const errors = []
page.on('pageerror', e => errors.push(e.message))
const calls = []
page.on('request', req => { if (req.method() === 'POST' && /\/routing\/preview$|\/routing\/charter-exception$|\/messages$/.test(req.url())) calls.push({ url: req.url(), body: req.postDataJSON() }) })
const api = async (path, data, method = 'POST') => {
  const response = await context.request.fetch(base + '/api/mindos' + path, { method, ...(data === undefined ? {} : { data }) })
  assert.equal(response.ok(), true, await response.text())
  return response.json()
}
const info = async () => (await context.request.get(base + '/__fixture')).json()
try {
  assert.equal((await (await context.request.get(base + '/api/health')).json()).version, 'task-routing-fixture')
  const conv = await api('/conversations', { title: '章程本轮例外（合成测试）', mode: 'chat' })
  const path = '/conversations/' + conv.id
  let result = await api(path + '/charter/workspace/start', { requestId: crypto.randomUUID() })
  const workspacePath = '/conversations/' + result.conversationId + '/charter/workspace/' + result.workspace.id
  const text = '默认仅在本机处理；需要在线处理时，先让我明确确认本轮例外。'
  const clause = { id: 'e2e-local-boundary', section: '模型处理边界', text, kind: 'boundary', scope: 'global', control: 'local_only', sources: [], quote: text, origin: 'manual' }
  result = await api(workspacePath, { revision: result.workspace.revision, requestId: crypto.randomUUID(), sourceText: text, clauses: [clause] }, 'PUT')
  result = await api(workspacePath + '/publish', { revision: result.workspace.revision, requestId: crypto.randomUUID(), selectedClauseIds: [clause.id] })
  const version = result.charter.version
  const state = await api(path + '/routing', undefined, 'GET')
  await api(path + '/routing', { mode: 'online', expectedRevision: state.mode.revision, acknowledge: true, serviceId: state.service.id, freshContext: true }, 'PUT')
  const before = await info()
  await page.goto(base + '/mindos/c/' + conv.id, { waitUntil: 'networkidle' })
  const composer = page.locator('.zj-page__composer textarea')
  await composer.fill('合成测试：请帮我安排一次周末散步。')
  await composer.press('Enter')
  const conflict = page.getByRole('dialog', { name: '这次处理与你的章程约定不同', exact: true })
  await conflict.waitFor()
  assert.equal((await info()).onlineRequests.length, before.onlineRequests.length)
  await page.screenshot({ path: dir + '/charter-exception.png' })
  await conflict.getByRole('button', { name: '仅本轮例外，继续核对资料权限', exact: true }).click()
  const consent = page.getByRole('dialog', { name: '这次要让在线模型使用哪些内容？', exact: true })
  await consent.waitFor()
  assert.equal((await info()).onlineRequests.length, before.onlineRequests.length, 'exception never implies content authorization')
  await page.screenshot({ path: dir + '/charter-sources-consent.png' })
  await consent.getByRole('button', { name: '允许所选内容用于该服务和用途', exact: true }).click()
  await page.getByText('合成回复：先澄清约束，再比较选择，不把愿望当事实。', { exact: true }).waitFor()
  const first = calls.find(c => c.url.endsWith('/routing/preview')).body
  const sent = calls.find(c => c.url.endsWith('/messages')).body
  assert.ok(first.requestId)
  assert.equal(sent.requestId, first.requestId)
  assert.ok(sent.charterExceptionId)
  assert.equal((await info()).onlineRequests.length, before.onlineRequests.length + 1)
  const charter = await api('/growth/charter', undefined, 'GET')
  assert.equal(charter.currentCharter.version, version)
  await page.getByTestId('provenance-toggle').last().click()
  await page.getByTestId('provenance-charter-clauses').getByRole('link', { name: /查看当时第|人生章程第/ }).waitFor()

  await composer.fill('合成测试：再帮我想一个饭后散步路线。')
  await composer.press('Enter')
  await conflict.waitFor()
  await conflict.getByRole('button', { name: '遵守章程，仅本地处理', exact: true }).click()
  await page.waitForFunction(() => document.body.innerText.split('合成回复：先澄清约束').length >= 3)
  const after = await info()
  assert.equal(after.onlineRequests.length, before.onlineRequests.length + 1, 'previous exception cannot authorize a later turn')
  assert.ok(after.localRequests > before.localRequests)
  assert.deepEqual(errors, [])
  console.log('charter policy E2E: exception before independent source consent, stable preview/send nonce, actual receipt and single-turn scope passed')
} catch (e) {
  await page.screenshot({ path: dir + '/policy-failure.png', fullPage: true })
  console.log((await page.locator('body').innerText()).slice(-5000))
  throw e
} finally { await browser.close() }
