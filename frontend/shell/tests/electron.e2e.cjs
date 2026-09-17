'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const { mkdtemp, rm, writeFile } = require('node:fs/promises')
const { tmpdir } = require('node:os')
const path = require('node:path')
const { _electron: electron } = require('../../mindos-web/node_modules/playwright')
const { expect } = require('../../mindos-web/node_modules/playwright/test')

async function openApp(t, mode, productConfig) {
  const directory = await mkdtemp(path.join(tmpdir(), 'zhijun-m0-e2e-'))
  const configPath = path.join(directory, 'product.json')
  if (productConfig) await writeFile(configPath, JSON.stringify(productConfig))
  const env = { ...process.env, ZHIJUN_DESKTOP_MODE: mode, ZHIJUN_DESKTOP_CONFIG: productConfig ? configPath : '',
    ZHIJUN_DESKTOP_USER_DATA: directory, ZHIJUN_SHELL_NOGPU: '1' }
  delete env.ELECTRON_RUN_AS_NODE
  let application
  t.after(async () => {
    try {
      if (application) {
        const child = application.process()
        try { await application.close() }
        finally { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL') }
        assert.notEqual(child.exitCode, null, 'the owned host must exit')
      }
    } finally { await rm(directory, { recursive: true, force: true }) }
  })
  application = await electron.launch({ executablePath: require('electron'),
    args: [path.join(__dirname, '..')], env, timeout: 20000 })
  const page = await application.firstWindow()
  const errors = []; const network = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => { if (/^(https?|wss?|file):/.test(request.url())) network.push(request.url()) })
  await page.getByTestId('sign-in').waitFor()
  t.after(() => { assert.deepEqual(errors, [], 'renderer must have no unhandled errors'); assert.deepEqual(network, [], 'no renderer network or local backend requests') })
  return { application, page }
}

test('default app boots independently, keeps real access closed, and exposes no Node privileges', async t => {
  const { application, page } = await openApp(t, '')
  // A real click traverses the isolated preload, but this disconnected fixture
  // cannot reach any OS microphone API. Replace those APIs before the click.
  await application.evaluate(({ systemPreferences }) => {
    globalThis.__microphoneCalls = 0
    systemPreferences.getMediaAccessStatus = () => { globalThis.__microphoneCalls++; throw Error('unexpected microphone probe') }
    systemPreferences.askForMediaAccess = async () => { globalThis.__microphoneCalls++; throw Error('unexpected microphone prompt') }
  })
  await expect(page.getByTestId('environment')).toContainText('正式连接尚未配置')
  await page.getByTestId('sign-in').click()
  await expect(page.getByTestId('error')).toBeVisible()
  const result = await page.evaluate(async () => {
    const api = window.zhijunDesktop
    const snapshot = await api.getSnapshot()
    const invalid = await api.beginSignIn({ callId: 'invalid-request-1', expectedGeneration: 0, token: 'synthetic-secret' })
    return { snapshot, invalid, node: typeof window.require, process: typeof window.process,
      broadFetch: typeof api.fetch, exports: Object.keys(api) }
  })
  assert.equal(result.snapshot.data.environment, 'unconfigured')
  assert.equal(result.snapshot.data.capabilities.materialsRead, false)
  assert.equal(result.invalid.error.code, 'INVALID_REQUEST')
  assert.equal(JSON.stringify(result.invalid).includes('synthetic-secret'), false)
  assert.equal(result.node, 'undefined'); assert.equal(result.process, 'undefined')
  assert.equal(result.broadFetch, 'undefined')
  await page.evaluate(async () => {
    const snapshot = await window.zhijunDesktop.getSnapshot()
    const button = document.createElement('button')
    button.id = 'test-explicit-microphone'
    button.textContent = '权限隔离测试'
    button.addEventListener('click', () => {
      window.__microphoneResult = window.zhijunDesktop.product.requestMicrophone({
        callId: 'e2e-microphone-click', expectedGeneration: snapshot.data.generation,
      })
    })
    document.body.appendChild(button)
  })
  await application.evaluate(({ app, BrowserWindow }) => {
    app.focus({ steal: true })
    BrowserWindow.getAllWindows()[0]?.focus()
  })
  await page.bringToFront()
  await expect.poll(() => page.evaluate(() => document.hasFocus())).toBe(true)
  await page.locator('#test-explicit-microphone').click()
  const microphone = await page.evaluate(() => window.__microphoneResult)
  assert.ok(
    ['CONFIGURATION_REQUIRED', 'ACCESS_DENIED'].includes(microphone.error.code),
    'explicit activation must stop at the focus or unconfigured-runtime gate',
  )
  assert.equal(await application.evaluate(() => globalThis.__microphoneCalls), 0)
  await page.locator('#test-explicit-microphone').evaluate(button => button.remove())
  const preferences = await application.evaluate(({ BrowserWindow }) => {
    const p = BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences()
    return { sandbox: p.sandbox, contextIsolation: p.contextIsolation, nodeIntegration: p.nodeIntegration, webSecurity: p.webSecurity }
  })
  assert.deepEqual(preferences, { sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true })
})

test('v1 simulation keeps full product gated while real preload policy, device switching and sign-out remain isolated', async t => {
  const { page } = await openApp(t, 'simulation')
  await expect(page.getByTestId('environment')).toContainText('模拟环境 · 合成数据')
  await page.getByTestId('sign-in').click()
  await page.getByTestId('connect-synthetic-box-a').click()
  await expect(page.getByText('盒子已连接，知君工作区尚未就绪。请重新连接以加载完整产品服务。')).toBeVisible()
  await expect(page.getByTestId('materials-table')).toHaveCount(0)
  await expect(page.locator('.main-layout')).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText('模拟私有目录')
  const safe = await page.evaluate(async () => {
    const api = window.zhijunDesktop
    const state = await api.getSnapshot()
    const context = () => ({ callId: crypto.randomUUID(), expectedGeneration: state.data.generation })
    return { snapshot: state.data, product: await api.product.start(context(), { version: 1, requestId: 'synthetic-action', operationId: 'get_api_mindos_zhijun_home', params: {}, query: {}, body: null }),
      page: await api.materials.list(context(), { limit: 20, offset: 0 }),
      invalid: await api.materials.list(context(), { limit: 51, offset: 0 }),
      injected: await api.materials.list(context(), { limit: 20, offset: 0, path: '/api/private' }) }
  })
  assert.equal(safe.snapshot.capabilities.product, false)
  assert.equal(safe.snapshot.subject.workspaceId, undefined)
  assert.equal(safe.product.error.code, 'SESSION_NOT_READY')
  assert.equal(safe.page.ok, true)
  assert.deepEqual(Object.keys(safe.page.data.items[0]).sort(), ['createdAt', 'fileName', 'fileType', 'materialId', 'status'])
  assert.equal(safe.invalid.error.code, 'INVALID_REQUEST')
  assert.equal(safe.injected.error.code, 'INVALID_REQUEST')
  await page.getByTestId('disconnect').click()
  await expect(page.getByTestId('materials-table')).toHaveCount(0)
  await page.getByTestId('connect-synthetic-box-b').click()
  await expect(page.getByTestId('secure-connection-device')).toContainText('模拟盒子 B')
  await expect(page.getByTestId('connection-simulation-note')).toBeVisible()
  await expect(page.getByTestId('secure-connection-progress')).toContainText('工作区暂不可用')
  assert.equal(await page.evaluate(async () => (await window.zhijunDesktop.getSnapshot()).data.subject.deviceId), 'synthetic-box-b')
  await expect(page.getByTestId('disconnect')).toBeVisible()
  const second = await page.evaluate(async () => {
    const api = window.zhijunDesktop; const s = await api.getSnapshot()
    return api.materials.list({ callId: crypto.randomUUID(), expectedGeneration: s.data.generation }, { limit: 20, offset: 0 })
  })
  assert.ok(second.data.items.every(item => item.materialId.startsWith('synthetic-box-b-')))
  await page.getByRole('link', { name: '设置', exact: true }).click()
  await expect(page.getByTestId('box-settings')).toBeVisible()
  await page.getByTestId('sign-out').click()
  await expect(page.getByTestId('sign-in')).toBeVisible()
  await expect(page.getByTestId('materials-table')).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText('synthetic-box-b-material')
})

test('unknown mode stays unconfigured; unknown hash cannot enter legacy business routes', async t => {
  const { page } = await openApp(t, 'production')
  await page.evaluate(() => { location.hash = '/chat/forbidden' })
  const snapshot = await page.evaluate(() => window.zhijunDesktop.getSnapshot())
  assert.equal(snapshot.data.environment, 'unconfigured')
  assert.equal(snapshot.data.capabilities.streamChat, false)
  assert.equal(snapshot.data.capabilities.matters, false)
  await expect(page.getByTestId('materials-table')).toHaveCount(0)
})


test('configured Consumer shows password login; UTF-8 overbudget input is cleared and rejected before authentication', async t => {
  const { page } = await openApp(t, '', { version: 1, consumerBaseUrl: 'https://consumer.example.test/prod-api' })
  await expect(page.getByTestId('environment')).toContainText('账号服务已配置')
  await expect(page.getByTestId('show-register')).toBeVisible()
  await page.getByTestId('show-register').click()
  await expect(page.getByTestId('registration')).toBeVisible()
  await expect(page.getByTestId('send-registration-code')).toBeVisible()
  await page.getByTestId('show-login').click()
  await page.getByTestId('login-phone').fill('13800000000')
  await page.getByTestId('login-password').fill('汉'.repeat(25))
  await page.getByTestId('sign-in').click()
  await expect(page.getByTestId('error')).toContainText('INVALID_REQUEST')
  await expect(page.getByTestId('login-password')).toHaveValue('')
  const snapshot = await page.evaluate(() => window.zhijunDesktop.getSnapshot())
  assert.equal(snapshot.data.subject, null)
  assert.equal(snapshot.data.environment, 'production')
  assert.equal(snapshot.data.capabilities.materialsRead, false)
  assert.equal(JSON.stringify(snapshot).includes('13800000000'), false)
})
