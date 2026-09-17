// Real built desktop UI + synthetic IPC only. No account, backend, SDK or external network.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdir, mkdtemp, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { extname, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const root = fileURLToPath(new URL('../dist-desktop/', import.meta.url))
const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' }
const server = createServer(async (request, response) => {
  const pathname = new URL(request.url, 'http://127.0.0.1').pathname
  const file = resolve(root, '.' + (pathname === '/' ? '/desktop.html' : pathname))
  if (!file.startsWith(root.endsWith(sep) ? root : root + sep)) { response.writeHead(403).end(); return }
  try {
    const content = await readFile(file)
    response.setHeader('content-type', mime[extname(file)] || 'application/octet-stream')
    response.end(content)
  } catch { response.writeHead(404).end() }
})
await new Promise(resolveListening => server.listen(0, '127.0.0.1', resolveListening))
const origin = `http://127.0.0.1:${server.address().port}`
const suppliedScreenshots = process.env.SECURE_CONNECTION_E2E_SCREENSHOT_DIR
const screenshots = suppliedScreenshots
  ? resolve(suppliedScreenshots)
  : await mkdtemp(join(tmpdir(), 'zhijun-secure-connection-e2e-'))
await mkdir(screenshots, { recursive: true })

let browser
let page
try {
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'no-preference' })
  page = await context.newPage()
  await page.clock.install({ time: new Date('2026-09-13T08:00:00Z') })
  const pageErrors = [], unexpected = []
  page.on('pageerror', error => pageErrors.push(error.message))
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (url.origin === origin && !url.pathname.startsWith('/api/')) return route.continue()
    unexpected.push(`${route.request().method()} ${url.href}`)
    return route.abort()
  })
  await page.addInitScript(() => {
    const capabilities = ready => ({
      materialsRead: ready, streamChat: ready, product: ready, uploads: ready,
      matters: ready, provisioning: false,
    })
    let sequence = 1
    let snapshot = {
      protocolVersion: 1, environment: 'production', generation: 7, sequence,
      phase: 'selecting_device', subject: { accountId: 'synthetic-owner' },
      capabilities: capabilities(false),
    }
    const subscribers = new Set()
    const pendingConnections = []
    let deviceListMode = 'success'
    let pendingDeviceList
    const stats = { starts: [], disconnects: [], connects: [], fetches: [], deviceLists: 0, signOuts: 0 }
    window.__secureConnectionStats = stats
    window.fetch = async input => { stats.fetches.push(String(input)); throw new Error('Unexpected renderer fetch') }
    const result = (data, generation = snapshot.generation) => Promise.resolve({ ok: true, generation, data })
    const publish = (phase, options = {}) => {
      const generation = options.newGeneration === false ? snapshot.generation : snapshot.generation + 1
      const environment = options.environment || 'production'
      const ready = phase === 'ready'
      const hasDevice = !['signed_out', 'selecting_device', 'authenticating'].includes(phase)
      const subject = phase === 'signed_out' || options.expired ? null : {
        accountId: 'synthetic-owner',
        ...(hasDevice ? { deviceId: 'synthetic-device', deviceName: '合成盒子' } : {}),
        ...(ready ? { workspaceId: 'a'.repeat(64) } : {}),
        ...(options.selectedPath ? { selectedPath: options.selectedPath } : {}),
      }
      snapshot = {
        protocolVersion: 1, environment, generation, sequence: ++sequence, phase, subject,
        capabilities: capabilities(ready),
        ...(phase === 'failed' ? { error: {
          code: options.expired ? 'SESSION_EXPIRED' : 'TRANSPORT_UNAVAILABLE',
          message: options.expired ? '登录已过期，请重新登录。' : '合成连接失败',
          recovery: options.expired ? 'user_sign_in' : 'user_reconnect',
        } } : {}),
      }
      for (const listener of subscribers) listener(structuredClone(snapshot))
      return structuredClone(snapshot)
    }
    window.__secureConnectionPublish = publish
    window.__setDeviceListMode = mode => { deviceListMode = mode }
    window.__resolveDeviceList = () => pendingDeviceList?.()
    window.__settleOldConnections = () => {
      for (const pending of pendingConnections.splice(0)) {
        pending.resolve({ ok: true, generation: pending.snapshot.generation, data: pending.snapshot })
      }
    }
    window.zhijunDesktop = {
      protocolVersion: 1,
      subscribe(listener) { subscribers.add(listener); return () => subscribers.delete(listener) },
      getSnapshot: () => result(structuredClone(snapshot)),
      listDevices: () => {
        stats.deviceLists++
        const devices = [{ deviceId: 'synthetic-device', displayName: '合成盒子', availability: 'online' }]
        if (deviceListMode === 'unavailable') return Promise.resolve({ ok: false, generation: snapshot.generation,
          error: { code: 'ACCOUNT_SERVICE_UNAVAILABLE', message: '账号服务暂时无法完成请求。', recovery: 'user_read' } })
        if (deviceListMode === 'pending') {
          const generation = snapshot.generation
          return new Promise(resolveDevices => { pendingDeviceList = () => resolveDevices({ ok: true, generation, data: devices }) })
        }
        return result(devices)
      },
      connect(context, deviceId) {
        stats.connects.push({ expectedGeneration: context.expectedGeneration, deviceId })
        const connecting = publish('connecting')
        return new Promise(resolveConnection => pendingConnections.push({ resolve: resolveConnection, snapshot: connecting }))
      },
      disconnect(context) {
        stats.disconnects.push({ expectedGeneration: context.expectedGeneration })
        return result(publish('selecting_device'))
      },
      signOut: () => { stats.signOuts++; return result(publish('signed_out')) },
      materials: { list: () => { throw new Error('Legacy materials must not load') } },
      product: {
        start(_context, request) {
          const id = String(stats.starts.length + 1).padStart(32, '0')
          stats.starts.push(request)
          return result({ id, state: 'queued', cursor: 0 })
        },
        poll(_context, { id }) {
          return result({ id, state: 'failed', cursor: 3, hasMore: false, events: [
            { seq: 1, kind: 'headers', status: 503, headers: { 'content-type': 'application/json' } },
            { seq: 2, kind: 'chunk', data: new TextEncoder().encode(JSON.stringify({ detail: '合成业务未启用' })) },
            { seq: 3, kind: 'end' },
          ] })
        },
        cancel: (_context, { id }) => result({ id, state: 'cancelled', cancelRequested: true }),
      },
    }
  })

  await page.goto(origin + '/desktop.html', { waitUntil: 'networkidle' })
  await page.getByTestId('connect-synthetic-device').waitFor()
  assert.equal(await page.evaluate(() => window.__secureConnectionStats.starts.length), 0)

  // Test the built Vue listener, not just its ref: native text inputs would
  // otherwise silently remove CR/LF from a pasted credential.
  const claimInput = page.getByTestId('claim-token')
  const pasteClaim = text => claimInput.evaluate((input, value) => {
    const clipboardData = new DataTransfer()
    clipboardData.setData('text/plain', value)
    const event = new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true })
    input.dispatchEvent(event)
    return event.defaultPrevented
  }, text)
  for (const invalid of ['ABCDEFGH23\n', '\rABCDEFGH23', 'abcdefgh23', ' ABCDEFGH23', 'ABCDEFGHIJKLMNOP2345A']) {
    await claimInput.fill('')
    assert.equal(await pasteClaim(invalid), true)
    assert.equal(await claimInput.inputValue(), '')
    assert.match(await page.locator('.form-error').last().textContent(), /粘贴内容无效/)
  }
  assert.equal(await pasteClaim('ABCDEFGH23'), true)
  assert.equal(await claimInput.inputValue(), 'ABCDEFGH23')
  await claimInput.evaluate(input => input.setSelectionRange(8, 10))
  assert.equal(await pasteClaim('45'), true)
  assert.equal(await claimInput.inputValue(), 'ABCDEFGH45')
  await claimInput.evaluate(input => input.setSelectionRange(0, input.value.length))
  assert.equal(await pasteClaim('ABCDEFGHIJKLMNOP2345'), true)
  assert.equal(await claimInput.inputValue(), 'ABCDEFGHIJKLMNOP2345')
  assert.equal(await claimInput.evaluate(input => {
    const event = new DragEvent('drop', { bubbles: true, cancelable: true })
    input.dispatchEvent(event)
    return event.defaultPrevented
  }), true)
  await claimInput.fill('')

  // Exercise the real controller's connect-pending cancellation path. The old
  // connect promise deliberately settles late after the generation was replaced.
  await page.getByTestId('connect-synthetic-device').click()
  const progress = page.getByTestId('secure-connection-progress')
  await progress.waitFor()
  assert.equal(await progress.getAttribute('data-phase'), 'connecting')
  assert.equal(await page.getByTestId('connection-stage-connect').getAttribute('data-state'), 'active')
  assert.equal(await page.getByTestId('connection-stage-authorize').getAttribute('data-state'), 'pending')
  assert.equal(await page.getByTestId('connection-stage-workspace').getAttribute('data-state'), 'pending')
  assert.equal(await progress.locator('[data-state="complete"]').count(), 0, 'connecting must not show a completed security step')
  assert.doesNotMatch(await progress.innerText(), /\d+\s*%|百分比/, 'connection UI must not invent progress percentages')
  assert.equal(await progress.getByTestId('connection-path').count(), 0, 'path is unknown until the native session exists')
  assert.match(await page.getByTestId('connection-security-note').innerText(), /将通过加密通道通信/,
    'connecting may describe the intended security boundary, not claim it is complete')
  assert.equal(await page.evaluate(() => window.__secureConnectionStats.starts.length), 0, 'business work cannot start while connecting')

  await page.clock.runFor(8100)
  await page.getByTestId('connection-wait-note').waitFor()
  assert.match(await page.getByTestId('connection-elapsed').innerText(), /8\s*秒/)
  assert.equal(await page.getByTestId('connection-elapsed').getAttribute('aria-hidden'), 'true')
  assert.doesNotMatch(await progress.locator('[aria-live="polite"]').innerText(), /秒|%/, 'screen-reader announcements must not tick every second')
  await page.clock.runFor(12000)
  assert.match(await page.getByTestId('connection-retry-note').innerText(), /取消.*稍后重新连接/)

  await page.getByTestId('connection-cancel').click()
  await page.getByTestId('connect-synthetic-device').waitFor()
  const cancelled = await page.evaluate(() => ({
    disconnects: window.__secureConnectionStats.disconnects,
    starts: window.__secureConnectionStats.starts.length,
  }))
  assert.equal(cancelled.disconnects.length, 1)
  assert.equal(cancelled.disconnects[0].expectedGeneration, 8, 'cancel must target the currently displayed connection generation')
  assert.equal(cancelled.starts, 0)
  await page.evaluate(() => window.__settleOldConnections())
  await page.clock.runFor(1)
  assert.equal(await progress.count(), 0, 'a late superseded connect result cannot reopen the progress card')

  // A new generation owns a fresh observation timer.
  await page.evaluate(() => window.__secureConnectionPublish('connecting'))
  await progress.waitFor()
  assert.equal(await page.getByTestId('connection-wait-note').count(), 0)
  assert.match(await page.getByTestId('connection-elapsed').innerText(), /0\s*秒/)
  await page.clock.runFor(1000)
  assert.match(await page.getByTestId('connection-elapsed').innerText(), /1\s*秒/)
  assert.equal(await page.evaluate(() => window.__secureConnectionStats.starts.length), 0)

  // selectedPath is shown only after transport establishment and is never inferred.
  await page.evaluate(() => window.__secureConnectionPublish('authorizing', { newGeneration: false, selectedPath: 'DIRECT' }))
  assert.equal(await progress.getAttribute('data-phase'), 'authorizing')
  assert.equal(await page.getByTestId('connection-stage-connect').getAttribute('data-state'), 'complete')
  assert.equal(await page.getByTestId('connection-stage-authorize').getAttribute('data-state'), 'active')
  assert.match(await progress.getByTestId('connection-path').innerText(), /直连通道/)
  assert.match(await page.getByTestId('connection-security-note').innerText(), /通过加密通道通信/)
  assert.equal(await page.getByTestId('connection-stage-workspace').getAttribute('data-state'), 'pending')

  await page.evaluate(() => {
    window.__secureConnectionPublish('connecting')
    window.__secureConnectionPublish('authorizing', { newGeneration: false, selectedPath: 'RELAY' })
  })
  assert.match(await progress.getByTestId('connection-path').innerText(), /安全中继通道/)

  await page.evaluate(() => window.__secureConnectionPublish('authorizing'))
  assert.equal(await progress.getByTestId('connection-path').count(), 0)
  assert.equal(await progress.locator('[aria-label="已建立加密连接"]').count(), 0)
  assert.doesNotMatch(await progress.innerText(), /直连通道|安全中继通道/, 'an absent selectedPath must stay unknown')

  await page.evaluate(() => window.__secureConnectionPublish('authorizing', { environment: 'simulation', selectedPath: 'DIRECT' }))
  await page.getByTestId('connection-simulation-note').waitFor()
  assert.match(await page.getByTestId('connection-simulation-note').innerText(), /模拟流程.*不代表.*真实安全连接/)
  assert.match(await page.getByTestId('connection-security-note').innerText(), /不证明真实连接.*资料外发也未获授权/)
  assert.match(await progress.getByTestId('connection-path').innerText(), /^模拟路径：直连通道$/,
    'a synthetic selectedPath may be shown only when it is explicitly labelled simulated')
  assert.equal(await page.getByTestId('connection-stage-connect').getAttribute('data-state'), 'simulated')

  await page.evaluate(() => window.__secureConnectionPublish('failed', { environment: 'production', selectedPath: 'DIRECT' }))
  assert.equal(await progress.getAttribute('data-phase'), 'failed')
  assert.equal(await progress.locator('[data-state="complete"]').count(), 0, 'failure cannot preserve a completed-security claim')
  assert.ok(await progress.locator('[data-state="error"], [data-state="blocked"]').count())
  assert.equal(await progress.getByTestId('connection-path').count(), 0)
  assert.equal(await page.locator('[aria-label="已建立加密连接"]').count(), 0)
  assert.equal(await page.evaluate(() => window.__secureConnectionStats.starts.length), 0, 'failed connection cannot dispatch business work')

  // The same real component must remain contained at supported desktop widths.
  await page.evaluate(() => window.__secureConnectionPublish('connecting', { environment: 'production' }))
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: width === 360 ? 780 : 900 })
    await page.clock.runFor(500)
    const layout = await progress.evaluate(element => {
      const box = element.getBoundingClientRect()
      return { pageOverflow: document.documentElement.scrollWidth > innerWidth + 1,
        left: box.left, right: box.right, viewport: innerWidth }
    })
    assert.equal(layout.pageOverflow, false, `${width}px must not introduce horizontal page scrolling`)
    assert.ok(layout.left >= -1 && layout.right <= layout.viewport + 1, `${width}px progress card must remain in the viewport`)
    await page.screenshot({ path: join(screenshots, `connecting-${width}.png`), fullPage: true, animations: 'disabled' })
  }

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.clock.runFor(1001)
  assert.equal(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches), true)
  assert.equal(await progress.evaluate(element => element.getAnimations({ subtree: true }).length), 0,
    'reduced-motion mode must not leave a repeating progress animation')

  // A ready snapshot still cannot earn the lock in simulation, or when the
  // production host did not provide an allowlisted selectedPath.
  await page.evaluate(() => window.__secureConnectionPublish('ready', { environment: 'simulation', selectedPath: 'DIRECT' }))
  await progress.waitFor({ state: 'detached' })
  assert.equal(await page.locator('[aria-label="已建立加密连接"]').count(), 0, 'simulation ready is not proof of a real encrypted connection')
  await page.getByRole('link', { name: '设置', exact: true }).click()
  await page.getByTestId('box-settings').waitFor()
  await page.evaluate(() => window.__secureConnectionPublish('authorizing', { environment: 'production' }))
  await progress.waitFor()
  await page.evaluate(() => window.__secureConnectionPublish('ready', { environment: 'production' }))
  await progress.waitFor({ state: 'detached' })
  assert.equal(await page.locator('[aria-label="已建立加密连接"]').count(), 0, 'production ready without selectedPath remains uncertified')

  // With both production and a known path, the transient card leaves and the
  // settings may certify that exact path. Business dispatch is permitted only now.
  await page.evaluate(() => window.__secureConnectionPublish('authorizing', { environment: 'production' }))
  await progress.waitFor()
  await page.evaluate(() => window.__secureConnectionPublish('ready', { environment: 'production', selectedPath: 'RELAY' }))
  await progress.waitFor({ state: 'detached' })
  const lock = page.locator('[aria-label="已建立加密连接"]')
  await lock.waitFor()
  assert.match(await page.getByTestId('connection-path').innerText(), /安全中继/)
  assert.match(await lock.getAttribute('title') || '', /通道已加密|加密数据/)
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.screenshot({ path: join(screenshots, 'ready-relay.png'), fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '切换盒子', exact: true }).click()
  await page.getByTestId('connect-synthetic-device').waitFor()
  assert.equal(await page.locator('[aria-label="已建立加密连接"]').count(), 0, 'disconnect must remove the settings lock immediately')

  // A transient account error preserves the authenticated subject. Exercise the
  // actual renderer/controller and click handler, including a pending retry.
  const recoveryBefore = await page.evaluate(() => structuredClone(window.__secureConnectionStats))
  await page.evaluate(() => {
    window.__setDeviceListMode('unavailable')
    window.__secureConnectionPublish('selecting_device')
  })
  await page.getByTestId('device-list-unavailable').waitFor()
  assert.match(await page.getByTestId('account').innerText(), /synthetic-owner/)
  assert.equal(await page.getByTestId('password-login').count(), 0)
  assert.equal(await page.getByText('暂无已完成授权的盒子。若刚提交认领，请保持盒子联网，稍后刷新设备。', { exact: true }).count(), 0,
    'unavailable device service must not be presented as an empty account')
  assert.equal(await page.getByTestId('reauthenticate').isEnabled(), true)
  await page.clock.runFor(30000)
  const unavailable = await page.evaluate(() => structuredClone(window.__secureConnectionStats))
  assert.equal(unavailable.deviceLists, recoveryBefore.deviceLists + 1, 'temporary account outage must not retry automatically')
  assert.equal(unavailable.signOuts, recoveryBefore.signOuts, 'temporary outage must not sign out automatically')
  assert.equal(unavailable.starts.length, recoveryBefore.starts.length, 'account recovery cannot dispatch business work')
  await page.screenshot({ path: join(screenshots, 'account-service-unavailable.png'), fullPage: true, animations: 'disabled' })
  await page.evaluate(() => window.__setDeviceListMode('pending'))
  await page.getByTestId('retry-account-devices').click()
  await page.getByText('正在获取盒子列表…', { exact: true }).waitFor()
  assert.equal(await page.getByTestId('refresh-devices').isDisabled(), true)
  assert.equal(await page.getByTestId('password-login').count(), 0)
  assert.match(await page.getByTestId('account').innerText(), /synthetic-owner/)
  await page.evaluate(() => window.__resolveDeviceList())
  await page.getByTestId('connect-synthetic-device').waitFor()
  assert.equal(await page.getByTestId('device-list-unavailable').count(), 0)
  assert.equal(await page.getByTestId('account-service-recovery').count(), 0)
  const recovered = await page.evaluate(() => structuredClone(window.__secureConnectionStats))
  assert.equal(recovered.deviceLists, unavailable.deviceLists + 1, 'one user click performs exactly one retry')
  assert.equal(recovered.signOuts, recoveryBefore.signOuts)
  await page.screenshot({ path: join(screenshots, 'account-devices-recovered.png'), fullPage: true, animations: 'disabled' })

  await page.evaluate(() => {
    window.__setDeviceListMode('unavailable')
    window.__secureConnectionPublish('selecting_device')
  })
  await page.getByTestId('reauthenticate').click()
  await page.getByTestId('password-login').waitFor()
  assert.equal(await page.getByTestId('show-login').getAttribute('aria-selected'), 'true')
  assert.equal(await page.getByTestId('account').count(), 0)
  assert.equal(await page.getByTestId('account-service-recovery').count(), 0)
  assert.equal(await page.evaluate(() => window.__secureConnectionStats.signOuts), recoveryBefore.signOuts + 1,
    'explicit reauthentication performs one sign-out and opens the login form')
  await page.screenshot({ path: join(screenshots, 'account-reauthenticate-login.png'), fullPage: true, animations: 'disabled' })

  // Re-enter an authenticated state so terminal expiry is tested independently
  // of the explicit sign-out above.
  await page.evaluate(() => window.__secureConnectionPublish('selecting_device'))
  await page.getByTestId('device-list-unavailable').waitFor()
  assert.equal(await page.getByTestId('password-login').count(), 0)
  const signOutsBeforeExpiry = await page.evaluate(() => window.__secureConnectionStats.signOuts)
  await page.evaluate(() => window.__secureConnectionPublish('failed', { expired: true }))
  await page.getByTestId('password-login').waitFor()
  assert.equal(await page.getByTestId('login-phone').isVisible(), true)
  assert.equal(await page.getByTestId('login-password').isVisible(), true)
  assert.equal(await page.getByTestId('sign-in').isEnabled(), true)
  assert.equal(await page.getByTestId('account').count(), 0)
  assert.equal(await page.getByTestId('connect-synthetic-device').count(), 0)
  assert.equal(await page.getByTestId('account-service-recovery').count(), 0)
  assert.match(await page.getByTestId('error').innerText(), /SESSION_EXPIRED/)
  assert.equal(await page.evaluate(() => window.__secureConnectionStats.signOuts), signOutsBeforeExpiry,
    'terminal expiry must render login without a renderer sign-out loop')
  await page.screenshot({ path: join(screenshots, 'session-expired-login.png'), fullPage: true, animations: 'disabled' })

  assert.deepEqual(unexpected, [], 'the synthetic desktop test must not reach an API or external origin')
  assert.deepEqual(await page.evaluate(() => window.__secureConnectionStats.fetches), [])
  assert.deepEqual(pageErrors, [])
  console.log(`secure connection E2E passed: truthful phases/path, timer reset, cancellation, safety claims, responsive/reduced-motion UI, account outage/manual retry recovery and expiry-to-login; screenshots ${screenshots}`)
} catch (error) {
  if (page) {
    await page.screenshot({ path: join(screenshots, 'failure.png'), fullPage: true }).catch(() => {})
    console.error((await page.locator('body').innerText().catch(() => '')).slice(-6000))
  }
  console.error(`Synthetic secure-connection failure screenshot: ${screenshots}`)
  throw error
} finally {
  await browser?.close()
  await new Promise(resolveClosing => server.close(resolveClosing))
}
