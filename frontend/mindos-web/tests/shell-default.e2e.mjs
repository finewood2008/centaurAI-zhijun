// P0 验收：默认壳是沉浸壳，而用户明确选过的经典壳不会被这次默认变更掀掉。
// 与 product-navigation.e2e.mjs 同一套隔离方式：静态服务 dist-desktop + 合成 IPC，不连后端、不碰用户存储。
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
await new Promise(r => server.listen(0, '127.0.0.1', r))
const origin = `http://127.0.0.1:${server.address().port}`

const STORAGE_KEY = 'zhijun.shell'
let browser
try {
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const page = await context.newPage()
  const pageErrors = []
  page.on('pageerror', error => pageErrors.push(error.message))
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin === origin && !url.pathname.startsWith('/api/')) return route.continue()
    return route.abort()
  })
  await page.addInitScript(() => {
    const snapshot = { protocolVersion: 1, generation: 7, sequence: 1, environment: 'production', phase: 'ready',
      subject: { accountId: 'synthetic-owner', deviceId: 'synthetic-device', deviceName: '合成盒子', workspaceId: 'synthetic-workspace' },
      capabilities: { product: true, materialsRead: true }, error: null }
    const subscribers = new Set()
    window.fetch = async () => { throw Error('Unexpected renderer fetch') }
    const result = data => Promise.resolve({ ok: true, generation: snapshot.generation, data })
    window.zhijunDesktop = {
      protocolVersion: 1,
      subscribe(fn) { subscribers.add(fn); return () => subscribers.delete(fn) },
      getSnapshot: () => result(snapshot),
      listDevices: () => result([{ deviceId: 'synthetic-device', displayName: '合成盒子', availability: 'online' }]),
      connect: () => result(snapshot), disconnect: () => result(snapshot), signOut: () => result(snapshot),
      materials: { list: () => { throw Error('Legacy materials must not load') } },
      product: {
        start: () => result({ id: '0'.repeat(32), state: 'queued', cursor: 0 }),
        poll: () => result({ id: '0'.repeat(32), state: 'succeeded', cursor: 1, hasMore: false,
          events: [{ seq: 1, kind: 'headers', status: 503, headers: { 'content-type': 'application/json' } }, { seq: 2, kind: 'end' }] }),
        cancel: () => result({ id: '0'.repeat(32), state: 'cancelled', cancelRequested: true }),
      },
    }
  })

  const seals = page.getByRole('toolbar', { name: '印' })
  const nav = page.getByRole('navigation', { name: '主导航' })
  const stored = () => page.evaluate(key => localStorage.getItem(key), STORAGE_KEY)
  const shellParamInUrl = () => page.evaluate(() => location.href.includes('shell='))

  // 1. 全新设备、地址栏没有参数：走沉浸壳。这是 P0「沉浸壳为默认」的直接验收。
  await page.goto(origin + '/desktop.html', { waitUntil: 'networkidle' })
  await seals.waitFor()
  assert.equal(await nav.count(), 0, '默认壳里没有主导航')
  assert.equal(await stored(), null, '默认不写存储：没选过就是没选过，留给以后改默认')

  // 2. 明确选经典壳：参数生效、被清出地址栏、写进存储。
  await page.goto(origin + '/desktop.html?shell=classic', { waitUntil: 'networkidle' })
  await nav.waitFor()
  assert.equal(await seals.count(), 0, '经典壳里没有印坞')
  assert.equal(await stored(), 'classic', '选择写进存储')
  assert.equal(await shellParamInUrl(), false, 'shell 参数用完即从地址栏清掉')

  // 3. 关键回归：选过经典壳的人，不带参数再打开，必须还在经典壳。
  //    默认值变更不能把老用户掀到另一个壳里。
  await page.goto(origin + '/desktop.html', { waitUntil: 'networkidle' })
  await nav.waitFor()
  assert.equal(await seals.count(), 0, '存过的选择压过新默认值')

  // 4. 切回沉浸壳同样生效并持久化。
  await page.goto(origin + '/desktop.html?shell=immersive', { waitUntil: 'networkidle' })
  await seals.waitFor()
  assert.equal(await nav.count(), 0)
  assert.equal(await stored(), 'immersive')

  // 5. 存储被写坏时取默认（沉浸），不掉回经典。
  await page.evaluate(key => localStorage.setItem(key, 'garbage'), STORAGE_KEY)
  await page.goto(origin + '/desktop.html', { waitUntil: 'networkidle' })
  await seals.waitFor()
  assert.equal(await nav.count(), 0, '存储损坏时走默认壳')

  assert.deepEqual(pageErrors, [], 'no page errors')
  console.log('shell-default: 默认沉浸壳、参数优先并持久化且清出地址栏、已存的经典壳选择压过新默认、存储损坏回落默认 —— 全部通过（合成 IPC，无后端）')
} finally {
  await browser?.close()
  server.close()
}
