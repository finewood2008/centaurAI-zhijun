'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const { mkdtemp, rm } = require('node:fs/promises')
const { tmpdir } = require('node:os')
const path = require('node:path')
const { _electron: electron } = require('../../mindos-web/node_modules/playwright')
const { expect } = require('../../mindos-web/node_modules/playwright/test')

async function openApp(t, mode) {
  const directory = await mkdtemp(path.join(tmpdir(), 'zhijun-m0-e2e-'))
  const env = { ...process.env, ZHIJUN_DESKTOP_MODE: mode,
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
  const preferences = await application.evaluate(({ BrowserWindow }) => {
    const p = BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences()
    return { sandbox: p.sandbox, contextIsolation: p.contextIsolation, nodeIntegration: p.nodeIntegration, webSecurity: p.webSecurity }
  })
  assert.deepEqual(preferences, { sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true })
})

test('simulation uses the real preload/runtime/policy: pagination, filters, switch and sign-out', async t => {
  const { page } = await openApp(t, 'simulation')
  await expect(page.getByTestId('environment')).toContainText('模拟环境 · 合成数据')
  await page.getByTestId('sign-in').click()
  await page.getByTestId('connect-synthetic-box-a').click()
  const rows = page.getByTestId('materials-table').locator('tbody tr')
  await expect(rows).toHaveCount(20)
  await expect(rows.first()).toContainText('模拟资料 001')
  await page.getByTestId('next-page').click()
  await expect(rows.first()).toContainText('模拟资料 021')
  await page.getByTestId('next-page').click()
  await expect(rows).toHaveCount(7)
  await expect(page.getByTestId('next-page')).toBeDisabled()
  await page.getByTestId('status-filter').selectOption('queued')
  await expect(rows).toHaveCount(10)
  for (const text of await rows.allTextContents()) assert.ok(text.includes('排队中'))
  await expect(page.locator('body')).not.toContainText('模拟私有目录')
  const safe = await page.evaluate(async () => {
    const api = window.zhijunDesktop
    const state = await api.getSnapshot()
    const context = () => ({ callId: crypto.randomUUID(), expectedGeneration: state.data.generation })
    return { page: await api.materials.list(context(), { limit: 20, offset: 0 }),
      invalid: await api.materials.list(context(), { limit: 51, offset: 0 }),
      injected: await api.materials.list(context(), { limit: 20, offset: 0, path: '/api/private' }) }
  })
  assert.equal(safe.page.ok, true)
  assert.deepEqual(Object.keys(safe.page.data.items[0]).sort(), ['createdAt', 'fileName', 'fileType', 'materialId', 'status'])
  assert.equal(safe.invalid.error.code, 'INVALID_REQUEST')
  assert.equal(safe.injected.error.code, 'INVALID_REQUEST')
  await page.getByTestId('disconnect').click()
  await expect(page.getByTestId('materials-table')).toHaveCount(0)
  await page.getByTestId('connect-synthetic-box-b').click()
  await expect(rows).toHaveCount(20)
  const second = await page.evaluate(async () => {
    const api = window.zhijunDesktop; const s = await api.getSnapshot()
    return api.materials.list({ callId: crypto.randomUUID(), expectedGeneration: s.data.generation }, { limit: 20, offset: 0 })
  })
  assert.ok(second.data.items.every(item => item.materialId.startsWith('synthetic-box-b-')))
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
