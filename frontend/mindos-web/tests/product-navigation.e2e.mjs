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
    let snapshot = { protocolVersion: 1, generation: 7, sequence: 1, environment: 'production', phase: 'ready', subject: { accountId: 'synthetic-owner', deviceId: 'synthetic-device', workspaceId: 'synthetic-workspace' }, capabilities: { product: true, materialsRead: false }, error: null }
    const subscribers = new Set(), jobs = new Map()
    const stats = { starts: [], subscriptions: 0, activeSubscriptions: 0, fetches: [] }
    window.__productTestStats = stats
    window.fetch = async url => { stats.fetches.push(String(url)); throw Error('Unexpected renderer fetch') }
    const result = data => Promise.resolve({ ok: true, generation: snapshot.generation, data })
    const set = phase => { snapshot = { ...snapshot, phase, generation: snapshot.generation + 1, sequence: snapshot.sequence + 1, subject: phase === 'signed_out' ? null : snapshot.subject, capabilities: { product: false, materialsRead: false } }; subscribers.forEach(fn => fn(snapshot)); return result(snapshot) }
    window.zhijunDesktop = {
      protocolVersion: 1,
      subscribe(fn) { stats.subscriptions++; stats.activeSubscriptions++; subscribers.add(fn); return () => { subscribers.delete(fn); stats.activeSubscriptions-- } },
      getSnapshot: () => result(snapshot),
      listDevices: () => result([]), disconnect: () => set('selecting_device'), signOut: () => set('signed_out'),
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
  const stats = await page.evaluate(() => window.__productTestStats)
  assert.equal(stats.subscriptions, 1, 'navigation shares one host subscription')
  assert.equal(stats.activeSubscriptions, 1)
  assert.equal(stats.fetches.length, 0, 'all product paths use IPC')
  assert.ok(stats.starts.length > 15)
  assert.deepEqual(unexpected, [])
  assert.deepEqual(pageErrors, [])
  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await page.getByTestId('password-login').waitFor()
  assert.equal(await navigation.count(), 0, 'workspace unmounts on logout')
  console.log('product-navigation: five navigation entries, preferences, all 15 page components, shared connection and logout passed with synthetic HTTP errors')
} finally { await browser?.close(); await new Promise(resolve => server.close(resolve)) }
