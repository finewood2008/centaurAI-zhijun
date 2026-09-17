'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { pathToFileURL } = require('node:url')
const { webcrypto } = require('node:crypto')
const { EventEmitter } = require('node:events')

const source = fs.readFileSync(path.join(__dirname, '..', 'provisioning', 'preload-formal.mjs'), 'utf8')
const productionBuilder = fs.readFileSync(path.join(__dirname, '..', 'electron-builder.yml'), 'utf8')
const testBuilder = fs.readFileSync(path.join(__dirname, '..', 'electron-builder.test.yml'), 'utf8')

test('formal preload owns Web Bluetooth only and delegates claim state to Main', () => {
  assert.match(source, /@nexusaos\/device-discovery-electron\/main/)
  assert.match(source, /DISCOVERY_PICKER_CHANNELS/)
  assert.match(source, /DISCOVERY_PICKER_CHANNELS\.settled/)
  assert.match(source, /createElectronBluetoothSelectionEndpoint/)
  assert.match(source, /createElectronGattTransportFactory/)
  assert.match(source, /createElectronGattTransportEndpoint/)
  assert.match(source, /CLAIM_INVOKE_CHANNEL/)
  assert.match(source, /TRANSPORT_COMMAND_CHANNEL/)
  assert.match(source, /contextBridge\.exposeInMainWorld\(['"]desktopProvisioning['"]/)
  assert.doesNotMatch(source, /@nexusaos\/device-provisioning-electron|BLUETOOTH_PICKER_CHANNELS/)
  assert.doesNotMatch(source, /createClaimCoordinator|createNodeProvisioningCryptoProvider|consumerBaseUrl|fetch\(/)
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB|console\./)
})

test('formal preload exposes no v1 provisioning operation and scrubs password bytes', () => {
  assert.match(source, /testBuild:\s*false/)
  assert.match(source, /legacyAllowed:\s*false/)
  assert.match(source, /encoded\?\.fill\(0\)/)
  assert.doesNotMatch(source, /createProvisioningSession|provisionWifi|scanWifiNetworks|allowLegacyPlaintextProvisioning/)
  assert.doesNotMatch(source, /verificationCode|VERIFICATION_CODE_MISMATCH/)
})

test('physical confirmation sends no code or other input to Main', async () => {
  const h = await preloadHarness()
  await h.api.confirmPhysicalDevice()
  assert.equal(h.invoked.at(-1).payload.kind, 'claim.confirmPhysicalDevice')
  assert.equal(Object.hasOwn(h.invoked.at(-1).payload, 'input'), false)
})

test('production package excludes v1 SDKs while the marked test flavor retains them', () => {
  assert.match(productionBuilder, /provisioning-broker\.cjs/)
  assert.match(productionBuilder, /!provisioning\/preload\.mjs/)
  assert.match(productionBuilder, /!node_modules\/@nexusaos\/device-provisioning-electron/)
  assert.match(productionBuilder, /!node_modules\/@nexusaos\/device-provisioning-uni/)
  assert.match(testBuilder, /provisioning-broker\.cjs/)
  assert.doesNotMatch(testBuilder, /!provisioning\/preload\.mjs/)
  assert.match(testBuilder, /zhijunProvisioningTestBuild:\s*true/)
})

const SESSION = '11111111-1111-4111-8111-111111111111'
const NEXT_SESSION = '22222222-2222-4222-8222-222222222222'
const FIRST = '33333333-3333-4333-8333-333333333333'
const SECOND = '44444444-4444-4444-8444-444444444444'
const channels = { candidates: 'candidates', settled: 'settled', select: 'select', cancel: 'cancel' }
const plain = value => JSON.parse(JSON.stringify(value))
const device = (candidateId = FIRST, advertisedName = 'CentaurOS-Setup-ABC123') => ({
  candidateId, advertisedName, transport: 'ble-gatt', transportHandle: 'private-handle',
  deviceId: 'private-provider-id', selectionLeaseId: 'private-lease', rssi: -40,
})

async function preloadHarness(options = {}) {
  const { createElectronBluetoothSelectionEndpoint } = await import(pathToFileURL(path.join(
    __dirname, '..', 'node_modules', '@nexusaos', 'local-provisioning-electron-ble', 'dist', 'index.js')).href)
  const listeners = new Map(), timers = new Map(), requests = [], sent = [], invoked = []
  let api, timerId = 0
  const navigator = { userActivation: { isActive: true }, bluetooth: {
    requestDevice(input) {
      assert.equal(navigator.userActivation.isActive, true, 'requestDevice retains click activation')
      return new Promise((resolve, reject) => {
        const request = { input, resolve, reject }
        requests.push(request)
        options.onRequestDevice?.(request)
      })
    },
  } }
  const context = vm.createContext({
    contextBridge: { exposeInMainWorld(_name, value) { api = value } },
    ipcRenderer: {
      on(channel, listener) { listeners.set(channel, listener) },
      send(channel, payload) { sent.push({ channel, payload: plain(payload) }); options.onSend?.(channel, payload) },
      async invoke(channel, payload) { invoked.push({ channel, payload: plain(payload) }); return { state: 'idle' } },
    },
    DISCOVERY_PICKER_CHANNELS: channels,
    createElectronBluetoothSelectionEndpoint,
    createElectronGattTransportFactory: () => ({}),
    createElectronGattTransportEndpoint: () => ({ closeAll: async () => {} }),
    process: { argv: [`--zhijun-provisioning-flow=${SESSION}`] }, navigator,
    crypto: webcrypto, performance: { now: () => 100 }, AbortController, TextEncoder,
    ArrayBuffer, Uint8Array,
    setTimeout(callback) { timers.set(++timerId, callback); return timerId },
    clearTimeout(id) { timers.delete(id) }, addEventListener() {},
  })
  vm.runInContext(source.replace(/^import[\s\S]*?from ['"][^'"]+['"]\n/gm, ''), context)
  const snapshots = []
  api.onDiscoverySnapshot(snapshot => snapshots.push(plain(snapshot)))
  return {
    api, navigator, requests, sent, invoked, snapshots,
    emit(channel, payload) { listeners.get(channel)(undefined, payload) },
    candidates(list, sequence = 1, sessionId = SESSION) {
      listeners.get(channels.candidates)(undefined, { sessionId, sequence, candidates: list })
    },
    expire() { for (const [id, callback] of [...timers]) { timers.delete(id); callback() } },
  }
}

test('formal discovery streams additions, removals and empty snapshots using only opaque display fields', async () => {
  const h = await preloadHarness()
  assert.deepEqual(h.snapshots.at(-1), { state: 'idle', candidates: [] })
  const scan = h.api.scan()
  assert.equal(h.requests.length, 1, 'requestDevice starts synchronously')
  h.candidates([device()])
  assert.deepEqual(plain(await scan), [{ candidateId: FIRST, name: 'CentaurOS-Setup-ABC123', shortCode: 'ABC123' }])
  h.candidates([device(), device(SECOND, 'CentaurOS-Setup-DEF456')], 2)
  assert.equal(h.snapshots.at(-1).candidates.length, 2)
  h.candidates([device(SECOND, '\u202eCentaurOS-Setup-DEF456\u0000')], 3)
  assert.deepEqual(h.snapshots.at(-1).candidates, [{ candidateId: SECOND, name: 'CentaurOS-Setup-DEF456', shortCode: 'DEF456' }])
  h.candidates([], 4)
  assert.deepEqual(h.snapshots.at(-1), { state: 'scanning', candidates: [] })
  h.candidates([device()], 3)
  assert.deepEqual(h.snapshots.at(-1).candidates, [], 'out-of-order update ignored')
  assert.doesNotMatch(JSON.stringify(h.snapshots), /private-|transportHandle|selectionLeaseId|deviceId|rssi/)
  const replay = []
  const unsubscribe = h.api.onDiscoverySnapshot(snapshot => replay.push(plain(snapshot)))
  unsubscribe()
  h.candidates([device()], 5)
  assert.equal(replay.length, 1)
  h.api.cancelScan()
})

test('formal discovery reports immediate request failure safely and allows retry', async () => {
  const h = await preloadHarness()
  const scan = h.api.scan()
  h.requests[0].reject(Object.assign(new Error('private OS detail'), { name: 'NotAllowedError' }))
  await assert.rejects(scan, { message: 'NotAllowedError' })
  assert.deepEqual(h.snapshots.at(-1), { state: 'error', candidates: [], errorCode: 'NotAllowedError' })
  const retry = h.api.scan()
  assert.equal(h.requests.length, 2)
  h.requests[1].reject(new Error('private adapter identifier'))
  await assert.rejects(retry, { message: 'DISCOVERY_RUNTIME_FAILURE' })
  assert.doesNotMatch(JSON.stringify(h.snapshots), /private/)
})

test('formal discovery retains completed rows but rejects expired selection and stale sessions', async () => {
  const h = await preloadHarness()
  const scan = h.api.scan()
  h.candidates([device()])
  await scan
  h.expire()
  assert.equal(h.snapshots.at(-1).state, 'completed')
  assert.equal(h.snapshots.at(-1).candidates.length, 1)
  assert.deepEqual(h.sent.at(-1), { channel: 'cancel', payload: { sessionId: SESSION } })
  await assert.rejects(h.api.select(FIRST), { message: 'LOCAL_SELECTION_LEASE_INVALID' })
  await assert.rejects(h.api.scan(), { message: 'CLAIM_OPERATION_IN_PROGRESS' })
  h.requests[0].reject(Object.assign(new Error('previous cancellation'), { name: 'NotFoundError' }))
  await new Promise(resolve => setImmediate(resolve))
  const retry = h.api.scan()
  h.candidates([device()], 99)
  h.emit('settled', { sessionId: SESSION, cancelled: true })
  h.candidates([device(SECOND)], 1, NEXT_SESSION)
  assert.equal(plain(await retry)[0].candidateId, SECOND)
  assert.equal(h.snapshots.at(-1).state, 'scanning')
  h.api.cancelScan()
})

test('formal discovery cancellation settles pending scan without cancelling claim, and full cancel does both', async () => {
  const h = await preloadHarness()
  const scan = h.api.scan()
  h.candidates([])
  h.api.cancelScan()
  await assert.rejects(scan, { message: 'PROVISIONING_SCAN_CANCELLED' })
  assert.equal(h.invoked.length, 0)
  assert.deepEqual(h.sent.at(-1), { channel: 'cancel', payload: { sessionId: SESSION } })
  h.requests[0].reject(Object.assign(new Error('cancelled'), { name: 'NotFoundError' }))
  await new Promise(resolve => setImmediate(resolve))
  const retry = h.api.scan()
  const rejected = assert.rejects(retry, { message: 'PROVISIONING_SCAN_CANCELLED' })
  await h.api.cancel()
  await rejected
  assert.equal(h.invoked.at(-1).payload.kind, 'claim.cancel')
})

for (const stop of ['cancelScan', 'cancel', 'timeout']) {
  test(`formal discovery ${stop} before first picker event blocks overlap and cancels late session`, async () => {
    const h = await preloadHarness()
    const scan = h.api.scan()
    const stopped = assert.rejects(scan, {
      message: stop === 'timeout' ? 'PROVISIONING_SCAN_TIMEOUT' : 'PROVISIONING_SCAN_CANCELLED',
    })
    if (stop === 'timeout') h.expire()
    else await h.api[stop]()
    await stopped
    assert.equal(h.sent.length, 0, 'no unknown session ID is invented')
    await assert.rejects(h.api.scan(), { message: 'CLAIM_OPERATION_IN_PROGRESS' })
    assert.equal(h.requests.length, 1, 'native requests cannot overlap')
    h.candidates([device()])
    assert.deepEqual(h.sent.at(-1), { channel: 'cancel', payload: { sessionId: SESSION } })
    assert.deepEqual(h.snapshots.at(-1).candidates, [], 'late rows remain hidden')
    h.emit('settled', { sessionId: SESSION, cancelled: true })
    await assert.rejects(h.api.scan(), { message: 'CLAIM_OPERATION_IN_PROGRESS' })
    h.requests[0].reject(Object.assign(new Error('cancelled'), { name: 'NotFoundError' }))
    await new Promise(resolve => setImmediate(resolve))
    const retry = h.api.scan()
    h.candidates([device()], 2)
    assert.equal(h.snapshots.at(-1).candidates.length, 0, 'retired session cannot become the new scan')
    h.candidates([device(SECOND)], 1, NEXT_SESSION)
    assert.equal(plain(await retry)[0].candidateId, SECOND)
    h.api.cancelScan()
  })
}

test('formal discovery empty expiry settles pending scan and exposes a specific timeout', async () => {
  const h = await preloadHarness()
  const scan = h.api.scan()
  h.candidates([])
  h.emit('settled', { sessionId: SESSION, cancelled: true })
  await assert.rejects(scan, { message: 'PROVISIONING_SCAN_TIMEOUT' })
  assert.deepEqual(h.snapshots.at(-1), { state: 'completed', candidates: [], errorCode: 'PROVISIONING_SCAN_TIMEOUT' })
})

test('formal discovery binds only the selected SDK lease and suppresses duplicate selection', async () => {
  const h = await preloadHarness()
  const scan = h.api.scan()
  h.candidates([device()])
  await scan
  const selected = h.api.select(FIRST)
  await assert.rejects(h.api.select(FIRST), { message: 'LOCAL_SELECTION_LEASE_INVALID' })
  assert.deepEqual(h.sent.at(-1), { channel: 'select', payload: { sessionId: SESSION, candidateId: FIRST } })
  h.requests[0].resolve({ id: 'private-provider-id', name: 'CentaurOS-Setup-ABC123' })
  await selected
  assert.equal(h.snapshots.at(-1).state, 'selected')
  assert.equal(h.invoked.at(-1).payload.kind, 'claim.bindSelected')
  assert.match(h.invoked.at(-1).payload.input.selectionLeaseId, /^[0-9a-f-]{36}$/)
  assert.notEqual(h.invoked.at(-1).payload.input.candidateId, FIRST)
  assert.doesNotMatch(JSON.stringify(h.snapshots), /private-|selectionLeaseId|transportHandle/)
})

test('formal discovery requires a click and cancellation cannot bind a late selection', async () => {
  const h = await preloadHarness()
  h.navigator.userActivation.isActive = false
  await assert.rejects(h.api.scan(), { message: 'PROVISIONING_USER_GESTURE_REQUIRED' })
  assert.equal(h.requests.length, 0)
  h.navigator.userActivation.isActive = true
  const scan = h.api.scan()
  h.candidates([device()])
  await scan
  const selected = h.api.select(FIRST)
  h.api.cancelScan()
  h.requests[0].resolve({ name: 'CentaurOS-Setup-ABC123' })
  await assert.rejects(selected, { message: 'LOCAL_SELECTION_LEASE_INVALID' })
  assert.equal(h.invoked.length, 0)
})

test('formal discovery uses product picker with SDK selection endpoint across native callback updates', async t => {
  const { DISCOVERY_PICKER_CHANNELS } = await import(pathToFileURL(path.join(
    __dirname, '..', 'node_modules', '@nexusaos', 'device-discovery-electron', 'dist', 'main.js')).href)
  const { installElectronBluetoothPicker } = require('../provisioning/picker.cjs')
  const ipcMain = new EventEmitter(), webContents = new EventEmitter()
  const frame = { url: 'file:///trusted/setup.html' }
  webContents.mainFrame = frame
  let callback, h
  webContents.send = (channel, payload) => h.emit(channel.split(':').at(-1), payload)
  const dispose = installElectronBluetoothPicker({ ipcMain, webContents, documentUrl: frame.url,
    clock: { monotonicMs: () => 100 }, channels: DISCOVERY_PICKER_CHANNELS })
  t.after(dispose)
  h = await preloadHarness({
    onRequestDevice(request) {
      callback = id => {
        if (id) request.resolve({ id, name: 'CentaurOS-Setup-ABC123' })
        else request.reject(Object.assign(new Error('cancelled'), { name: 'NotFoundError' }))
      }
      webContents.emit('select-bluetooth-device', { preventDefault() {} }, [], callback)
    },
    onSend(channel, payload) {
      ipcMain.emit(DISCOVERY_PICKER_CHANNELS[channel], { sender: webContents, senderFrame: frame }, payload)
    },
  })
  const scan = h.api.scan()
  webContents.emit('select-bluetooth-device', { preventDefault() {} }, [
    { deviceId: 'private-provider-one', deviceName: 'CentaurOS-Setup-ABC123' },
  ], id => callback(id))
  const first = plain(await scan)[0]
  webContents.emit('select-bluetooth-device', { preventDefault() {} }, [
    { deviceId: 'private-provider-one', deviceName: 'CentaurOS-Setup-ABC123' },
    { deviceId: 'private-provider-two', deviceName: 'CentaurOS-Setup-DEF456' },
  ], id => callback(id))
  assert.equal(h.snapshots.at(-1).candidates.length, 2)
  assert.equal(h.snapshots.at(-1).candidates[0].candidateId, first.candidateId)
  await h.api.select(first.candidateId)
  assert.equal(h.snapshots.at(-1).state, 'selected')
  assert.equal(h.invoked.at(-1).payload.kind, 'claim.bindSelected')
  assert.doesNotMatch(JSON.stringify(h.snapshots), /private-provider|selectionLeaseId|transportHandle/)
})
