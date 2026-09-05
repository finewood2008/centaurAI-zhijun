// Run against the disposable backend/tests/task_routing_fixture.py on port 8772.
// Every response is recorded by a synthetic provider; never contact live models/data.
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

const base = 'http://127.0.0.1:8772'
const dir = '../../data/diagnostics/memory-context'
mkdirSync(dir, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort())
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
const get = async path => {
  const response = await context.request.get(base + path)
  assert.equal(response.ok(), true, await response.text())
  return response.json()
}
const post = async (path, data) => {
  const response = await context.request.post(base + path, { data })
  assert.equal(response.ok(), true, await response.text())
  return response.json()
}
const local = async cid => {
  const path = `/api/mindos/conversations/${cid}/routing`
  const state = await get(path)
  const response = await context.request.put(base + path, { data: { mode: 'local', expectedRevision: state.mode.revision } })
  assert.equal(response.ok(), true, await response.text())
}
const composer = page.locator('.zj-page__composer textarea')
const send = async (cid, question) => {
  const requestDone = page.waitForResponse(response => response.url() === `${base}/api/mindos/conversations/${cid}/messages` && response.request().method() === 'POST')
  await composer.fill(question)
  await composer.press('Enter')
  const response = await requestDone
  assert.equal(response.ok(), true, await response.text())
  await response.finished()
  await page.getByRole('button', { name: '停止', exact: true }).waitFor({ state: 'hidden' })
  const messages = (await get(`/api/mindos/conversations/${cid}`)).messages
  const reply = messages.filter(message => message.role === 'assistant').at(-1)
  assert.equal(reply.status, 'complete')
  assert.equal(reply.content, '合成回复：先澄清约束，再比较选择，不把愿望当事实。')
  const info = await get('/__fixture')
  const payload = info.localRequestPayloads.findLast(request => request.messages.at(-1)?.content === question)
  assert.ok(payload, 'assert the actual local-provider boundary, not just a preview')
  return { provenance: reply.provenance, payload }
}

try {
  const health = await get('/api/health')
  assert.equal(health.version, 'task-routing-fixture', 'refuse to write synthetic data outside the disposable fixture')
  const initial = await get('/__fixture')
  const state = await get('/api/mindos/growth/charter')
  const charter = await post('/api/mindos/growth/charter', {
    expectedVersion: state.currentCharter?.version ?? 0,
    requestId: `memory-e2e-${Date.now()}`,
    vision: '', roles: ['合成案例：社区教师'], goals: ['合成目标：每周留出一个晚上陪伴家人'],
    principles: [], challengeStyle: '', boundaries: [], quietDomains: [],
  })
  const checked = `已核对人生章程第 ${charter.version} 版`
  await page.goto(base + '/mindos/me/charter', { waitUntil: 'networkidle' })
  const creation = page.waitForRequest(request => request.url() === base + '/api/mindos/conversations' && request.method() === 'POST')
  await page.getByRole('button', { name: '通过对话修改', exact: true }).click()
  assert.equal((await creation).postDataJSON().taskContext, 'charter', 'explicit charter entry persists the task, not a copied old transcript')
  await composer.waitFor()
  const charterCid = new URL(page.url()).pathname.split('/').at(-1)
  await local(charterCid)
  await page.reload({ waitUntil: 'networkidle' })
  for (const question of ['章程有哪些还没填写？', '还有哪些空着？']) {
    const { provenance, payload } = await send(charterCid, question)
    assert.equal(provenance.memoryContext.intent, 'charter')
    assert.equal(provenance.memoryContext.charterComplete, true)
    assert.equal(provenance.charterVersion, charter.version)
    assert.equal(provenance.memoryContext.directCount, 0, 'charter state is not counted as an ontology claim')
    assert.ok(payload.system.includes('合成案例：社区教师'))
    assert.ok(payload.system.includes('合成目标：每周留出一个晚上陪伴家人'))
    assert.equal(payload.system.match(/待完善（尚未填写，不代表没有限制）/g)?.length, 5)
    assert.match(payload.system, /实际读取的 7 栏/)
    await page.locator('.zj-prov__summary').last().filter({ hasText: checked }).waitFor()
  }
  const beforeReload = (await get('/__fixture')).localRequests
  await page.reload({ waitUntil: 'networkidle' })
  assert.equal(await page.locator('.zj-prov__summary').filter({ hasText: checked }).count(), 2)
  assert.equal((await get('/__fixture')).localRequests, beforeReload, 'refresh restores saved receipts without generating a new reply')
  await page.getByTestId('provenance-toggle').last().click()
  await page.getByRole('link', { name: checked, exact: true }).waitFor()
  assert.equal(await page.getByTestId('provenance-graph').count(), 0, 'charter-only references must not show a contradictory empty ontology graph')
  await page.screenshot({ path: `${dir}/charter-state-desktop.png`, fullPage: true })

  // An explicit self-overview covers independent confirmed sections, including wishes as wishes.
  const seeded = await get('/api/mindos/ontology/claims')
  const seed = values => seeded.items.find(claim => claim.content === values.content) ?? post('/api/mindos/ontology/claims', values)
  const identity = await seed({ content: '合成案例：我是一名社区教师', section: 'who', layer: 'self_declared' })
  const principle = await seed({ content: '合成案例：我认同做决定前尊重当事人的意愿', section: 'principles', layer: 'self_declared' })
  const wish = await seed({ content: '合成案例：我希望以后更从容地公开表达', section: 'direction', layer: 'aspirational' })
  const conversation = await post('/api/mindos/conversations', { title: '本体参与对话验证（合成）' })
  await local(conversation.id)
  await page.goto(`${base}/mindos/c/${conversation.id}`, { waitUntil: 'networkidle' })
  const overview = await send(conversation.id, '你目前怎么看我？')
  assert.equal(overview.provenance.memoryContext.intent, 'self_overview')
  for (const record of [identity, principle, wish]) {
    assert.ok(overview.provenance.confirmedClaims.some(claim => claim.id === record.id))
    assert.ok(overview.payload.system.includes(record.content))
  }
  assert.ok(overview.provenance.memoryContext.directCount >= 3)
  await page.locator('.zj-prov__summary').last().filter({ hasText: /本轮直接参考了/ }).waitFor()
  const followup = await send(conversation.id, '那星桥项目这件事呢？')
  assert.ok(followup.provenance.confirmedClaims.some(claim => claim.id === initial.claimId))
  assert.ok(followup.payload.system.includes('星桥项目只是工作安排，不代表我的个人追求'))
  assert.ok(followup.provenance.memoryContext.inheritedCount > 0)
  await page.locator('.zj-prov__summary').last().filter({ hasText: /本轮直接参考了.*沿用了近期对话/ }).waitFor()
  await page.getByTestId('provenance-toggle').last().click()
  await page.getByTestId('provenance-inherited').last().waitFor()
  await page.screenshot({ path: `${dir}/ontology-context-desktop.png`, fullPage: true })
  await page.reload({ waitUntil: 'networkidle' })
  await page.locator('.zj-prov__summary').last().filter({ hasText: /沿用了近期对话/ }).waitFor()

  await page.setViewportSize({ width: 390, height: 844 })
  if (await page.getByRole('dialog', { name: '导航菜单', exact: true }).isVisible()) {
    await page.getByRole('button', { name: '关闭导航', exact: true }).click()
  }
  // Let the desktop-to-mobile sidebar transition finish before inspecting readability.
  await page.waitForFunction(() => document.querySelector('.ws-sidebar').getBoundingClientRect().right <= 0)
  await page.getByTestId('provenance-toggle').last().click()
  await page.getByTestId('provenance-inherited').last().waitFor()
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, '390px layout must not overflow horizontally')
  const inputBox = await composer.boundingBox()
  assert.ok(inputBox && inputBox.width >= 200 && inputBox.x >= 0 && inputBox.y + inputBox.height <= 844)
  assert.ok(await page.locator('.zj-prov__summary').last().isVisible())
  await page.screenshot({ path: `${dir}/ontology-context-mobile.png`, fullPage: true })
  const final = await get('/__fixture')
  assert.equal(final.onlineRequests.length, initial.onlineRequests.length, 'local checks must never invoke the online provider')
  assert.deepEqual(errors, [])
  console.log('memory context E2E: explicit charter task, complete real-field snapshot, short follow-up, saved receipts, self-overview, direct/inherited claim evidence, local-only payloads, desktop/mobile passed')
} catch (error) {
  await page.screenshot({ path: `${dir}/failure.png`, fullPage: true })
  console.log((await page.locator('body').innerText()).slice(-7000))
  throw error
} finally {
  await browser.close()
}
