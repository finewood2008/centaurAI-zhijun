// Isolated browser/component coverage: synthetic IPC, no backend, no user storage.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
const root = fileURLToPath(new URL('../dist-desktop/', import.meta.url))
const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }
const server = createServer(async (req, res) => {
  const pathname = new URL(req.url, 'http://127.0.0.1').pathname
  const file = resolve(root, '.' + (pathname === '/' ? '/desktop.html' : pathname))
  if (!file.startsWith(root.endsWith(sep) ? root : root + sep)) { res.writeHead(403).end(); return }
  try { const content = await readFile(file); res.setHeader('content-type', mime[extname(file)] || 'application/octet-stream'); res.end(content) }
  catch { res.writeHead(404).end() }
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const origin = `http://127.0.0.1:${server.address().port}`
let browser
try {
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const page = await context.newPage()
  const pageErrors = [], unexpected = []
  page.on('pageerror', error => pageErrors.push(error.message))
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin === origin && !url.pathname.startsWith('/api/')) return route.continue()
    unexpected.push(url.href); return route.abort()
  })
  await page.addInitScript(() => {
    let snapshot = { protocolVersion: 1, generation: 7, sequence: 1, environment: 'production', phase: 'failed', subject: { accountId: 'synthetic-owner' }, capabilities: { product: false, materialsRead: false }, error: { code: 'TRANSPORT_UNAVAILABLE', message: '合成连接失败', recovery: 'user_reconnect' } }
    const subscribers = new Set(), jobs = new Map()
    const stats = { deviceLists: 0, starts: [], subscriptions: 0, activeSubscriptions: 0, fetches: [] }
    window.__productTestStats = stats
    window.fetch = async url => { stats.fetches.push(String(url)); throw Error('Unexpected renderer fetch') }
    const result = data => Promise.resolve({ ok: true, generation: snapshot.generation, data })
    const set = phase => {
      snapshot = { ...snapshot, phase, generation: snapshot.generation + 1, sequence: snapshot.sequence + 1,
        subject: phase === 'signed_out' ? null : { accountId: 'synthetic-owner', ...(phase === 'ready' ? { deviceId: 'synthetic-device', deviceName: '合成盒子', workspaceId: 'synthetic-workspace' } : {}) },
        capabilities: { product: phase === 'ready', materialsRead: phase === 'ready' },
        error: phase === 'failed' ? { code: 'TRANSPORT_UNAVAILABLE', message: '合成连接失败', recovery: 'user_reconnect' } : null }
      subscribers.forEach(fn => fn(snapshot)); return result(snapshot)
    }
    window.__productTestFail = () => set('failed')
    window.__productTestStage = phase => set(phase)
    window.__productTestExpire = () => {
      snapshot = { ...snapshot, phase: 'failed', generation: snapshot.generation + 1, sequence: snapshot.sequence + 1,
        subject: null, capabilities: { product: false, materialsRead: false },
        error: { code: 'CONNECTIVITY_SESSION_EXPIRED', message: '连接会话已过期，请重新登录。', recovery: 'user_sign_in' } }
      subscribers.forEach(fn => fn(snapshot))
    }
    window.zhijunDesktop = {
      protocolVersion: 1,
      subscribe(fn) { stats.subscriptions++; stats.activeSubscriptions++; subscribers.add(fn); return () => { subscribers.delete(fn); stats.activeSubscriptions-- } },
      getSnapshot: () => result(snapshot),
      listDevices: () => { stats.deviceLists++; return result([{ deviceId: 'synthetic-device', displayName: '合成盒子', availability: 'online' }]) }, connect: () => set('ready'), disconnect: () => set('selecting_device'), signOut: () => set('signed_out'),
      materials: { list: () => { throw Error('Legacy materials must not load in product root') } },
      product: {
        start(_context, request) { const id = String(jobs.size + 1).padStart(32, '0'); jobs.set(id, request); stats.starts.push(request); return result({ id, state: 'queued', cursor: 0 }) },
        poll(_context, { id }) { return result({ id, state: 'succeeded', cursor: 3, hasMore: false, events: [
          { seq: 1, kind: 'headers', status: 503, headers: { 'content-type': 'application/json' } },
          { seq: 2, kind: 'chunk', data: new TextEncoder().encode(JSON.stringify({ detail: '合成故障：测试导航在业务错误时仍可操作' })) }, { seq: 3, kind: 'end' },
        ] }) },
        cancel: (_context, { id }) => result({ id, state: 'cancelled', cancelRequested: true }),
      },
    }
  })
  await page.goto(origin + '/desktop.html', { waitUntil: 'networkidle' })
  const navigation = page.getByRole('navigation', { name: '主导航' })
  await navigation.waitFor()
  assert.equal(await navigation.getByRole('link').count(), 5)
  await page.getByTestId('workspace-unavailable').waitFor()
  for (const label of ['今日来信', '对话', '我的本体', '判断', '资料与边界']) {
    await navigation.getByRole('link', { name: label, exact: true }).click()
    await page.waitForFunction(title => document.title === `${title} · 知君`, label)
    assert.equal(await page.getByTestId('workspace-unavailable').count(), 1)
  }
  await page.getByRole('link', { name: '偏好', exact: true }).click()
  await page.waitForFunction(() => document.title === '偏好 · 知君')
  assert.equal(await page.locator('.ws-app__content .page').count(), 0, 'offline preferences cannot mount business setup')
  let before = await page.evaluate(() => window.__productTestStats.starts.length)
  assert.equal(before, 0, 'navigation and onboarding guard cannot dispatch while workspace is unavailable')
  for (const phase of ['connecting', 'authorizing', 'disconnecting']) {
    await page.evaluate(phase => window.__productTestStage(phase), phase)
    assert.equal(await navigation.count(), 1, phase + ' retains navigation')
    assert.equal(await page.getByTestId('workspace-unavailable').count(), 1)
    assert.equal(await page.evaluate(() => window.__productTestStats.starts.length), 0)
  }
  await page.evaluate(() => window.__productTestFail())
  await page.getByTestId('disconnect').click()
  await page.getByTestId('connect-synthetic-device').waitFor()
  assert.equal(await navigation.count(), 1, 'device selection retains navigation')
  assert.equal(await page.evaluate(() => window.__productTestStats.starts.length), 0)
  await page.getByTestId('connect-synthetic-device').click()
  await page.getByTestId('workspace-unavailable').waitFor({ state: 'detached' })
  const connectionStatus = page.getByRole('status').filter({ hasText: '已连接盒子' })
  assert.match(await connectionStatus.innerText(), /合成盒子/)
  assert.doesNotMatch(await connectionStatus.innerText(), /synthetic-device/)

  for (const label of ['今日来信', '对话', '我的本体', '判断', '资料与边界']) {
    await navigation.getByRole('link', { name: label, exact: true }).click()
    await page.waitForFunction(title => document.title === `${title} · 知君`, label)
    await page.waitForTimeout(80)
    assert.equal(await page.getByRole('button', { name: '切换盒子', exact: true }).count(), 1)
  }
  for (const path of ['/settings', '/materials', '/materials/m_synthetic', '/knowledge', '/knowledge/new', '/knowledge/k_synthetic', '/recycle-bin', '/search', '/graph', '/me/charter', '/me/inbox', '/onboarding', '/onboarding/chat', '/onboarding/c/c_synthetic', '/c/c_synthetic', '/growth']) {
    await page.evaluate(path => { location.hash = path }, path)
    await page.waitForTimeout(100)
    assert.equal(await navigation.count(), 1, path)
  }
  assert.equal(await page.locator('.growth-page').count(), 1, 'the connected business page was actually mounted')
  await page.evaluate(() => window.__productTestFail())
  await page.getByTestId('workspace-unavailable').waitFor()
  assert.equal(await navigation.count(), 1, 'connection failure preserves the navigation shell')
  assert.equal(await page.locator('.growth-page').count(), 0, 'old business page is unmounted immediately')
  before = await page.evaluate(() => window.__productTestStats.starts.length)
  await navigation.getByRole('link', { name: '对话', exact: true }).click()
  await page.waitForFunction(() => document.title === '对话 · 知君')
  assert.equal(await page.evaluate(() => window.__productTestStats.starts.length), before, 'offline navigation cannot create a new job')
  await page.getByTestId('disconnect').click()
  await page.getByTestId('connect-synthetic-device').click()
  await page.getByTestId('workspace-unavailable').waitFor({ state: 'detached' })
  const stats = await page.evaluate(() => window.__productTestStats)
  assert.equal(stats.subscriptions, 1, 'navigation shares one host subscription')
  assert.equal(stats.activeSubscriptions, 1)
  assert.equal(stats.fetches.length, 0, 'all product paths use IPC')
  assert.ok(stats.starts.length > 15)
  assert.deepEqual(unexpected, [])
  assert.deepEqual(pageErrors, [])
  await page.evaluate(() => window.__productTestExpire())
  await page.getByTestId('password-login').waitFor()
  assert.equal(await navigation.count(), 0, 'workspace unmounts on confirmed connectivity-session expiry')
  console.log('product-navigation: logged-in failure keeps navigation without business dispatch, reselect/reconnect recovers, stale page unmounts; five navigation entries, preferences, all 15 page components, shared connection and expiry-to-login passed with synthetic HTTP errors')
} finally { await browser?.close(); await new Promise(resolve => server.close(resolve)) }
