'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path')
const { readFile } = require('node:fs/promises')
const { EventEmitter } = require('node:events')
const {
  PROVISIONING_URL,
  isProvisioningEnabled,
  isLegacyPlaintextAllowed,
  createProvisioningWindow,
  closeProvisioningWindow,
} = require('../provisioning-window.cjs')

test('formal provisioning is limited to supported Electron platforms and Linux stays test-only', () => {
  for (const platform of ['darwin', 'win32']) {
    assert.equal(isProvisioningEnabled({ platform }), true, platform)
  }
  for (const platform of ['linux', 'aix', 'freebsd', 'openbsd', '', null]) {
    assert.equal(isProvisioningEnabled({ platform }), false, String(platform))
  }
  assert.equal(isProvisioningEnabled({ platform: 'linux', testBuild: true }), true)
  assert.equal(typeof createProvisioningWindow, 'function')
  const url = new URL(PROVISIONING_URL)
  assert.equal(url.protocol, 'file:')
  assert.equal(path.basename(url.pathname), 'setup.html')
  assert.equal(url.search, '')
  assert.equal(url.hash, '')
  assert.equal(url.username, '')
  assert.equal(url.password, '')
})

test('legacy plaintext Wi-Fi provisioning is limited to development opt-in or the explicit test build', () => {
  const enabled = { ZHIJUN_ALLOW_LEGACY_PLAINTEXT_PROVISIONING: '1' }
  assert.equal(isLegacyPlaintextAllowed({ isPackaged: false, env: enabled }), true)
  assert.equal(isLegacyPlaintextAllowed({ isPackaged: true, env: enabled }), false)
  assert.equal(isLegacyPlaintextAllowed({ isPackaged: true, testBuild: true, env: {} }), true)
  for (const value of [undefined, '', '0', 'true', 'yes', '01', 1]) {
    assert.equal(isLegacyPlaintextAllowed({ isPackaged: false,
      env: { ZHIJUN_ALLOW_LEGACY_PLAINTEXT_PROVISIONING: value } }), false, String(value))
  }
})

function windowFixture() {
  const created = []
  const partitions = []
  const requests = []
  const sdkCalls = []
  let disposed = 0
  class FakeContents extends EventEmitter {
    constructor() { super(); this.url = ''; this.destroyed = false; this.mainFrame = { url: '' } }
    isDestroyed() { return this.destroyed }
    getURL() { return this.url }
    setWindowOpenHandler(handler) { this.openHandler = handler }
  }
  class FakeWindow extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.webContents = new FakeContents(); this.destroyed = false
      this.shown = 0; this.focused = 0; created.push(this)
    }
    isDestroyed() { return this.destroyed }
    async loadURL(url) { this.webContents.url = url; this.webContents.mainFrame.url = url; this.loaded = url }
    show() { this.shown++ }
    focus() { this.focused++ }
    destroy() { if (!this.destroyed) { this.destroyed = true; this.webContents.destroyed = true; this.emit('closed') } }
  }
  const session = { fromPartition(partition, options) {
    const value = {
      partition, options,
      setPermissionCheckHandler(handler) { value.permissionCheck = handler },
      setPermissionRequestHandler(handler) { value.permissionRequest = handler },
      setBluetoothPairingHandler(handler) { value.pairing = handler },
      webRequest: { onBeforeRequest(filter, handler) { value.requestFilter = filter; value.beforeRequest = handler } },
    }
    partitions.push(value); return value
  } }
  const dialog = { async showMessageBox() { return { response: 0 } } }
  const importPicker = async () => ({ installElectronBluetoothPicker(options) {
    sdkCalls.push(options); return { dispose() { disposed++ } }
  } })
  const formal = {
    ipcMain: {},
    provisioningConfig: { trustedRootSpkiPins: ['sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='],
      trustedRootCertificatesPem: ['-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----'] },
    provisioningContext: { accountId: 'account-test', clientId: 'client-test', consumerApi: {}, resumeStore: {} },
    createBroker: () => ({ ready: Promise.resolve(), dispose: async () => {} }),
  }
  return { FakeWindow, session, dialog, importPicker, created, partitions, requests, sdkCalls,
    formal, get disposed() { return disposed } }
}

test('window host uses an ephemeral session, trusted Bluetooth permissions and a narrow picker', async t => {
  closeProvisioningWindow()
  t.after(closeProvisioningWindow)
  const f = windowFixture()
  const window = await createProvisioningWindow({ BrowserWindow: f.FakeWindow, session: f.session, ...f.formal,
    dialog: f.dialog, importPicker: f.importPicker, isPackaged: true,
    env: { ZHIJUN_ALLOW_LEGACY_PLAINTEXT_PROVISIONING: '1' } })
  assert.equal(window.loaded, PROVISIONING_URL)
  assert.equal(window.shown, 1)
  assert.equal(window.options.title, '添加 AI 盒子')
  assert.equal(window.options.backgroundColor, '#fffcf6')
  assert.equal(f.created.length, 1)
  assert.match(f.partitions[0].partition, /^zhijun-provisioning-[0-9a-f-]{36}$/)
  assert.equal(f.partitions[0].partition.startsWith('persist:'), false)
  assert.deepEqual(f.partitions[0].options, { cache: false })
  assert.equal(window.options.webPreferences.contextIsolation, true)
  assert.equal(window.options.webPreferences.nodeIntegration, false)
  assert.equal(window.options.webPreferences.webviewTag, false)
  assert.equal(window.options.webPreferences.sandbox, true)
  assert.equal(window.options.webPreferences.additionalArguments.length, 1)
  assert.match(window.options.webPreferences.additionalArguments[0], /^--zhijun-provisioning-flow=[0-9a-f-]{36}$/)
  assert.equal(window.options.webPreferences.additionalArguments.includes('--zhijun-legacy-plaintext-provisioning'), false,
    'a formal app never enables legacy plaintext through the environment')
  assert.equal(f.sdkCalls.length, 1)
  assert.equal(f.sdkCalls[0].webContents, window.webContents)
  assert.equal(f.sdkCalls[0].documentUrl, PROVISIONING_URL)
  assert.equal(f.sdkCalls[0].ipcMain, f.formal.ipcMain)
  assert.equal(f.sdkCalls[0].timeoutMs, 30000)
  assert.equal(f.sdkCalls[0].scanTimeoutMs, 8000)
  assert.equal(typeof f.sdkCalls[0].clock.monotonicMs, 'function')

  const isolated = f.partitions[0]
  assert.equal(isolated.permissionCheck(window.webContents, 'bluetooth', '', { isMainFrame: true }), true)
  assert.equal(isolated.permissionCheck(window.webContents, 'media', '', { isMainFrame: true }), false)
  assert.equal(isolated.permissionCheck(new f.FakeWindow({}).webContents, 'bluetooth', '', { isMainFrame: true }), false)
  let allowed
  isolated.permissionRequest(window.webContents, 'bluetooth', value => { allowed = value }, { isMainFrame: false })
  assert.equal(allowed, false)
  let requestDecision
  isolated.beforeRequest({ url: PROVISIONING_URL }, value => { requestDecision = value })
  assert.deepEqual(requestDecision, { cancel: false })
  isolated.beforeRequest({ url: 'https://outside.invalid/' }, value => { requestDecision = value })
  assert.deepEqual(requestDecision, { cancel: true })
  let paired
  await isolated.pairing({ frame: { url: PROVISIONING_URL }, pairingKind: 'confirm' }, value => { paired = value })
  assert.deepEqual(paired, { confirmed: false }, 'a lookalike frame object cannot authorize Bluetooth pairing')
  assert.deepEqual(window.webContents.openHandler(), { action: 'deny' })
  let prevented = false
  window.webContents.emit('will-navigate', { preventDefault() { prevented = true } }, 'https://outside.invalid/')
  assert.equal(prevented, true)

  const existing = await createProvisioningWindow({ BrowserWindow: f.FakeWindow, session: f.session, ...f.formal,
    dialog: f.dialog, importPicker: f.importPicker })
  assert.equal(existing, window)
  assert.equal(window.focused, 1)
  assert.equal(f.created.length, 2, 'the extra object was only the deliberately untrusted permission fixture')
  closeProvisioningWindow()
  assert.equal(window.destroyed, true)
  assert.equal(f.disposed, 1)
})

test('development legacy opt-in cannot enter the formal v2 preload', async t => {
  closeProvisioningWindow()
  t.after(closeProvisioningWindow)
  const f = windowFixture()
  const window = await createProvisioningWindow({ BrowserWindow: f.FakeWindow, session: f.session, ...f.formal,
    dialog: f.dialog, importPicker: f.importPicker, isPackaged: false,
    env: { ZHIJUN_ALLOW_LEGACY_PLAINTEXT_PROVISIONING: '1' } })
  assert.equal(window.options.webPreferences.additionalArguments.length, 1)
  assert.match(window.options.webPreferences.additionalArguments[0], /^--zhijun-provisioning-flow=/)
  assert.equal(window.options.webPreferences.additionalArguments.includes('--zhijun-legacy-plaintext-provisioning'), false)
  assert.equal(JSON.stringify(window.options).includes('password'), false)
})

test('packaged provisioning test build is visibly marked and arms only the isolated preload', async t => {
  closeProvisioningWindow()
  t.after(closeProvisioningWindow)
  const f = windowFixture()
  const window = await createProvisioningWindow({ BrowserWindow: f.FakeWindow, session: f.session,
    dialog: f.dialog, importPicker: f.importPicker, isPackaged: true, testBuild: true, env: {} })
  assert.equal(window.options.title, '添加 AI 盒子 · 配网测试版')
  assert.deepEqual(window.options.webPreferences.additionalArguments,
    ['--zhijun-provisioning-test-build', '--zhijun-legacy-plaintext-provisioning'])
})

test('closing during SDK loading cannot install a picker or revive the setup window', async () => {
  closeProvisioningWindow()
  const f = windowFixture()
  let finishImport
  const importPicker = () => new Promise(resolve => { finishImport = resolve })
  const opening = createProvisioningWindow({ BrowserWindow: f.FakeWindow, session: f.session, ...f.formal,
    dialog: f.dialog, importPicker })
  await Promise.resolve()
  closeProvisioningWindow()
  let installed = false
  finishImport({ installElectronBluetoothPicker() { installed = true; return { dispose() {} } } })
  await assert.rejects(opening, /PROVISIONING_WINDOW_CLOSED/)
  assert.equal(installed, false)
  assert.equal(f.created[0].destroyed, true)
})

test('pairing approval is revoked if the trusted setup window closes while confirmation is open', async t => {
  closeProvisioningWindow()
  t.after(closeProvisioningWindow)
  const f = windowFixture()
  let answer
  f.dialog.showMessageBox = () => new Promise(resolve => { answer = resolve })
  const window = await createProvisioningWindow({ BrowserWindow: f.FakeWindow, session: f.session, ...f.formal,
    dialog: f.dialog, importPicker: f.importPicker })
  let response
  const pairing = f.partitions[0].pairing({ frame: window.webContents.mainFrame,
    pairingKind: 'confirm' }, value => { response = value })
  closeProvisioningWindow()
  answer({ response: 1 })
  await pairing
  assert.deepEqual(response, { confirmed: false })
})

test('provisioning window files keep credentials outside the main renderer and use an isolated security boundary', async () => {
  const root = path.join(__dirname, '..')
  const [host, preload, html, css, renderer, main] = await Promise.all([
    readFile(path.join(root, 'provisioning-window.cjs'), 'utf8'),
    readFile(path.join(root, 'provisioning', 'preload.mjs'), 'utf8'),
    readFile(path.join(root, 'provisioning', 'setup.html'), 'utf8'),
    readFile(path.join(root, 'provisioning', 'setup.css'), 'utf8'),
    readFile(path.join(root, 'provisioning', 'renderer.js'), 'utf8'),
    readFile(path.join(root, 'main.js'), 'utf8'),
  ])

  assert.match(host, /contextIsolation:\s*true/)
  assert.match(host, /sandbox:\s*!testBuild/)
  assert.match(host, /testBuild\s*\?\s*['"]preload\.mjs['"]\s*:\s*['"]preload\.cjs['"]/)
  assert.match(host, /nodeIntegration:\s*false/)
  assert.match(host, /fromPartition\(`zhijun-provisioning-\$\{randomUUID\(\)\}`/) 
  assert.match(host, /setWindowOpenHandler\([^)]*\).*deny/s)
  assert.match(host, /will-navigate/)
  assert.match(host, /setPermissionRequestHandler/)
  assert.match(host, /setPermissionCheckHandler/)
  assert.match(host, /setBluetoothPairingHandler/)
  assert.doesNotMatch(host, /allowRunningInsecureContent:\s*true|webviewTag:\s*true|nodeIntegration:\s*true/)
  assert.match(main, /contextIsolation:\s*true,\s*sandbox:\s*true,\s*nodeIntegration:\s*false/)

  assert.match(preload, /contextBridge\.exposeInMainWorld/)
  assert.match(preload, /protocolVersion:\s*legacyArmed\s*\?\s*1\s*:\s*2/)
  assert.match(preload, /await currentScan\?\.catch/)
  assert.doesNotMatch(preload, /require\(['"](?:node:)?(?:fs|child_process|net|http|https)['"]\)/)
  assert.doesNotMatch(preload, /zhijun:invoke|fetch\(|XMLHttpRequest|WebSocket/)
  assert.match(html, /Content-Security-Policy/i)
  assert.match(html, /default-src\s+'none'/i)
  assert.match(html, /type="password"/i)
  assert.doesNotMatch(html, /https?:\/\//i)
  assert.match(renderer, /element\(['"]password['"]\)\.value\s*=\s*['"]['"]/) 
  assert.match(html, /配网测试版/)
  assert.match(renderer, /test-build-warning/)
  assert.doesNotMatch(renderer, /localStorage|sessionStorage|indexedDB/)

  assert.match(html, /class="environment-banner"/)
  assert.match(html, /class="page-heading"/)
  assert.match(html, /class="step(?: success)?"/)
  for (const token of ['#a6452e', '#b8543c', '#8f3a26', '#fffcf6', '#fbf8f1', '#d8d3c8', '#1d211f']) {
    assert.match(css, new RegExp(token), `${token} must match the main Zhijun visual system`)
  }
  assert.match(css, /\.legacy input\s*\{[^}]*width:\s*16px[^}]*height:\s*16px/s,
    'the legacy checkbox must not inherit full-width text input sizing')
  assert.match(css, /@media\s*\(max-width:\s*620px\)/)
})
