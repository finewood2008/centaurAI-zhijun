// Built desktop UI, synthetic IPC and disposable browser storage only.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdir, readFile } from 'node:fs/promises'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { expect } from 'playwright/test'
const root = fileURLToPath(new URL('../dist-desktop/', import.meta.url))
const screenshots = '/private/tmp/zhijun-settings-ontology'
await mkdir(screenshots, { recursive: true })
const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }
const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://127.0.0.1').pathname
  const file = resolve(root, '.' + path)
  if (!file.startsWith(root.endsWith(sep) ? root : root + sep)) return res.writeHead(403).end()
  try { res.setHeader('content-type', mime[extname(file)] || 'application/octet-stream'); res.end(await readFile(file)) }
  catch { res.writeHead(404).end() }
})
await new Promise(done => server.listen(0, '127.0.0.1', done))
const origin = `http://127.0.0.1:${server.address().port}`
let browser, page
try {
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  page = await context.newPage()
  const errors = [], unexpected = []
  page.on('pageerror', e => errors.push(e.message))
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin === origin && !url.pathname.startsWith('/api/')) return route.continue()
    unexpected.push(url.href); return route.abort()
  })
  await page.addInitScript(() => {
    if (!localStorage.getItem('zhijun.me.view')) localStorage.setItem('zhijun.me.view', 'list')
    let owner = 'A', generation = 7, sequence = 1
    let snapshot = { protocolVersion: 1, generation, sequence, phase: 'selecting_device', environment: 'simulation',
      subject: { accountId: 'synthetic-account' }, capabilities: { product: false, provisioning: true } }
    if (sessionStorage.getItem('synthetic-connected') === 'yes') snapshot = { ...snapshot, phase: 'ready',
      subject: { accountId: 'synthetic-account', deviceId: 'A', deviceName: '测试盒子 A', workspaceId: 'workspace-A' },
      capabilities: { product: true, materialsRead: false, provisioning: true } }
    const subscribers = new Set(), jobs = new Map()
    const stats = { requests: [], controls: [], late: [], fetches: [], provisions: 0, claims: 0 }
    window.__settingsStats = stats
    window.fetch = async url => { stats.fetches.push(String(url)); throw Error('No renderer fetch allowed') }
    let connectMode = 'ready', mapMode = 'normal', deferMemory = false
    const reply = (data, gen = generation) => Promise.resolve({ ok: true, generation: gen, data })
    const publish = (phase, box = owner) => {
      owner = box
      snapshot = { protocolVersion: 1, environment: 'simulation', generation: ++generation, sequence: ++sequence, phase,
        subject: phase === 'signed_out' ? null : { accountId: 'synthetic-account',
          ...(['ready', 'connecting', 'failed'].includes(phase) ? { deviceId: box, deviceName: `测试盒子 ${box}` } : {}),
          ...(phase === 'ready' ? { workspaceId: `workspace-${box}`, selectedPath: 'DIRECT' } : {}) },
        capabilities: { product: phase === 'ready', materialsRead: false, provisioning: true },
        ...(phase === 'failed' ? { error: { code: 'TRANSPORT_UNAVAILABLE', message: '合成连接失败', recovery: 'user_reconnect' } } : {}) }
      subscribers.forEach(fn => fn(structuredClone(snapshot)))
      return reply(snapshot)
    }
    window.__settingsPublish = publish
    window.__settingsConnectMode = mode => { connectMode = mode }
    window.__settingsMapMode = mode => { mapMode = mode }
    window.__settingsDeferMemory = () => { deferMemory = true }
    window.__settingsResolveLate = () => { stats.late.splice(0).forEach(done => done()) }
    const memory = { A: { mode: 'important', revision: 1 }, B: { mode: 'manual', revision: 1 } }
    const nudge = { enabled: true, maxPerDay: 3 }
    const claims = [{ id: 'claim-one', content: '合成理解：我在做一个产品。', section: 'matters', trustState: 'confirmed',
      layer: 'self_declared', scope: 'long_term', firstSeen: '2026-09-01T00:00:00Z', lastReaffirmed: '2026-09-01T00:00:00Z', evidence: [] }]
    const payload = request => {
      const id = request.operationId
      if (id === 'get_api_mindos_zhijun_onboarding') return { state: 'ready' }
      if (id === 'get_api_mindos_memory_policy') return structuredClone(memory[owner])
      if (id === 'put_api_mindos_memory_policy') { memory[owner] = { mode: request.body.mode, revision: memory[owner].revision + 1 }; return memory[owner] }
      if (id === 'get_api_mindos_nudges_policy') return nudge
      if (id === 'put_api_mindos_nudges_policy') { Object.assign(nudge, request.body); return nudge }
      if (id === 'get_api_mindos_ontology_stats') return { hasOntology: mapMode !== 'empty', entities: 0,
        claims: { confirmed: 1, working: 0, retracted: 0, superseded: 0 }, bySection: {}, inbox: 0, proposals: 0 }
      if (id === 'get_api_mindos_ontology_claims') return mapMode === 'fail' ? undefined : { items: mapMode === 'empty' ? [] : claims }
      if (id === 'get_api_mindos_ontology_inbox') return { items: [] }
      return undefined
    }
    window.zhijunDesktop = {
      protocolVersion: 1,
      subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn) },
      getSnapshot: () => reply(snapshot),
      listDevices: () => reply(['A', 'B'].map(deviceId => ({ deviceId, displayName: `测试盒子 ${deviceId}`, availability: 'online' }))),
      connect(_context, deviceId) { stats.controls.push('connect-' + deviceId); return publish(connectMode, deviceId) },
      disconnect() { stats.controls.push('disconnect'); return publish('selecting_device') },
      signOut() { stats.controls.push('signOut'); return publish('signed_out') },
      claimDevice() { stats.claims++; return reply({ deviceId: 'B', displayName: '测试盒子 B', availability: 'online' }) },
      openProvisioning() { stats.provisions++; return reply({ opened: true }) },
      product: {
        start(_context, request) {
          const id = String(stats.requests.length + 1).padStart(32, '0'), data = payload(request), gen = generation
          const deferred = deferMemory && request.operationId === 'get_api_mindos_memory_policy'
          if (deferred) deferMemory = false
          jobs.set(id, { data, gen, deferred }); stats.requests.push({ ...request, owner, gen })
          return reply({ id, state: 'queued', cursor: 0 })
        },
        poll(_context, { id }) {
          const job = jobs.get(id), status = job.data === undefined ? 503 : 200
          const response = { id, state: 'succeeded', cursor: 3, hasMore: false, events: [
            { seq: 1, kind: 'headers', status, headers: { 'content-type': 'application/json' } },
            { seq: 2, kind: 'chunk', data: new TextEncoder().encode(JSON.stringify(job.data ?? { detail: '合成读取失败' })) },
            { seq: 3, kind: 'end' },
          ] }
          if (job.deferred) return new Promise(done => stats.late.push(() => done({ ok: true, generation: job.gen, data: response })))
          return reply(response, job.gen)
        },
        cancel: (_context, { id }) => reply({ id, state: 'cancelled', cancelRequested: true }),
      },
    }
  })
  const settings = page.getByRole('link', { name: '设置', exact: true })
  const mainNav = page.getByRole('navigation', { name: '主导航', exact: true })
  const me = mainNav.getByRole('link', { name: '我的本体', exact: true })
  const panorama = page.getByRole('heading', { name: '本体全景', exact: true })
  const memory = page.getByTestId('memory-policy').getByRole('combobox')
  const goto = async path => { await page.evaluate(path => { location.hash = path }, path) }
  const layout = async name => {
    if (name.startsWith('ontology-')) await expect(page.locator('.zj-me__map svg').first()).toBeVisible()
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, name)
    await page.screenshot({ path: `${screenshots}/${name}.png`, fullPage: true, animations: 'disabled' })
  }
  await page.goto(origin + '/desktop.html#/settings')
  await expect(page.getByTestId('box-settings')).toBeVisible()
  await expect(page.getByTestId('workspace-settings')).toHaveCount(0)
  assert.equal(await page.evaluate(() => window.__settingsStats.requests.length), 0)
  await expect(page.locator('.product-topbar')).not.toContainText(/切换盒子|退出登录|刷新|测试盒子/)
  await page.getByTestId('refresh-devices').click()
  await page.getByTestId('open-provisioning').click()
  assert.equal(await page.evaluate(() => window.__settingsStats.provisions), 1)
  await page.getByTestId('claim-token').fill('ABCDEFGH23')
  await page.getByTestId('claim-device').click()
  assert.equal(await page.evaluate(() => window.__settingsStats.claims), 1)
  await layout('settings-disconnected-desktop')

  // Connection failure and cancellation remain inside settings, without business reads.
  await page.evaluate(() => window.__settingsConnectMode('failed'))
  await page.getByTestId('connect-A').click()
  await expect(page.getByTestId('secure-connection-progress')).toContainText('连接暂未就绪')
  await page.getByTestId('disconnect').click()
  await page.evaluate(() => window.__settingsConnectMode('connecting'))
  await page.getByTestId('connect-A').click()
  await page.getByRole('button', { name: '取消连接', exact: true }).click()
  await expect(page.getByTestId('connect-A')).toBeVisible()
  assert.equal(await page.evaluate(() => window.__settingsStats.requests.length), 0)
  await page.evaluate(() => window.__settingsConnectMode('ready'))
  await page.getByTestId('connect-A').click()
  await expect(memory).toHaveValue('important')
  assert.match(page.url(), /#\/settings$/)
  await layout('settings-connected-desktop')
  await memory.selectOption('manual')
  await expect(page.getByText('记忆整理偏好已保存，现有记忆不受影响', { exact: true })).toBeVisible()
  await memory.selectOption('important')
  await expect(memory).toBeEnabled()
  await page.getByRole('switch').uncheck()
  await expect(page.getByRole('switch')).not.toBeChecked()

  // Reload A's setting with a delayed response, then switch to B before it returns.
  await mainNav.getByRole('link', { name: '资料与边界', exact: true }).click()
  await expect(page.locator('.ws-app__content a[href="#/settings"]')).toHaveCount(0)
  await page.evaluate(() => window.__settingsDeferMemory())
  await settings.click()
  await expect.poll(() => page.evaluate(() => window.__settingsStats.late.length)).toBe(1)
  await page.getByTestId('switch-box').click()
  await expect(page.getByTestId('workspace-settings')).toHaveCount(0)
  const readsBefore = await page.evaluate(() => window.__settingsStats.requests.length)
  await me.click()
  await expect(page.getByTestId('workspace-unavailable')).toContainText('设置')
  await expect(page.getByTestId('disconnect')).toHaveCount(0)
  assert.equal(await page.evaluate(() => window.__settingsStats.requests.length), readsBefore)
  await settings.click()
  await page.getByTestId('connect-B').click()
  await expect(memory).toHaveValue('manual')
  await page.evaluate(() => window.__settingsResolveLate())
  await expect(memory).toHaveValue('manual')
  await expect(page.getByTestId('box-settings')).toContainText('测试盒子 B')
  assert.match(page.url(), /#\/settings$/)

  // Old storage cannot override the overview; repeated same-route clicks reset it.
  await me.click()
  await expect(panorama).toBeVisible()
  await expect(page.locator('.zj-me__map svg').first()).toBeVisible()
  await page.getByRole('button', { name: '摘要', exact: true }).click()
  await expect(page.getByRole('heading', { name: '知君对我的理解', exact: true })).toBeVisible()
  await me.click()
  await expect(panorama).toBeVisible()
  await page.getByRole('button', { name: '列表', exact: true }).click()
  await mainNav.getByRole('link', { name: '资料与边界', exact: true }).click()
  await me.click()
  await expect(panorama).toBeVisible()
  assert.equal(await page.evaluate(() => localStorage.getItem('zhijun.me.view')), 'list', 'view switching does not rewrite old storage')
  await layout('ontology-desktop')
  await page.getByRole('button', { name: '列表', exact: true }).click()
  await goto('/me?section=matters')
  await expect(page.getByRole('button', { name: '列表', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await goto('/me/inbox')
  await expect(page.getByRole('heading', { name: '知君最近学到的', exact: true })).toBeVisible()
  await goto('/me?section=proposals')
  await expect(page.getByRole('heading', { name: '需要你裁决', exact: true })).toBeVisible()
  await goto('/me?claim=claim-one')
  await expect(panorama).toBeVisible()
  await expect(page.getByTestId('selfmap-panel')).toContainText('合成理解')
  await page.getByRole('button', { name: '摘要', exact: true }).click()
  await goto('/me?claim=claim-one&from=source')
  await expect(panorama).toBeVisible()
  await expect(page.getByTestId('selfmap-panel')).toContainText('合成理解')

  for (const width of [900, 390]) {
    await page.setViewportSize({ width, height: 900 })
    await layout(`ontology-${width}`)
    if (width === 390) await page.getByRole('button', { name: '打开导航菜单', exact: true }).click()
    await settings.click()
    await expect(memory).toHaveValue('manual')
    await layout(`settings-${width}`)
    if (width === 390) await page.getByRole('button', { name: '打开导航菜单', exact: true }).click()
    await me.click()
    await expect(panorama).toBeVisible()
  }
  await page.setViewportSize({ width: 1440, height: 1000 })
  for (const mode of ['empty', 'fail']) {
    await settings.click()
    await page.evaluate(mode => window.__settingsMapMode(mode), mode)
    await me.click()
    await expect(panorama).toBeVisible()
    if (mode === 'empty') await expect(page.locator('.zj-me__explainer')).toBeVisible()
    else {
      await expect(page.getByText('合成读取失败', { exact: true })).toBeVisible()
      await page.evaluate(() => window.__settingsMapMode('normal'))
      await page.getByRole('button', { name: '重试', exact: true }).click()
      await expect(page.locator('.zj-me__map svg').first()).toBeVisible()
    }
  }
  // Refresh restores the default even when old storage says summary.
  await page.getByRole('button', { name: '摘要', exact: true }).click()
  await page.evaluate(() => {
    localStorage.setItem('zhijun.me.view', 'summary')
    sessionStorage.setItem('synthetic-connected', 'yes')
  })
  await page.reload()
  await expect(panorama).toBeVisible()
  await settings.click()
  await page.getByTestId('disconnect').click()
  await expect(page.getByTestId('box-settings')).toBeVisible()
  await expect(page.getByTestId('workspace-settings')).toHaveCount(0)
  await page.getByTestId('sign-out').click()
  await expect(page.locator('.ws-sidebar')).toHaveCount(0)
  assert.deepEqual(unexpected, [])
  assert.deepEqual(await page.evaluate(() => window.__settingsStats.fetches), [])
  assert.deepEqual(errors, [])
  console.log(`settings + ontology E2E passed; screenshots: ${screenshots}`)
} catch (error) {
  if (page) { await page.screenshot({ path: `${screenshots}/failure.png`, fullPage: true }).catch(() => {}); console.error(await page.locator('body').innerText().catch(() => '')) }
  throw error
} finally { await browser?.close(); await new Promise(done => server.close(done)) }
